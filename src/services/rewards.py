import json
import sqlite3
import re
from typing import Dict, Any, List, Optional
from src.core.state import DB_PATH

# Market-wide baseline multipliers
DEFAULT_CARD_MULTIPLIERS = {
    "Chase Sapphire Reserve": {"travel": 3.0, "dining": 3.0, "default": 1.0},
    "Amex Gold": {"dining": 4.0, "groceries": 4.0, "travel": 3.0, "default": 1.0},
    "Citi Double Cash": {"default": 2.0}
}

# Expanded profile library matching user card variations
KNOWN_CARD_PROFILES = {
    "amex gold": {"dining": 4.0, "groceries": 4.0, "travel": 3.0, "default": 1.0},
    "chase sapphire reserve": {"travel": 3.0, "dining": 3.0, "default": 1.0},
    "chase sapphire preferred": {"travel": 2.0, "dining": 3.0, "streaming": 3.0, "online groceries": 3.0, "default": 1.0},
    "citi double cash": {"default": 2.0},
    "blue cash everyday": {"groceries": 3.0, "online retail": 3.0, "gas": 3.0, "default": 1.0},
    "blue cash preferred": {"groceries": 6.0, "streaming": 6.0, "transit": 3.0, "gas": 3.0, "default": 1.0},
    "savor": {"dining": 4.0, "entertainment": 4.0, "streaming": 4.0, "groceries": 3.0, "default": 1.0},
    "savorone": {"dining": 3.0, "entertainment": 3.0, "streaming": 3.0, "groceries": 3.0, "default": 1.0},
    "quicksilver": {"default": 1.5},
    "chase freedom unlimited": {"dining": 3.0, "drugstores": 3.0, "default": 1.5},
    "chase freedom flex": {"dining": 3.0, "drugstores": 3.0, "default": 1.0},
    "wells fargo active cash": {"default": 2.0},
    "discover it": {"default": 1.0},
    "apple card": {"apple": 3.0, "apple pay": 2.0, "default": 1.0},
    "bilt": {"dining": 3.0, "travel": 2.0, "rent": 1.0, "default": 1.0},
}

MERCHANT_CATEGORY_RULES = {
    r"starbucks|dunkin|mcdonald|chipotle|wendy|burger|taco|chick-fil-a|subway|panera|pizza|coffee|cafe|restaurant|bistro|deli|bar\b|grill": "dining",
    r"whole\s*foods|trader\s*joe|safeway|kroger|wegmans|aldi|publix|heb|market|grocery|supermarket": "groceries",
    r"uber(?!\s*eats)|lyft|delta|united\s*air|american\s*air|southwest|jetblue|hotel|marriott|hilton|hyatt|airbnb|expedia": "travel",
    r"amazon|target\.com|walmart\.com|ebay|bestbuy|apple\.com": "online retail",
    r"chevron|shell|bp|exxon|mobil|sunoco|wawa|speedway|gas\b": "gas",
    r"netflix|spotify|hulu|disney|hbo|max|apple\s*tv|youtube\s*prem": "streaming",
}

def infer_category(category: str, merchant: Optional[str] = None) -> str:
    """Normalize or infer category from merchant name."""
    cat = (category or "").strip().lower()
    if cat and cat not in {"other", "general", "uncategorized"}:
        return cat

    if merchant:
        m_lower = merchant.lower()
        for pattern, inferred in MERCHANT_CATEGORY_RULES.items():
            if re.search(pattern, m_lower):
                return inferred

    return cat or "other"


def get_user_wallet_cards(user_id: str) -> List[Dict[str, Any]]:
    """Retrieve credit cards owned by user from DB."""
    cards = []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # 1. From user_debts
            c.execute("SELECT name FROM user_debts WHERE user_id = ?", (user_id,))
            for (name,) in c.fetchall():
                clean_name = re.sub(r"\(credit card\)|®|™", "", name, flags=re.IGNORECASE).strip()
                cards.append(clean_name)

            # 2. From distinct accounts in transactions that match credit cards
            c.execute("""
                SELECT DISTINCT account_used FROM transactions 
                WHERE user_id = ? AND account_used IS NOT NULL AND account_used NOT IN ('Checking', '360 Checking', 'Savings', '360 Performance Savings', 'Plaid Sync', 'MONEY')
            """, (user_id,))
            for (acc,) in c.fetchall():
                clean_name = re.sub(r"\(credit card\)|®|™", "", acc, flags=re.IGNORECASE).strip()
                if clean_name and clean_name not in cards:
                    cards.append(clean_name)
    except Exception:
        pass

    # Map cards to matched profiles
    wallet = []
    for card in cards:
        card_lower = card.lower()
        matched_profile = None
        for key, prof in KNOWN_CARD_PROFILES.items():
            if key in card_lower:
                matched_profile = prof
                break
        if matched_profile:
            wallet.append({"name": card, "multipliers": matched_profile})
        else:
            wallet.append({"name": card, "multipliers": {"default": 1.0}})
    return wallet


