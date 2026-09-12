"""
Comprehensive test suite for Delilah Financial OS government form tools:
- find_government_forms
- fill_pdf_form
- Schema invariants (NEW_50_TOOLS_SCHEMA == 50, ADVISOR_TOOLS_DISPATCH == 50)
- User profile data gathering & fuzzy matching
- Dual async/sync execution
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pymupdf

from src.services.advisor_tools import ADVISOR_TOOLS_DISPATCH, NEW_50_TOOLS_SCHEMA
from src.services.forms import (
    SYNONYM_MAP,
    async_fill_pdf_form,
    async_find_government_forms,
    fill_pdf_form,
    find_government_forms,
    get_user_financial_profile,
    get_workspace_dir,
    match_field_to_attribute,
    match_form_fields_to_profile,
)
from src.services.llm import BOT_TOOLS_SCHEMA, EXPECTED_TOOL_NAMES, SCHEMA_TOOL_NAMES

# ────────────────────────────────────────────────────────────
# 1. Schema & Invariant Tests
# ────────────────────────────────────────────────────────────


def test_schema_registration_and_invariants():
    """Verify tool registration in BOT_TOOLS_SCHEMA and 50-tools invariants."""
    # Invariant: advisor tools collections must remain locked at exactly 50
    assert len(NEW_50_TOOLS_SCHEMA) == 50
    assert len(ADVISOR_TOOLS_DISPATCH) == 50
    schema_advisor_names = {t["function"]["name"] for t in NEW_50_TOOLS_SCHEMA}
    assert schema_advisor_names == set(ADVISOR_TOOLS_DISPATCH.keys())

    # Verify new tools appear in EXPECTED_TOOL_NAMES
    assert "find_government_forms" in EXPECTED_TOOL_NAMES
    assert "fill_pdf_form" in EXPECTED_TOOL_NAMES

    # Verify SCHEMA_TOOL_NAMES matches EXPECTED_TOOL_NAMES exactly
    assert SCHEMA_TOOL_NAMES == EXPECTED_TOOL_NAMES

    # Verify tool schema parameter requirements
    schemas_by_name = {t["function"]["name"]: t["function"] for t in BOT_TOOLS_SCHEMA}

    assert "find_government_forms" in schemas_by_name
    fgf = schemas_by_name["find_government_forms"]
    assert "description" in fgf["parameters"]["properties"]
    assert "description" in fgf["parameters"]["required"]

    assert "fill_pdf_form" in schemas_by_name
    fpf = schemas_by_name["fill_pdf_form"]
    assert "pdf_url" in fpf["parameters"]["properties"]
    assert "pdf_url" in fpf["parameters"]["required"]
    assert "field_overrides" in fpf["parameters"]["properties"]


# ────────────────────────────────────────────────────────────
# 2. User Profile Extraction Tests
# ────────────────────────────────────────────────────────────


@pytest.fixture
def temp_finances_db(tmp_path):
    """Creates a populated temporary SQLite database for user profile testing."""
    db_file = tmp_path / "finances.db"
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()

    c.execute("""
        CREATE TABLE user_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            observed_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE plaid_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            plaid_account_id TEXT,
            name TEXT,
            official_name TEXT,
            mask TEXT,
            type TEXT,
            subtype TEXT,
            institution_name TEXT,
            currency TEXT,
            current_balance REAL,
            available_balance REAL,
            credit_limit REAL,
            last_synced_at TEXT,
            active INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE cash_inflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            type TEXT,
            source TEXT,
            gross_amount REAL,
            net_expected REAL,
            recurrence TEXT,
            status TEXT
        )
    """)

    # Populate test user
    uid = "test_user_alpha"
    demographics = [
        ("first_name", "Sarah"),
        ("last_name", "Connor"),
        ("address", "100 Cyberdyne Way"),
        ("city", "Los Angeles"),
        ("state", "CA"),
        ("zip_code", "90001"),
        ("ssn_last_4", "9988"),
        ("phone", "213-555-0144"),
        ("email", "sconnor@resistance.org"),
    ]
    for k, v in demographics:
        c.execute(
            "INSERT INTO user_info (user_id, key, value) VALUES (?, ?, ?)", (uid, k, v)
        )

    c.execute(
        """
        INSERT INTO plaid_accounts (user_id, name, mask, type, subtype, institution_name, active)
        VALUES (?, 'Total Checking', '4432', 'depository', 'checking', 'Chase', 1)
    """,
        (uid,),
    )

    c.execute(
        """
        INSERT INTO cash_inflows (user_id, type, source, gross_amount, net_expected, recurrence, status)
        VALUES (?, 'Paycheck', 'Cyberdyne Systems', 4000.0, 3100.0, 'biweekly', 'Active')
    """,
        (uid,),
    )

    conn.commit()
    conn.close()
    return str(db_file)


def test_get_user_financial_profile(temp_finances_db):
    """Verify profile extractor correctly gathers demographics, accounts, and income."""
    uid = "test_user_alpha"
    profile = get_user_financial_profile(uid, db_path=temp_finances_db)

    assert profile["first_name"] == "Sarah"
    assert profile["last_name"] == "Connor"
    assert profile["full_name"] == "Sarah Connor"
    assert profile["address"] == "100 Cyberdyne Way"
    assert profile["city"] == "Los Angeles"
    assert profile["state"] == "CA"
    assert profile["zip_code"] == "90001"
    assert profile["city_state_zip"] == "Los Angeles, CA 90001"
    assert "100 Cyberdyne Way" in profile["full_address"]
    assert profile["ssn_last_4"] == "9988"
    assert profile["ssn"] == "***-**-9988"
    assert profile["phone"] == "213-555-0144"
    assert profile["email"] == "sconnor@resistance.org"
    assert profile["account_number"] == "4432"
    assert profile["bank_name"] == "Chase"
    assert profile["employer"] == "Cyberdyne Systems"
    # 4000 * 26 = 104000
    assert profile["annual_income"] == "104000"


# ────────────────────────────────────────────────────────────
# 3. Fuzzy Field Matching Tests
# ────────────────────────────────────────────────────────────


def test_match_field_to_attribute():
    """Verify fuzzy matching assigns PDF widgets to canonical attributes."""
    test_cases = [
        ("Text1", "Enter Address", "Enter Address", "address"),
        ("Text6", "Enter Name", "Enter Name", "full_name"),
        ("Text7", "Enter Street Address", "", "address"),
        ("Text8", "Enter city, state, ZIP", "", "city_state_zip"),
        ("Text9", "Enter social security number", "", "ssn"),
        ("Text11", "Enter phone number", "", "phone"),
        (
            "topmostSubform[0].Page1[0].p1-t1[0]",
            "1a. Name shown on tax return.",
            "",
            "full_name",
        ),
        (
            "topmostSubform[0].Page1[0].p1-t2[0]",
            "1b. First social security number",
            "",
            "ssn",
        ),
        ("Applicant_First_Name", "", "", "first_name"),
        ("Applicant_Last_Name", "", "", "last_name"),
        ("AnnualGrossIncome", "", "", "annual_income"),
        ("EmployerName", "", "", "employer"),
        ("ContactEmail", "", "", "email"),
    ]
    for name, label, tooltip, expected_attr in test_cases:
        attr, score = match_field_to_attribute(name, label, tooltip)
        assert (
            attr == expected_attr
        ), f"Field '{name}' ({label}) mapped to '{attr}', expected '{expected_attr}'"
        assert score >= 0.7, f"Score {score} too low for {name}"


def test_match_form_fields_to_profile():
    """Verify form fields mapping merges profile data and manual field overrides."""
    fields = [
        {"name": "applicant_name", "label": "Enter Name", "type": "text"},
        {"name": "residence_address", "label": "Street Address", "type": "text"},
        {
            "name": "city_state_zip_field",
            "label": "Enter city, state, ZIP",
            "type": "text",
        },
        {"name": "ssn_field", "label": "Social Security Number", "type": "text"},
        {"name": "override_target", "label": "Custom Field", "type": "text"},
    ]
    profile = {
        "full_name": "Alice Smith",
        "address": "456 Oak Lane",
        "city_state_zip": "Austin, TX 78701",
        "ssn": "***-**-1234",
    }
    overrides = {
        "override_target": "Manual Custom Value",
        "applicant_name": "Alice M. Smith",  # Explicit field override
    }

    matched = match_form_fields_to_profile(fields, profile, field_overrides=overrides)

    assert matched["applicant_name"] == "Alice M. Smith"  # Override took precedence
    assert matched["residence_address"] == "456 Oak Lane"
    assert matched["city_state_zip_field"] == "Austin, TX 78701"
    assert matched["ssn_field"] == "***-**-1234"
    assert matched["override_target"] == "Manual Custom Value"


# ────────────────────────────────────────────────────────────
# 4. find_government_forms Tests
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_government_forms_mocked(monkeypatch):
    """Test find_government_forms with mocked SearXNG search results."""
    mock_search_result = {
        "results": [
            {
                "title": "Application for Homestead Exemption - Official GA Form",
                "url": "https://dor.georgia.gov/document/form/lgs-homestead/download",
                "snippet": "Homestead exemption application for resident homeowners.",
            },
            {
                "title": "IRS Form 4506 - Copy of Tax Return",
                "url": "https://www.irs.gov/pub/irs-pdf/f4506.pdf",
                "snippet": "Request a copy of your tax return.",
            },
        ]
    }

    async def mock_searxng(query, **kwargs):
        return mock_search_result

    async def mock_verify(url, client):
        return True, url

    monkeypatch.setattr("src.services.forms.search_searxng", mock_searxng)
    monkeypatch.setattr("src.services.forms._verify_pdf_url", mock_verify)

    # Test async execution
    res_async = await async_find_government_forms("Georgia homestead exemption")
    assert res_async["status"] == "success"
    assert (
        res_async["url"]
        == "https://dor.georgia.gov/document/form/lgs-homestead/download"
    )
    assert "Homestead" in res_async["form_name"]
    assert res_async["eligibility_purpose"]

    # Test sync execution via hybrid wrapper
    res_sync = find_government_forms("Georgia homestead exemption")
    assert res_sync["status"] == "success"
    assert (
        res_sync["url"]
        == "https://dor.georgia.gov/document/form/lgs-homestead/download"
    )


@pytest.mark.asyncio
async def test_find_government_forms_not_found(monkeypatch):
    """Test find_government_forms when no candidate is found."""

    async def mock_searxng_empty(query, **kwargs):
        return {"results": []}

    monkeypatch.setattr("src.services.forms.search_searxng", mock_searxng_empty)

    res = await async_find_government_forms("Nonexistent form query 99999")
    assert res["status"] == "not_found"
    assert res["url"] == ""


# ────────────────────────────────────────────────────────────
# 5. fill_pdf_form Tests (Standalone with Synthetic PDF)
# ────────────────────────────────────────────────────────────


@pytest.fixture
def synthetic_fillable_pdf(tmp_path) -> Path:
    """Generates a real fillable PDF file with AcroForm widgets for testing."""
    pdf_path = tmp_path / "sample_fillable.pdf"
    doc = pymupdf.open()
    page = doc.new_page()

    # Widget 1: Name
    w1 = pymupdf.Widget()
    w1.rect = pymupdf.Rect(50, 50, 300, 75)
    w1.field_name = "Text6"
    w1.field_label = "Enter Name"
    w1.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    page.add_widget(w1)

    # Widget 2: Address
    w2 = pymupdf.Widget()
    w2.rect = pymupdf.Rect(50, 85, 300, 110)
    w2.field_name = "Text1"
    w2.field_label = "Enter Address"
    w2.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    page.add_widget(w2)

    # Widget 3: Phone
    w3 = pymupdf.Widget()
    w3.rect = pymupdf.Rect(50, 120, 300, 145)
    w3.field_name = "Text11"
    w3.field_label = "Enter phone number"
    w3.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    page.add_widget(w3)

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def test_fill_pdf_form_standalone(synthetic_fillable_pdf, tmp_path, monkeypatch):
    """
    Verifies fill_pdf_form end-to-end:
    - Ingests fillable PDF
    - Matches user profile
    - Fills widgets
    - Saves output PDF to workspace
    - Asserts resulting file is non-zero byte and contains filled data
    """

    # Force PyMuPDF fallback by simulating Stirling unavailable
    async def mock_no_stirling(client):
        return None

    monkeypatch.setattr("src.services.forms._get_active_stirling_url", mock_no_stirling)

    # Redirect workspace to temp directory
    test_workspace = tmp_path / "workspace" / "user_test_99"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.services.forms.get_workspace_dir", lambda uid: test_workspace
    )

    # Mock user profile
    monkeypatch.setattr(
        "src.services.forms.get_user_financial_profile",
        lambda uid: {
            "full_name": "Bruce Wayne",
            "address": "1007 Mountain Drive",
            "phone": "212-555-0199",
        },
    )

    # Execute sync call
    result = fill_pdf_form(
        str(synthetic_fillable_pdf),
        field_overrides={"Text11": "555-BAT-CALL"},
        user_id="user_test_99",
    )

    assert result["status"] == "success"
    assert "file_path" in result
    assert "download_url" in result
    assert "fields_filled" in result

    # Check generated file
    out_file = Path(result["file_path"])
    assert out_file.exists()
    assert out_file.stat().st_size > 0

    # Inspect filled PDF widgets to confirm values were written
    doc = pymupdf.open(str(out_file))
    widgets = {w.field_name: w.field_value for w in doc[0].widgets()}

    assert widgets.get("Text6") == "Bruce Wayne"
    assert widgets.get("Text1") == "1007 Mountain Drive"
    assert widgets.get("Text11") == "555-BAT-CALL"  # Override applied
    doc.close()


@pytest.mark.asyncio
async def test_async_fill_pdf_form(synthetic_fillable_pdf, tmp_path, monkeypatch):
    """Test async execution of fill_pdf_form."""
    test_workspace = tmp_path / "workspace" / "user_async"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.services.forms.get_workspace_dir", lambda uid: test_workspace
    )
    monkeypatch.setattr(
        "src.services.forms.get_user_financial_profile",
        lambda uid: {"full_name": "Clark Kent", "address": "344 Clinton St"},
    )

    result = await async_fill_pdf_form(
        str(synthetic_fillable_pdf),
        user_id="user_async",
    )
    assert result["status"] == "success"
    assert Path(result["file_path"]).stat().st_size > 0


# ────────────────────────────────────────────────────────────
# 6. Live Functional Acceptance Checks
# ────────────────────────────────────────────────────────────


def test_live_find_government_forms_georgia():
    """
    Acceptance Criteria check:
    find_government_forms('Georgia homestead exemption') returns a dict
    containing a non-empty url field pointing to a PDF.
    """
    res = find_government_forms("Georgia homestead exemption")
    assert isinstance(res, dict)
    assert res.get("url"), "Expected non-empty URL"
    assert (
        res["url"].lower().endswith(".pdf")
        or "/download" in res["url"].lower()
        or ".pdf?" in res["url"].lower()
    ), f"Expected PDF download link, got {res['url']}"
    assert res.get("form_name")
    assert res.get("status") == "success"


def test_live_fill_pdf_form_irs_4506(tmp_path, monkeypatch):
    """
    Acceptance Criteria check:
    fill_pdf_form called with a publicly available fillable PDF URL (e.g. IRS Form 4506)
    completes without exception, returns a file path to a non-zero-byte PDF,
    and the filled PDF contains at least one pre-populated field value.
    """
    test_workspace = tmp_path / "workspace" / "live_user"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.services.forms.get_workspace_dir", lambda uid: test_workspace
    )

    monkeypatch.setattr(
        "src.services.forms.get_user_financial_profile",
        lambda uid: {
            "full_name": "Jordan Belfort",
            "address": "1 Wall Street",
            "ssn_last_4": "4321",
            "ssn": "***-**-4321",
        },
    )

    url = "https://www.irs.gov/pub/irs-pdf/f4506.pdf"
    res = fill_pdf_form(
        url,
        field_overrides={"topmostSubform[0].Page1[0].p1-t1[0]": "Jordan Belfort"},
        user_id="live_user",
    )

    assert res["status"] == "success"
    out_path = Path(res["file_path"])
    assert out_path.exists()
    assert out_path.stat().st_size > 0

    # Verify filled value in PDF
    doc = pymupdf.open(str(out_path))
    widgets = {w.field_name: w.field_value for w in doc[0].widgets()}
    doc.close()

    assert "topmostSubform[0].Page1[0].p1-t1[0]" in widgets
    assert widgets["topmostSubform[0].Page1[0].p1-t1[0]"] == "Jordan Belfort"


@pytest.mark.asyncio
async def test_forms_dispatch_branches(synthetic_fillable_pdf, tmp_path, monkeypatch):
    """Verify both find_government_forms and fill_pdf_form dispatch execution."""
    test_workspace = tmp_path / "workspace" / "dispatch_user"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.services.forms.get_workspace_dir", lambda uid: test_workspace
    )

    # 1. Dispatch find_government_forms
    async def mock_searxng(query, **kwargs):
        return {
            "results": [
                {
                    "title": "Mock Exemption Form",
                    "url": "https://example.gov/form.pdf",
                    "snippet": "Official exemption application.",
                }
            ]
        }

    async def mock_verify(url, client):
        return True, url

    monkeypatch.setattr("src.services.forms.search_searxng", mock_searxng)
    monkeypatch.setattr("src.services.forms._verify_pdf_url", mock_verify)

    from src.services.forms import find_government_forms, fill_pdf_form

    res1 = await find_government_forms("Homestead exemption")
    db_result1 = (
        json.dumps(res1, indent=2) if isinstance(res1, (dict, list)) else str(res1)
    )
    parsed1 = json.loads(db_result1)
    assert parsed1["url"] == "https://example.gov/form.pdf"
    assert parsed1["status"] == "success"

    # 2. Dispatch fill_pdf_form
    res2 = await fill_pdf_form(
        str(synthetic_fillable_pdf),
        field_overrides={"Text6": "Dispatch User"},
        user_id="dispatch_user",
    )
    db_result2 = (
        json.dumps(res2, indent=2) if isinstance(res2, (dict, list)) else str(res2)
    )
    parsed2 = json.loads(db_result2)
    assert parsed2["status"] == "success"
    assert Path(parsed2["file_path"]).exists()


# ────────────────────────────────────────────────────────────
# 7. Remediation Tests: Challenger 1 & Challenger 2 Defect Fixes
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hybrid_dict_async_concurrency():
    """
    Verifies HybridDict async concurrency primitives:
    - asyncio.gather(*[find_government_forms(q) for q in ...])
    - asyncio.wait
    - asyncio.as_completed
    Ensures __hash__ = object.__hash__ prevents TypeError: unhashable type.
    """
    # 1. asyncio.gather with multiple forms
    q_list = ["IRS Form 4506", "Georgia homestead exemption", "IRS Form 1040 Schedule C"]
    batch = await asyncio.gather(*[find_government_forms(q) for q in q_list])
    assert len(batch) == 3
    for item in batch:
        assert isinstance(item, dict)
        assert item.get("status") == "success"
        assert item.get("url")

    # 2. asyncio.wait
    form_item = find_government_forms("IRS Form 4506")
    done, pending = await asyncio.wait([form_item])
    assert len(done) == 1
    assert len(pending) == 0
    res = list(done)[0].result()
    assert res.get("status") == "success"

    # 3. asyncio.as_completed
    items = [find_government_forms("IRS Form 4506"), find_government_forms("Georgia homestead exemption")]
    collected = []
    for fut in asyncio.as_completed(items):
        r = await fut
        collected.append(r)
    assert len(collected) == 2
    assert all(r.get("status") == "success" for r in collected)


def test_hybrid_dict_copy_and_equality():
    """Verifies HybridDict copy(), __copy__(), equality, and serialization."""
    import copy

    res = find_government_forms("IRS Form 4506")
    # Must be hashable
    assert hash(res) is not None

    # copy()
    c1 = res.copy()
    assert isinstance(c1, dict)
    assert c1.get("status") == "success"

    # __copy__()
    c2 = copy.copy(res)
    assert isinstance(c2, dict)
    assert c2.get("status") == "success"

    # __eq__ and __ne__
    assert res == c1
    assert not (res != c1)
    assert res != {}

    # bool()
    assert bool(res) is True

    # json.dumps serialization of copy
    dumped = json.dumps(res.copy())
    loaded = json.loads(dumped)
    assert loaded.get("status") == "success"
    assert loaded.get("url")


@pytest.mark.asyncio
async def test_dead_link_rejection():
    """Verifies _verify_pdf_url rejects HTTP 404 dead links even if ending with .pdf."""
    import httpx
    from src.services.forms import _verify_pdf_url

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        # Nonexistent dead link ending in .pdf
        is_valid, _ = await _verify_pdf_url(
            "https://www.irs.gov/nonexistent_dead_link_404.pdf", client
        )
        assert is_valid is False

        # Nonexistent 404 on httpbin
        is_valid_404, _ = await _verify_pdf_url(
            "https://httpbin.org/status/404.pdf", client
        )
        assert is_valid_404 is False


def test_workspace_dir_path_traversal_hardened():
    """Verifies get_workspace_dir sanitizes traversal sequences like ../ and cannot escape workspace."""
    p1 = get_workspace_dir("../../etc/passwd")
    assert not str(p1).startswith("/etc")
    assert ".." not in str(p1)
    assert "passwd" in str(p1)

    p2 = get_workspace_dir("..\\..\\windows\\system32")
    assert ".." not in str(p2)

    p3 = get_workspace_dir("")
    assert p3.name == "1"


def test_official_government_domain_priority():
    """Verifies official .gov domains are prioritized over commercial law firms / SEO aggregators."""
    from src.services.forms import _domain_official_priority

    gov_score = _domain_official_priority("https://dor.georgia.gov/document/form/download")
    irs_score = _domain_official_priority("https://www.irs.gov/pub/irs-pdf/f4506.pdf")
    law_score = _domain_official_priority("https://thomasandbrownlaw.com/wp-content/uploads/form.pdf")
    cpa_score = _domain_official_priority("https://weneedacpa.com/2026/04/irs-form-4506")

    assert gov_score == 3
    assert irs_score == 3
    assert law_score == 0
    assert cpa_score == 0
    assert gov_score > law_score


def test_valid_domain_queries_return_official_pdfs():
    """
    Verifies valid domain queries return official PDF form URLs:
    - IRS Form 4506
    - IRS Form 1040 Schedule C
    - Georgia homestead exemption
    - California Solar Property Tax Exclusion
    - HVAC utility rebate
    """
    domain_queries = [
        ("IRS Form 4506", "irs.gov"),
        ("IRS Form 1040 Schedule C", "irs.gov"),
        ("Georgia homestead exemption", "georgia.gov"),
        ("California Solar Property Tax Exclusion", "ca.gov"),
        ("HVAC utility rebate", None),
    ]
    for q, domain_substr in domain_queries:
        res = find_government_forms(q)
        assert res.get("status") == "success", f"Query '{q}' failed with status {res.get('status')}"
        assert res.get("url"), f"Query '{q}' returned empty URL"
        if domain_substr:
            assert domain_substr in res["url"], f"Query '{q}' URL '{res['url']}' does not contain expected domain '{domain_substr}'"
        assert res.get("form_name")
        assert res.get("eligibility_purpose")

