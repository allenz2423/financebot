"""
Migration Script: Populate Delilah Active World Model from delilah_memories

Extracts high-value, structured facts from delilah_memories and seeds:
- kg_entities (person:allen, org:cuny_csi, org:cuny_hpc, liability:discover, contract:apple_upgrade)
- kg_claims (bi-temporal assertions with explicit source authority and provenance)
- kg_dossiers (detailed documentation on CUNY aid and Apple upgrade terms)
"""

import sqlite3
import json
from src.core.state import DB_PATH
from src.services.world_model import upsert_entity, assert_claim

def run_migration():
    print("Beginning migration of core facts to Delilah Active World Model...")
    
    # 1. ENTITIES
    upsert_entity(
        entity_id="person:allen",
        entity_type="person",
        canonical_name="Allen Zhao",
        aliases=["Allen", "incoming", "me", "user"],
        attributes={
            "status": "Senior Student",
            "school": "CUNY College of Staten Island",
            "major": "Computer Science",
            "gpa": 3.18,
            "residence": "Sunset Park, Brooklyn, NY",
            "rent": 0.00,
            "target_graduation": "Spring 2027",
            "fragrance_collection_bottles": 62
        }
    )
    print(" [AWM] Created entity: person:allen")

    upsert_entity(
        entity_id="org:cuny_csi",
        entity_type="institution",
        canonical_name="CUNY College of Staten Island",
        aliases=["CSI", "CUNY CSI", "College of Staten Island"],
        attributes={
            "tuition_rate_per_term": 3610.22,
            "system": "City University of New York"
        }
    )
    print(" [AWM] Created entity: org:cuny_csi")

    upsert_entity(
        entity_id="org:cuny_hpc",
        entity_type="institution",
        canonical_name="CUNY High Performance Computing Center",
        aliases=["HPC Center", "CUNY HPC", "HPC"],
        attributes={
            "employment_type": "Federal Work-Study (FWS)",
            "hourly_rate": 17.00,
            "max_weekly_hours": 20,
            "semester_earnings_cap": 2500.00,
            "start_date": "2026-08-28"
        }
    )
    print(" [AWM] Created entity: org:cuny_hpc")

    upsert_entity(
        entity_id="liability:discover_it",
        entity_type="account",
        canonical_name="Discover it Credit Card",
        aliases=["Discover", "Discover Card"],
        attributes={
            "promo_apr": 0.0,
            "promo_expiry": "2026-12-15",
            "priority": "Primary Debt Target"
        }
    )
    print(" [AWM] Created entity: liability:discover_it")

    upsert_entity(
        entity_id="contract:apple_upgrade",
        entity_type="contract",
        canonical_name="Apple Upgrade Program Lease (Klarna)",
        aliases=["Apple Upgrade", "MacBook Lease", "Klarna Lease"],
        attributes={
            "provider": "Klarna",
            "device_target": "MacBook Pro 14 M5 Pro (48GB/2TB)",
            "terms": "36-mo @ $77.69/mo or 24-mo @ $108.10/mo",
            "cash_buyout_target": 4082.01,
            "structure": "Lease (must return/trade device at term end)"
        }
    )
    print(" [AWM] Created entity: contract:apple_upgrade")

    # 2. BI-TEMPORAL CLAIMS
    # Allen's employment & FWS rules
    assert_claim(
        subject_id="person:allen",
        predicate="employed_by",
        object_id="org:cuny_hpc",
        provenance_type="DIRECT_OBSERVATION",
        source_authority=5,
        valid_from="2026-08-28 00:00:00",
        evidence_refs=["cuny_fws_payroll_calendar_2026_27.pdf"]
    )

    assert_claim(
        subject_id="org:cuny_hpc",
        predicate="hourly_wage",
        scalar_value="17.00",
        provenance_type="DOCUMENT",
        source_authority=4,
        valid_from="2026-08-28 00:00:00",
        evidence_refs=["cuny_fws_payroll_calendar_2026_27.pdf"]
    )

    assert_claim(
        subject_id="org:cuny_hpc",
        predicate="semester_cap",
        scalar_value="2500.00",
        provenance_type="DOCUMENT",
        source_authority=4,
        valid_from="2026-08-28 00:00:00",
        evidence_refs=["cuny_fws_payroll_calendar_2026_27.pdf"]
    )

    assert_claim(
        subject_id="org:cuny_hpc",
        predicate="cap_exhaustion_date",
        scalar_value="2026-10-29",
        provenance_type="CALCULATION",
        source_authority=4,
        valid_from="2026-09-02 00:00:00",
        evidence_refs=["fws_timesheet_calculator_v1"]
    )

    # Allen's academic enrollment
    assert_claim(
        subject_id="person:allen",
        predicate="enrolled_at",
        object_id="org:cuny_csi",
        provenance_type="USER_STATED",
        source_authority=4,
        valid_from="2026-08-25 00:00:00"
    )

    assert_claim(
        subject_id="person:allen",
        predicate="target_graduation",
        scalar_value="Spring 2027",
        provenance_type="USER_STATED",
        source_authority=4,
        valid_from="2026-09-02 00:00:00"
    )

    # Debt and liabilities
    assert_claim(
        subject_id="person:allen",
        predicate="owes_debt_to",
        object_id="liability:discover_it",
        provenance_type="DIRECT_OBSERVATION",
        source_authority=5,
        valid_from="2026-09-01 00:00:00"
    )

    # Apple lease evaluation
    assert_claim(
        subject_id="person:allen",
        predicate="evaluating_contract",
        object_id="contract:apple_upgrade",
        provenance_type="USER_STATED",
        source_authority=3,
        valid_from="2026-09-04 00:00:00"
    )

    # 3. DOSSIERS
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO kg_dossiers (doc_id, primary_entity_id, title, content, tags)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET content = excluded.content
        """, (
            "cuny_aid_disbursement_2026",
            "org:cuny_csi",
            "CSI Fall 2026 Financial Aid & Disbursement Schedule",
            """# CUNY CSI Fall 2026 Financial Aid Structure
- SEEK Stipends (Transportation $595 + Edu $750 = $1,345): Deposit DIRECTLY to bank.
- SEEK Fees ($279.60, 09/07), Initial TAP ($2,782.50, 09/21), TAP Waiver ($621.30, 09/21), CUSTA ($50.00, 09/28) go toward TUITION FIRST ($3,610.22).
- Overages refund SAME DAY tuition is fully covered.
- Pell Fall #2 balance ($2,773.12) processed 09/28, funds released Friday 10/02/26.
- FWS: $2,500 semester cap. PP8 Sep 17 ($460), PP9 Oct 1 ($680), PP10 Oct 15 ($680), PP11 Oct 29 ($680, FINAL - cap hit).""",
            "cuny,fws,aid,tuition,pell"
        ))
        
        # Index dossier into FTS
        c.execute("DELETE FROM kg_search_fts WHERE target_id = 'cuny_aid_disbursement_2026'")
        c.execute("""
            INSERT INTO kg_search_fts (target_id, target_type, title, content, tags)
            VALUES ('cuny_aid_disbursement_2026', 'dossier', 
                    'CSI Fall 2026 Financial Aid & Disbursement Schedule', 
                    'SEEK TAP Pell FWS tuition refund disbursement cap', 
                    'cuny fws aid tuition')
        """)
        conn.commit()

    print(" [AWM] Successfully migrated core facts and dossiers into Active World Model.")

if __name__ == "__main__":
    run_migration()
