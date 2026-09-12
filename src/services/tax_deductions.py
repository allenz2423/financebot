"""
Delilah Financial OS - Automated Tax Deduction Discovery & Smart Tagger Engine.

Scans the transaction ledger for tax-deductible business, professional, software,
office, telecommunication, and charitable expenses. Automatically tags candidates
and computes estimated tax savings.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import discord

from src.core.state import DB_PATH
from src.db.migrations import apply_all


@dataclass
class DeductionRule:
    category: str
    deductible_percentage: float  # e.g. 1.0 for 100%, 0.5 for 50% meals
    patterns: List[str]
    notes: str


# Curated tax deduction rules based on IRS Schedule C / 1099 guidelines
TAX_RULES: List[DeductionRule] = [
    DeductionRule(
        category="Software & Cloud Infrastructure",
        deductible_percentage=1.0,
        patterns=[
            r"\b(?:github|gitlab|bitbucket)\b",
            r"\b(?:aws|amazon\s*web\s*services|digitalocean|linode|vultr|hetzner|ovh)\b",
            r"\b(?:google\s*cloud|gcp|azure|cloudflare|fastly)\b",
            r"\b(?:openai|anthropic|cohere|huggingface|midjourney)\b",
            r"\b(?:vercel|netlify|render|railway|fly\.io|supabase)\b",
            r"\b(?:jetbrains|docker|postman|datadog|sentry|pagerduty|new\s*relic)\b",
            r"\b(?:slack|zoom|notion|atlassian|jira|trello|asana|linear)\b",
            r"\b(?:google\s*workspace|gsuite|microsoft\s*365|office\s*365)\b",
            r"\b(?:adobe|creative\s*cloud|figma|canva|sketch)\b",
            r"\b(?:apple\s*developer|google\s*play\s*developer)\b",
            r"\b(?:namecheap|godaddy|hover|porkbun|squarespace)\b",
            r"\b(?:mailchimp|sendgrid|twilio|stripe|hubspot)\b",
        ],
        notes="100% deductible business software & developer tools"
    ),
    DeductionRule(
        category="Tech Equipment & Office Supplies",
        deductible_percentage=1.0,
        patterns=[
            r"\b(?:best\s*buy|micro\s*center|b&h\s*photo|newegg)\b",
            r"\b(?:staples|office\s*depot|office\s*max)\b",
            r"\b(?:apple\s*store|dell|lenovo|framework|keychron)\b",
        ],
        notes="100% deductible hardware, peripherals, and office supplies"
    ),
    DeductionRule(
        category="Telecommunications & Internet",
        deductible_percentage=0.75,  # Standard dual-use allocation
        patterns=[
            r"\b(?:comcast|xfinity|spectrum|verizon\s*fios|at&t\s*internet)\b",
            r"\b(?:verizon|at&t|t-mobile|mint\s*mobile|google\s*fi)\b",
        ],
        notes="75% allocated business phone and internet deduction"
    ),
    DeductionRule(
        category="Professional Development & Education",
        deductible_percentage=1.0,
        patterns=[
            r"\b(?:coursera|udemy|edx|pluralsight|oreilly|frontend\s*masters)\b",
            r"\b(?:linkedin\s*premium|linkedin\s*learning)\b",
            r"\b(?:acm|ieee|usenix)\b",
            r"\b(?:cpa|legalzoom|turbotax|h&r\s*block)\b",
        ],
        notes="100% deductible professional certifications, courses, and tax prep"
    ),
    DeductionRule(
        category="Business Travel & Lodging",
        deductible_percentage=1.0,
        patterns=[
            r"\b(?:delta|united\s*airlines|american\s*airlines|southwest|jetblue|alaska\s*air)\b",
            r"\b(?:marriott|hilton|hyatt|ihg|airbnb|hotels\.com)\b",
            r"\b(?:amtrak|hertz|enterprise|avis|national\s*car)\b",
        ],
        notes="100% deductible business transit and lodging"
    ),
    DeductionRule(
        category="Business Meals (50%)",
        deductible_percentage=0.5,
        patterns=[
            r"\b(?:client\s*dinner|client\s*lunch|business\s*meal)\b",
        ],
        notes="50% deductible client business entertainment/dining"
    ),
    DeductionRule(
        category="Charitable Contributions",
        deductible_percentage=1.0,
        patterns=[
            r"\b(?:red\s*cross|goodwill|salvation\s*army|unicef|doctors\s*without\s*borders)\b",
            r"\b(?:wikipedia|wikimedia|eff|electronic\s*frontier|free\s*software\s*foundation)\b",
            r"\b(?:united\s*way|st\s*jude|habitat\s*for\s*humanity)\b",
        ],
        notes="100% deductible 501(c)(3) charitable donations"
    ),
]


def match_transaction_deduction(
    merchant: str,
    category: Optional[str] = None,
) -> Optional[Tuple[DeductionRule, str]]:
    """
    Evaluates whether a transaction matches known tax deduction patterns.
    Returns (DeductionRule, matched_pattern) or None.
    """
    search_text = f"{merchant or ''} {category or ''}".lower()

    for rule in TAX_RULES:
        for pat in rule.patterns:
            if re.search(pat, search_text, re.IGNORECASE):
                return rule, pat

    return None


def scan_and_discover_deductions(
    *,
    user_id: str,
    year: Optional[int] = None,
    auto_apply: bool = False,
    marginal_tax_rate: float = 0.30,  # Estimated 30% combined fed + state
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Scans user's ledger for untagged tax-deductible transactions.
    Optionally applies tags to the database.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("scan_and_discover_deductions: forced isolation violation — user_id is required")

    target_year = year or datetime.now().year
    start_date = f"{target_year}-01-01"
    end_date = f"{target_year + 1}-01-01"

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        apply_all(db_conn)
        c = db_conn.cursor()

        # Query all expense transactions for the given year
        c.execute("""
            SELECT id, date, COALESCE(clean_merchant, merchant) as m_name, amount, category,
                   tax_deductible, tax_category
            FROM transactions
            WHERE user_id = ?
              AND date >= ? AND date < ?
              AND amount > 0
              AND merchant NOT LIKE '%System Balance Sync%'
            ORDER BY date DESC
        """, (user_id, start_date, end_date))
        rows = c.fetchall()

        already_tagged_count = 0
        already_tagged_deductible = 0.0
        candidates: List[Dict[str, Any]] = []
        applied_count = 0

        category_summary: Dict[str, Dict[str, Any]] = {}

        for r in rows:
            tx_id, tx_date, merch, amt, cat, is_tax, tax_cat = r[0], r[1], r[2] or "Unknown", float(r[3] or 0.0), r[4] or "", bool(r[5]), r[6]

            if is_tax:
                already_tagged_count += 1
                already_tagged_deductible += amt
                continue

            # Check if this untagged transaction matches tax rules
            match_res = match_transaction_deduction(merch, cat)
            if match_res:
                rule, pat = match_res
                deductible_amt = round(amt * rule.deductible_percentage, 2)
                tax_savings = round(deductible_amt * marginal_tax_rate, 2)

                item = {
                    "transaction_id": tx_id,
                    "date": tx_date,
                    "merchant": merch,
                    "gross_amount": amt,
                    "deductible_amount": deductible_amt,
                    "deductible_percentage": int(rule.deductible_percentage * 100),
                    "tax_category": rule.category,
                    "estimated_tax_savings": tax_savings,
                    "notes": rule.notes,
                }
                candidates.append(item)

                # Accumulate category totals
                cat_key = rule.category
                if cat_key not in category_summary:
                    category_summary[cat_key] = {
                        "count": 0,
                        "gross": 0.0,
                        "deductible": 0.0,
                        "tax_savings": 0.0,
                    }
                category_summary[cat_key]["count"] += 1
                category_summary[cat_key]["gross"] += amt
                category_summary[cat_key]["deductible"] += deductible_amt
                category_summary[cat_key]["tax_savings"] += tax_savings

                if auto_apply:
                    c.execute("""
                        UPDATE transactions
                        SET tax_deductible = 1, tax_category = ?
                        WHERE id = ? AND user_id = ?
                    """, (rule.category, tx_id, user_id))
                    applied_count += 1

        if auto_apply and applied_count > 0:
            db_conn.commit()

        total_potential_gross = sum(c["gross_amount"] for c in candidates)
        total_potential_deductible = sum(c["deductible_amount"] for c in candidates)
        total_estimated_tax_savings = sum(c["estimated_tax_savings"] for c in candidates)

        return {
            "user_id": user_id,
            "year": target_year,
            "auto_applied": auto_apply,
            "applied_count": applied_count,
            "candidates_found_count": len(candidates),
            "already_tagged_count": already_tagged_count,
            "already_tagged_deductible": round(already_tagged_deductible, 2),
            "total_potential_gross": round(total_potential_gross, 2),
            "total_potential_deductible": round(total_potential_deductible, 2),
            "estimated_tax_savings": round(total_estimated_tax_savings, 2),
            "marginal_tax_rate_used": marginal_tax_rate,
            "categories": [
                {
                    "category": k,
                    "count": v["count"],
                    "gross": round(v["gross"], 2),
                    "deductible": round(v["deductible"], 2),
                    "tax_savings": round(v["tax_savings"], 2),
                }
                for k, v in sorted(category_summary.items(), key=lambda x: x[1]["deductible"], reverse=True)
            ],
            "sample_candidates": candidates[:15],
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_tax_discovery_embed(data: Dict[str, Any]) -> discord.Embed:
    """Format deduction discovery into a Discord embed."""
    year = data["year"]
    candidates = data["candidates_found_count"]
    deductible = data["total_potential_deductible"]
    savings = data["estimated_tax_savings"]
    applied = data["auto_applied"]

    color = discord.Color.green() if candidates > 0 else discord.Color.blue()
    title = f"🧾 Tax Deduction Discovery — {year}"

    status_str = f"✅ **Applied {data['applied_count']} tags to ledger!**" if applied else "👀 **Preview Mode** (Run `!tax scan apply` to tag all)"

    embed = discord.Embed(
        title=title,
        color=color,
        description=(
            f"{status_str}\n\n"
            f"**New Deduction Candidates:** **{candidates}** transactions\n"
            f"**Potential Deductions:** **${deductible:,.2f}**\n"
            f"💰 **Estimated Tax Refund/Savings:** **${savings:,.2f}** (at {int(data['marginal_tax_rate_used'] * 100)}% tax bracket)"
        )
    )

    for cat in data["categories"][:6]:
        embed.add_field(
            name=f"📁 {cat['category']} ({cat['count']} txs)",
            value=f"Deductible: **${cat['deductible']:,.2f}** · Saves: **${cat['tax_savings']:,.2f}**",
            inline=True
        )

    samples = data.get("sample_candidates", [])
    if samples:
        sample_lines = [
            f"• `{s['date']}` **{s['merchant']}**: **${s['deductible_amount']:,.2f}** ({s['tax_category']})"
            for s in samples[:6]
        ]
        embed.add_field(name="🔍 Discovered Candidates", value="\n".join(sample_lines), inline=False)

    embed.set_footer(text="Use !tax scan apply to tag · !tax export to generate CPA-ready spreadsheet")
    return embed


def format_tax_discovery_report(data: Dict[str, Any]) -> str:
    """Format deduction discovery into a text report."""
    lines = [
        f"🧾 AUTOMATED TAX DEDUCTION AUDIT — {data['year']}",
        "=" * 60,
        f"New Candidates Found:       {data['candidates_found_count']} transactions",
        f"Potential Write-Offs:       ${data['total_potential_deductible']:,.2f}",
        f"Estimated Tax Savings:      ${data['estimated_tax_savings']:,.2f} (marginal rate: {data['marginal_tax_rate_used'] * 100:.0f}%)",
        f"Already Tagged in Ledger:   {data['already_tagged_count']} txs (${data['already_tagged_deductible']:,.2f})",
        "-" * 60,
    ]

    for cat in data["categories"]:
        lines.append(f"• {cat['category']}:")
        lines.append(f"   {cat['count']} items | Deductible: ${cat['deductible']:,.2f} | Saves: ~${cat['tax_savings']:,.2f}")

    lines.append("")
    lines.append("SAMPLE DISCOVERED ITEMS:")
    for s in data["sample_candidates"]:
        lines.append(f"   {s['date']} | {s['merchant']:<25} | ${s['deductible_amount']:>8,.2f} | {s['tax_category']}")

    return "\n".join(lines)