def recommend_best_card(*, user_id: str, category: str = "", merchant: str = None, only_wallet: bool = False) -> dict:
    """
    Recommends the best credit card to swipe based on category, merchant, and user's wallet.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("recommend_best_card: forced isolation violation")

    eff_category = infer_category(category, merchant)
    wallet = get_user_wallet_cards(user_id)

    # If only_wallet is requested or user has cards and not explicitly opting for market
    cards_to_check = {}
    if wallet:
        for item in wallet:
            cards_to_check[item["name"]] = item["multipliers"]
    
    # If wallet empty or user requested market view, fall back to DEFAULT_CARD_MULTIPLIERS
    if not cards_to_check or not only_wallet:
        # If user explicitly wants market comparison or has no cards
        if not wallet:
            cards_to_check = DEFAULT_CARD_MULTIPLIERS
        elif not only_wallet:
            # combine wallet with market benchmarks
            pass

    best_card = None
    best_multiplier = 0.0

    for card, multipliers in cards_to_check.items():
        multiplier = multipliers.get(eff_category, multipliers.get("default", 1.0))
        if multiplier > best_multiplier:
            best_multiplier = multiplier
            best_card = card

    return {
        "status": "success",
        "recommended_card": best_card,
        "multiplier": best_multiplier,
        "category": eff_category,
        "merchant": merchant,
        "wallet_cards_count": len(wallet)
    }


def audit_wallet_rewards(user_id: str, days_back: int = 90) -> str:
    """
    Analyzes historical transactions to quantify earned cash back vs. optimal wallet swipes,
    revealing 'Lost Cash Back' left on the table.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("audit_wallet_rewards: forced isolation violation")

    wallet = get_user_wallet_cards(user_id)
    if not wallet:
        return "No credit card accounts found in your profile to audit rewards against."

    from datetime import datetime, timedelta
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT date, merchant, clean_merchant, amount, account_used, category
                FROM transactions
                WHERE user_id = ? AND date >= ? AND amount > 0
                  AND merchant NOT LIKE '%System Balance Sync%'
                ORDER BY date DESC
            """, (user_id, start_date))
            rows = c.fetchall()

        if not rows:
            return f"No purchases found in the last {days_back} days to audit."

        total_spent = 0.0
        actual_rewards = 0.0
        optimal_rewards = 0.0
        suboptimal_swipes = []

        for date, merch, clean_m, amt, acc, cat in rows:
            amount = float(amt)
            total_spent += amount
            m_name = clean_m or merch or "Unknown"
            eff_cat = infer_category(cat, m_name)

            # Find actual multiplier used
            actual_mult = 1.0
            acc_lower = (acc or "").lower()
            for w in wallet:
                if w["name"].lower() in acc_lower or acc_lower in w["name"].lower():
                    actual_mult = w["multipliers"].get(eff_cat, w["multipliers"].get("default", 1.0))
                    break

            # Find best possible multiplier in user's wallet
            best_wallet_card = None
            best_mult = 0.0
            for w in wallet:
                mult = w["multipliers"].get(eff_cat, w["multipliers"].get("default", 1.0))
                if mult > best_mult:
                    best_mult = mult
                    best_wallet_card = w["name"]

            earned = amount * (actual_mult / 100.0)
            optimal = amount * (best_mult / 100.0)
            actual_rewards += earned
            optimal_rewards += optimal

            diff = optimal - earned
            if diff > 0.25:
                suboptimal_swipes.append({
                    "date": date[:10],
                    "merchant": m_name,
                    "amount": amount,
                    "category": eff_cat,
                    "used_card": acc or "Other",
                    "best_card": best_wallet_card,
                    "used_mult": actual_mult,
                    "best_mult": best_mult,
                    "missed": diff
                })

        missed_total = optimal_rewards - actual_rewards
        report = [
            f"💳 CREDIT CARD REWARDS OPTIMIZATION AUDIT ({days_back} Days)",
            f"Analyzed {len(rows)} purchases (${total_spent:,.2f} total spend).",
            f"Active Wallet Cards: {', '.join([w['name'] for w in wallet])}",
            "-" * 45,
            f"Estimated Rewards Earned:    ${actual_rewards:,.2f}",
            f"Maximum Possible in Wallet:  ${optimal_rewards:,.2f}",
            f"💸 Missed Cash Back:         ${missed_total:,.2f}",
            "-" * 45
        ]

        if suboptimal_swipes:
            suboptimal_swipes.sort(key=lambda x: x["missed"], reverse=True)
            report.append("🔍 BIGGEST SUB-OPTIMAL SWIPES (Missed Rewards):")
            for i, s in enumerate(suboptimal_swipes[:5], 1):
                report.append(
                    f"{i}. {s['merchant']} (${s['amount']:.2f} on {s['date']}) | {s['category'].title()}\n"
                    f"   Used: {s['used_card']} ({s['used_mult']}%) ➔ Should have used: **{s['best_card']}** ({s['best_mult']}%)\n"
                    f"   ↳ Left ${s['missed']:.2f} on the table"
                )
            report.append("-" * 45)

        if missed_total > 10.0:
            report.append(f"💡 RECOMMENDATION: By consistently using the highest multiplier card for each merchant, you can reclaim ~${missed_total * (365/days_back):,.2f}/year in passive cash back.")
        else:
            report.append("✅ EXCELLENT DISCIPLINE: You are already maximizing nearly all card category bonuses!")

        return "\n".join(report)

    except Exception as e:
        return f"ERROR auditing wallet rewards: {str(e)}"
