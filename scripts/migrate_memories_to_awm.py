"""
Comprehensive Migration Script: Populate Delilah Active World Model from delilah_memories

Extracts all semantic entities, bi-temporal epistemic claims, and system policies from
the 72 raw delilah_memories records and asserts them into:
- kg_entities
- kg_claims
- kg_dossiers
- kg_search_fts
"""

import sqlite3
import json
from src.core.state import DB_PATH
from src.services.world_model import upsert_entity, assert_claim, audit_world_model_health

def run_comprehensive_migration():
    print("Beginning full migration of all 72 memories into Delilah Active World Model...")

    # 1. CANONICAL ENTITIES
    entities = [
        ("person:allen", "person", "Allen Zhao", ["Allen", "incoming", "me", "user"], {
            "name": "Allen Zhao",
            "residence": "Sunset Park, Brooklyn, NY",
            "rent": 0.00,
            "status": "Senior Student",
            "school": "CUNY College of Staten Island",
            "major": "Computer Science",
            "gpa": 3.18,
            "target_graduation": "Spring 2027",
            "career_target": "Software Engineer (6-figure target)",
            "primary_spending": ["food", "transportation", "fragrance"],
            "fragrance_collection_bottles": 62,
            "fragrance_collection_val": 8000.00,
            "top_debt_priority": "Credit Card Payoff ($4,671.45)",
            "emergency_fund_target": 1000.00,
            "emergency_fund_timing": "After credit card payoff"
        }),
        ("person:rui_lin", "person", "Rui Lin", ["Mom", "Rui Lin"], {
            "relationship": "Mother",
            "financial_support": "Covers health insurance and housing"
        }),
        ("org:cuny_csi", "institution", "CUNY College of Staten Island", ["CSI", "CUNY CSI", "College of Staten Island"], {
            "tuition_rate_per_term": 3610.22,
            "system": "City University of New York"
        }),
        ("org:cuny_hpc", "institution", "CUNY High Performance Computing Center", ["HPC Center", "CUNY HPC", "HPC"], {
            "employment_type": "Federal Work-Study (FWS)",
            "hourly_rate": 17.00,
            "max_weekly_hours": 20,
            "semester_earnings_cap": 2500.00,
            "start_date": "2026-08-28"
        }),
        ("org:mta", "institution", "Metropolitan Transportation Authority", ["MTA", "Subway", "Bus"], {
            "daily_commute_cost": 6.00,
            "class_days_per_week": 4,
            "fair_fares_status": "Application submitted 2026-09-06 (50% discount if approved)"
        }),
        ("liability:discover_it", "account", "Discover it Credit Card", ["Discover", "Discover Card"], {
            "promo_apr": 0.0,
            "promo_expiry": "2026-12-15",
            "priority": "Primary Debt Target"
        }),
        ("liability:amex_bce", "account", "American Express Blue Cash Everyday", ["Amex", "Amex BCE", "Blue Cash Everyday"], {
            "apr": 26.50,
            "priority": "High Interest Payoff"
        }),
        ("liability:apple_card", "account", "Apple Card (Goldman Sachs)", ["Apple Card", "Apple GS"], {
            "priority": "Revolving Credit"
        }),
        ("contract:apple_upgrade", "contract", "Apple Upgrade Program Lease (Klarna)", ["Apple Upgrade", "MacBook Lease", "Klarna Lease"], {
            "provider": "Klarna",
            "device_target": "MacBook Pro 14 M5 Pro (48GB/2TB)",
            "terms": "36-mo @ $77.69/mo or 24-mo @ $108.10/mo",
            "cash_buyout_target": 4082.01,
            "structure": "Lease (must return/trade device at term end)",
            "status": "Delayed to Spring 2027"
        }),
        ("policy:financial_protocols", "policy", "Delilah System Rules & Financial Protocols", ["Protocols", "Rules", "Directives"], {
            "sign_convention": "NEGATIVE is INCOME (inflow), POSITIVE is EXPENSE (outflow)",
            "date_integrity": "NEVER invent a date. Dates must come directly from verified documents.",
            "fragrance_freeze": "NO PERFUMES RULE active until January 1, 2027.",
            "fragrance_slowdown": "Target max 1 bottle per 3 months post-freeze.",
            "tax_adjustment": "Always apply NYC sales tax (8.875%) to purchase estimates and bucket targets.",
            "budget_style": "Strict",
            "check_in_frequency": "Proactive and frequent"
        }),
        ("vendor:azmere_usa", "vendor", "AZMERE USA", ["Azmere", "AZMERE"], {
            "segment": "Niche/High-End Fragrance Wholesaler/Discounter",
            "pricing_example": "Amouage Oud Stallion 50ml for $190"
        })
    ]

    for ent_id, ent_type, canon_name, aliases, attrs in entities:
        upsert_entity(
            entity_id=ent_id,
            entity_type=ent_type,
            canonical_name=canon_name,
            aliases=aliases,
            attributes=attrs
        )
    print(f" [AWM] Upserted {len(entities)} canonical entities.")

    # 2. EPISTEMIC BI-TEMPORAL CLAIMS
    claims = [
        # Academic & Career
        ("person:allen", "enrolled_at", "org:cuny_csi", None, "USER_STATED", 5, "2026-08-25 00:00:00", []),
        ("person:allen", "target_graduation", None, "Spring 2027", "USER_STATED", 5, "2026-09-02 00:00:00", []),
        ("person:allen", "major", None, "Computer Science", "USER_STATED", 5, "2026-08-25 00:00:00", []),
        ("person:allen", "gpa", None, "3.18", "USER_STATED", 4, "2026-09-02 00:00:00", []),
        ("person:allen", "career_goal", None, "Software Engineer (6-figure salary)", "USER_STATED", 5, "2026-09-02 00:00:00", []),
        ("person:allen", "job_search_status", None, "Applying now for full-time SWE roles", "USER_STATED", 5, "2026-09-02 00:00:00", []),

        # Employment & Income
        ("person:allen", "employed_by", "org:cuny_hpc", None, "DIRECT_OBSERVATION", 5, "2026-08-28 00:00:00", ["cuny_fws_payroll_calendar_2026_27.pdf"]),
        ("org:cuny_hpc", "hourly_wage", None, "17.00", "DOCUMENT", 5, "2026-08-28 00:00:00", ["cuny_fws_payroll_calendar_2026_27.pdf"]),
        ("org:cuny_hpc", "semester_cap", None, "2500.00", "DOCUMENT", 5, "2026-08-28 00:00:00", ["cuny_fws_payroll_calendar_2026_27.pdf"]),
        ("org:cuny_hpc", "max_weekly_hours", None, "20", "DOCUMENT", 5, "2026-08-28 00:00:00", ["cuny_fws_payroll_calendar_2026_27.pdf"]),
        ("org:cuny_hpc", "cap_exhaustion_date", None, "2026-10-29", "CALCULATION", 5, "2026-09-02 00:00:00", ["fws_timesheet_calculator_v1"]),
        ("person:allen", "has_side_income", None, "false", "USER_STATED", 5, "2026-09-02 00:00:00", []),
        ("person:allen", "rent_obligation", None, "0.00", "USER_STATED", 5, "2026-09-02 00:00:00", []),

        # Debts & Liabilities
        ("person:allen", "owes_debt_to", "liability:discover_it", None, "DIRECT_OBSERVATION", 5, "2026-09-01 00:00:00", []),
        ("person:allen", "owes_debt_to", "liability:amex_bce", None, "DIRECT_OBSERVATION", 5, "2026-09-01 00:00:00", []),
        ("person:allen", "owes_debt_to", "liability:apple_card", None, "DIRECT_OBSERVATION", 5, "2026-09-01 00:00:00", []),
        ("liability:amex_bce", "interest_apr", None, "0.2650", "USER_STATED", 5, "2026-09-06 00:00:00", []),
        ("liability:discover_it", "promo_apr", None, "0.0000", "USER_STATED", 5, "2026-09-01 00:00:00", []),
        ("liability:discover_it", "promo_expiry", None, "2026-12-15", "USER_STATED", 5, "2026-09-01 00:00:00", []),

        # Goals & Strategy
        ("person:allen", "primary_financial_goal", None, "Pay off credit card debt first ($4,671.45)", "USER_STATED", 5, "2026-09-02 00:00:00", []),
        ("person:allen", "secondary_financial_goal", None, "Emergency fund $1,000 (after debt payoff)", "USER_STATED", 5, "2026-09-02 00:00:00", []),
        ("contract:apple_upgrade", "purchase_timing", None, "Delayed to Spring 2027", "USER_STATED", 5, "2026-09-06 00:00:00", []),

        # Lifestyle & Policies
        ("policy:financial_protocols", "sign_convention", None, "NEGATIVE=INCOME, POSITIVE=EXPENSE", "DIRECT_OBSERVATION", 5, "2026-08-20 00:00:00", []),
        ("policy:financial_protocols", "date_integrity", None, "Never invent dates; strict document provenance", "DIRECT_OBSERVATION", 5, "2026-09-02 00:00:00", []),
        ("policy:financial_protocols", "fragrance_freeze_active", None, "true", "USER_STATED", 5, "2026-09-06 00:00:00", []),
        ("policy:financial_protocols", "fragrance_freeze_end_date", None, "2027-01-01", "USER_STATED", 5, "2026-09-06 00:00:00", []),
        ("policy:financial_protocols", "sales_tax_rate_nyc", None, "0.08875", "DIRECT_OBSERVATION", 5, "2026-09-04 00:00:00", []),
        ("person:allen", "daily_commute_cost", None, "6.00", "USER_STATED", 4, "2026-09-02 00:00:00", []),
        ("person:allen", "daily_food_cost", None, "10.00", "USER_STATED", 4, "2026-09-02 00:00:00", [])
    ]

    for subj, pred, obj, val, prov, auth, valid_from, refs in claims:
        assert_claim(
            subject_id=subj,
            predicate=pred,
            object_id=obj,
            scalar_value=val,
            provenance_type=prov,
            source_authority=auth,
            valid_from=valid_from,
            evidence_refs=refs
        )
    print(f" [AWM] Asserted {len(claims)} epistemic claims.")

    # 3. DOSSIERS
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        dossiers = [
            (
                "cuny_aid_disbursement_2026",
                "org:cuny_csi",
                "CSI Fall 2026 Financial Aid & Disbursement Schedule",
                """# CUNY CSI Fall 2026 Financial Aid Structure
- SEEK Stipends (Transportation $595 + Edu $750 = $1,345): Deposit DIRECTLY to bank. Currently pending/overdue as of 09/11.
- SEEK Fees ($279.60, 09/07), Initial TAP ($2,782.50, 09/21), TAP Waiver ($621.30, 09/21), CUSTA ($50.00, 09/28) go toward TUITION FIRST ($3,610.22).
- Overages refund SAME DAY tuition is fully covered.
  * 09/21: $73.18 refund
  * 09/28: $50.00 refund
- Pell Fall #2 balance ($2,773.12) processed 09/28, funds released Friday 10/02/26.
- FWS: $2,500 semester cap ($17/hr, max 20 hrs/wk).
  * PP8: Sep 17 ($460)
  * PP9: Oct 1 ($680)
  * PP10: Oct 15 ($680)
  * PP11: Oct 29 ($680, FINAL - cap hit exactly). No payments in Nov/Dec.""",
                "cuny,fws,aid,tuition,pell,seek"
            ),
            (
                "delilah_system_protocols",
                "policy:financial_protocols",
                "Delilah Financial OS Core Directives & Execution Protocols",
                """# Delilah Core Financial Protocols
1. Sign Convention: NEGATIVE amounts = INCOME (inflow), POSITIVE amounts = EXPENSES (outflow).
2. Date Integrity: NEVER invent a date. Every financial projection date must match a verified document.
3. Fragrance Freeze: Strict NO PERFUMES rule until January 1, 2027. Afterwards, limit purchases to 1 bottle per 3 months.
4. Tax Rule: Always apply 8.875% NYC sales tax to purchase estimates and bucket targets.
5. Debt-First Strategy: All surplus cash flows into credit card payoff ($4,671.45 total) before funding emergency fund or buying hardware.
6. Push-After-Reminder: After every reminder cycle fires, send a push alert and re-schedule the forward-passing iteration.""",
                "protocols,rules,directives,tax,fragrance"
            ),
            (
                "apple_macbook_upgrade_evaluation",
                "contract:apple_upgrade",
                "Apple Upgrade Program Lease & MacBook Evaluation",
                """# MacBook Pro 14 M5 Pro (48GB / 2TB) Evaluation
- Hardware: Space Black, 15-core CPU / 16-core GPU, nano-texture display ($4,082.01 retail target).
- Apple Upgrade Terms (Klarna Lease): 36-mo @ $77.69/mo or 24-mo @ $108.10/mo + tax.
- Crucial Term: This is a LEASE, not an ownership program. Device must be returned at term end.
- Status: Purchase DELAYED to Spring 2027 upon graduation refund.""",
                "apple,macbook,lease,klarna,hardware"
            )
        ]

        for doc_id, prim_id, title, content, tags in dossiers:
            c.execute("""
                INSERT INTO kg_dossiers (doc_id, primary_entity_id, title, content, tags)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET content = excluded.content, title = excluded.title, tags = excluded.tags
            """, (doc_id, prim_id, title, content, tags))
            
            c.execute("DELETE FROM kg_search_fts WHERE target_id = ?", (doc_id,))
            c.execute("""
                INSERT INTO kg_search_fts (target_id, target_type, title, content, tags)
                VALUES (?, 'dossier', ?, ?, ?)
            """, (doc_id, title, content, tags))

        conn.commit()
    print(" [AWM] Seeded rich dossiers and FTS search indices.")

    # 4. AUDIT
    health = audit_world_model_health()
    print("\n=== COGNITIVE HEALTH AUDIT ===")
    print(f"Status: {health['status']}")
    print(f"Total Entities: {health['entity_count']}")
    print(f"Total Active Claims: {health['active_claims_count']}")
    print(f"Contradictions: {health['unresolved_contradictions_count']}")
    print(f"Due Predictions: {health['due_predictions_count']}")
    print(f"Audited At: {health['audited_at']}")

if __name__ == "__main__":
    run_comprehensive_migration()
