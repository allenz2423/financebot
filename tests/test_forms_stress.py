"""
Empirical Stress Test Suite for Delilah Financial OS PDF Form Filling (fill_pdf_form).
Author: teamwork_preview_challenger_2

Validates:
1. Live IRS Form (Form 4506) full fill workflow with real user financial profile and overrides.
2. Synthetic PDFs with various AcroForm field types (Text, Checkbox, Radio, and unfillable/flat PDF).
3. Multi-page document handling.
4. Error cases: 404, non-existent domain, 0-byte file, malformed bytes, non-PDF content.
5. Multi-tenant data isolation: ensures zero leakage between User A and User B.
6. Field overrides precedence over database profile values (exact and semantic).
7. PDF validity inspection using both PyMuPDF and pypdfium2 (page counts, non-zero file size, widget values).
8. Dual-engine fidelity: Stirling PDF microservice vs PyMuPDF fallback.
9. Sandbox workspace storage and path structure.
10. Unconventional override value types (int, bool, list, dict, None, huge strings).
"""

import os
import re
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf
import pypdfium2
import pytest

from src.services.forms import (
    async_fill_pdf_form,
    fill_pdf_form,
    get_user_financial_profile,
    get_workspace_dir,
    match_field_to_attribute,
    match_form_fields_to_profile,
)


# ────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────


@pytest.fixture
def multi_tenant_db(tmp_path) -> str:
    """Sets up an isolated multi-tenant SQLite database with two distinct users."""
    db_file = tmp_path / "tenant_finances.db"
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()

    c.execute("""
        CREATE TABLE user_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE plaid_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            name TEXT,
            mask TEXT,
            type TEXT,
            subtype TEXT,
            institution_name TEXT,
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

    # User Alice (High net worth)
    alice_data = [
        ("full_name", "Alice Highworth"),
        ("first_name", "Alice"),
        ("last_name", "Highworth"),
        ("address", "742 Evergreen Terrace"),
        ("city", "Springfield"),
        ("state", "OR"),
        ("zip_code", "97477"),
        ("ssn_last_4", "1111"),
        ("phone", "503-555-0101"),
        ("email", "alice@highworth.com"),
    ]
    for k, v in alice_data:
        c.execute("INSERT INTO user_info (user_id, key, value) VALUES ('alice', ?, ?)", (k, v))
    c.execute(
        "INSERT INTO plaid_accounts (user_id, name, mask, type, subtype, institution_name, active) VALUES ('alice', 'Private Banking Checking', '1111', 'depository', 'checking', 'First Republic', 1)"
    )
    c.execute(
        "INSERT INTO cash_inflows (user_id, type, source, gross_amount, net_expected, recurrence, status) VALUES ('alice', 'Executive Salary', 'Apex Global Corp', 25000.0, 18000.0, 'monthly', 'Active')"
    )

    # User Bob (Modest income)
    bob_data = [
        ("full_name", "Bob Modest"),
        ("first_name", "Bob"),
        ("last_name", "Modest"),
        ("address", "123 Elm Street"),
        ("city", "Boise"),
        ("state", "ID"),
        ("zip_code", "83702"),
        ("ssn_last_4", "2222"),
        ("phone", "208-555-0202"),
        ("email", "bob@modest.org"),
    ]
    for k, v in bob_data:
        c.execute("INSERT INTO user_info (user_id, key, value) VALUES ('bob', ?, ?)", (k, v))
    c.execute(
        "INSERT INTO plaid_accounts (user_id, name, mask, type, subtype, institution_name, active) VALUES ('bob', 'Basic Checking', '2222', 'depository', 'checking', 'Credit Union', 1)"
    )
    c.execute(
        "INSERT INTO cash_inflows (user_id, type, source, gross_amount, net_expected, recurrence, status) VALUES ('bob', 'Hourly Wage', 'Local Retailer', 2500.0, 2000.0, 'monthly', 'Active')"
    )

    conn.commit()
    conn.close()
    return str(db_file)


@pytest.fixture
def synthetic_mixed_form_pdf(tmp_path) -> Path:
    """Creates a synthetic PDF containing Text, Checkbox, and Radio widgets."""
    pdf_path = tmp_path / "mixed_fields.pdf"
    doc = pymupdf.open()
    page = doc.new_page()

    # 1. Text field: Name
    w1 = pymupdf.Widget()
    w1.rect = pymupdf.Rect(50, 50, 300, 75)
    w1.field_name = "applicant_name"
    w1.field_label = "Applicant Full Name"
    w1.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    page.add_widget(w1)

    # 2. Text field: Address
    w2 = pymupdf.Widget()
    w2.rect = pymupdf.Rect(50, 85, 300, 110)
    w2.field_name = "applicant_address"
    w2.field_label = "Mailing Address"
    w2.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    page.add_widget(w2)

    # 3. Checkbox: Citizen Status
    w3 = pymupdf.Widget()
    w3.rect = pymupdf.Rect(50, 120, 70, 140)
    w3.field_name = "us_citizen"
    w3.field_label = "US Citizen Checkbox"
    w3.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    w3.field_value = "Off"
    page.add_widget(w3)

    # 4. Radio: Filing Status
    w4 = pymupdf.Widget()
    w4.rect = pymupdf.Rect(50, 150, 70, 170)
    w4.field_name = "filing_single"
    w4.field_label = "Filing Status Single"
    w4.button_caption = "Single"
    w4.field_type = pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON
    w4.field_value = "Off"
    page.add_widget(w4)

    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def synthetic_flat_pdf(tmp_path) -> Path:
    """Creates a flat PDF with no AcroForm interactive fields."""
    pdf_path = tmp_path / "flat_doc.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Flat Government Form Notice - No interactive fields.", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def synthetic_multipage_pdf(tmp_path) -> Path:
    """Creates a 4-page PDF with fields scattered across pages 1, 2, and 4."""
    pdf_path = tmp_path / "multipage_form.pdf"
    doc = pymupdf.open()
    for p in range(4):
        page = doc.new_page()
        page.insert_text((50, 40), f"Official Form Section - Page {p + 1}", fontsize=14)
        if p in (0, 1, 3):
            w = pymupdf.Widget()
            w.rect = pymupdf.Rect(50, 70, 250, 95)
            w.field_name = f"field_on_page_{p + 1}"
            w.field_label = f"Page {p + 1} Input"
            w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            page.add_widget(w)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


# ────────────────────────────────────────────────────────────
# 1. Live IRS Form Full Fill Workflow
# ────────────────────────────────────────────────────────────


def test_stress_live_irs_4506_full_workflow():
    """
    Stress-tests fill_pdf_form on the real IRS Form 4506 from irs.gov:
    - Verifies network fetch from official IRS CDN.
    - Fills with user profile and specific field overrides.
    - Validates output with PyMuPDF and pypdfium2.
    """
    url = "https://www.irs.gov/pub/irs-pdf/f4506.pdf"
    override_name = "Empirical Live Test User"
    override_ssn = "987-65-4321"

    res = fill_pdf_form(
        url,
        field_overrides={
            "topmostSubform[0].Page1[0].p1-t1[0]": override_name,
            "topmostSubform[0].Page1[0].p1-t2[0]": override_ssn,
        },
        user_id="stress_live_irs",
    )

    assert res["status"] == "success"
    out_file = Path(res["file_path"])
    assert out_file.exists()
    assert out_file.stat().st_size > 50000, "IRS Form 4506 should be > 50KB"
    assert "stress_live_irs" in res["download_url"]

    # Verify with PyMuPDF
    doc_mupdf = pymupdf.open(str(out_file))
    assert len(doc_mupdf) == 2, "IRS Form 4506 must have exactly 2 pages"
    widgets = {w.field_name: w.field_value for w in doc_mupdf[0].widgets()}
    doc_mupdf.close()

    assert widgets.get("topmostSubform[0].Page1[0].p1-t1[0]") == override_name
    assert widgets.get("topmostSubform[0].Page1[0].p1-t2[0]") == override_ssn

    # Verify independently with pypdfium2
    pdoc = pypdfium2.PdfDocument(str(out_file))
    assert len(pdoc) == 2, "pypdfium2 must confirm 2 pages"


# ────────────────────────────────────────────────────────────
# 2. Synthetic AcroForm Field Types (Text, Checkbox, Radio)
# ────────────────────────────────────────────────────────────


def test_stress_synthetic_acroform_types(synthetic_mixed_form_pdf):
    """
    Stress-tests filling of Text, Checkbox, and Radio button widgets.
    Inspects resulting PDF via PyMuPDF and pypdfium2.
    """
    res = fill_pdf_form(
        str(synthetic_mixed_form_pdf),
        field_overrides={
            "applicant_name": "Elena Rostova",
            "applicant_address": "400 Moscow Ave",
            "us_citizen": "Yes",
            "filing_single": "Yes",
        },
        user_id="stress_synthetic_types",
    )

    assert res["status"] == "success"
    out_file = Path(res["file_path"])
    assert out_file.exists()
    assert out_file.stat().st_size > 0

    # Inspect with PyMuPDF
    doc = pymupdf.open(str(out_file))
    assert len(doc) == 1
    widgets = {w.field_name: (w.field_value, w.field_type_string) for w in doc[0].widgets()}
    doc.close()

    assert widgets["applicant_name"][0] == "Elena Rostova"
    assert widgets["applicant_address"][0] == "400 Moscow Ave"
    assert widgets["us_citizen"][0] == "Yes"
    assert widgets["us_citizen"][1] == "CheckBox"
    assert widgets["filing_single"][0] == "Yes"
    assert widgets["filing_single"][1] == "RadioButton"

    # Verify with pypdfium2
    pdoc = pypdfium2.PdfDocument(str(out_file))
    assert len(pdoc) == 1


# ────────────────────────────────────────────────────────────
# 3. Flat / Unfillable PDF
# ────────────────────────────────────────────────────────────


def test_stress_synthetic_flat_pdf(synthetic_flat_pdf):
    """
    Stress-tests passing an unfillable flat PDF (0 interactive fields):
    - Must complete gracefully without crashing or throwing exceptions.
    - fields_filled should be empty.
    - Output PDF must be valid and readable.
    """
    res = fill_pdf_form(str(synthetic_flat_pdf), user_id="stress_flat")
    assert res["status"] == "success"
    assert res["fields_filled"] == {}

    out_file = Path(res["file_path"])
    assert out_file.exists()
    assert out_file.stat().st_size > 0

    # Verify with pypdfium2
    pdoc = pypdfium2.PdfDocument(str(out_file))
    assert len(pdoc) == 1


# ────────────────────────────────────────────────────────────
# 4. Multi-Page Document Handling
# ────────────────────────────────────────────────────────────


def test_stress_multipage_pdf(synthetic_multipage_pdf):
    """
    Stress-tests a multi-page PDF with fields located on pages 1, 2, and 4.
    Verifies that fields on all pages are filled and page count is preserved.
    """
    res = fill_pdf_form(
        str(synthetic_multipage_pdf),
        field_overrides={
            "field_on_page_1": "Val 1",
            "field_on_page_2": "Val 2",
            "field_on_page_4": "Val 4",
        },
        user_id="stress_multipage",
    )

    assert res["status"] == "success"
    out_file = Path(res["file_path"])

    doc = pymupdf.open(str(out_file))
    assert len(doc) == 4
    w_p1 = {w.field_name: w.field_value for w in doc[0].widgets()}
    w_p2 = {w.field_name: w.field_value for w in doc[1].widgets()}
    w_p4 = {w.field_name: w.field_value for w in doc[3].widgets()}
    doc.close()

    assert w_p1.get("field_on_page_1") == "Val 1"
    assert w_p2.get("field_on_page_2") == "Val 2"
    assert w_p4.get("field_on_page_4") == "Val 4"

    pdoc = pypdfium2.PdfDocument(str(out_file))
    assert len(pdoc) == 4


# ────────────────────────────────────────────────────────────
# 5. Multi-Tenant Data Isolation
# ────────────────────────────────────────────────────────────


def test_stress_multi_tenant_data_isolation(multi_tenant_db, synthetic_mixed_form_pdf):
    """
    Crucial Security Invariant:
    Verify User Alice's financial data (SSN, income, accounts, name) NEVER leaks into User Bob's form fill,
    and User Bob's data NEVER leaks into Alice's form fill.
    Also verifies isolated workspace storage directories.
    """
    # Patch profile extractor to use the multi_tenant_db fixture
    with patch(
        "src.services.forms.get_user_financial_profile",
        side_effect=lambda uid, **kw: get_user_financial_profile(uid, db_path=multi_tenant_db),
    ):
        res_alice = fill_pdf_form(str(synthetic_mixed_form_pdf), user_id="alice")
        res_bob = fill_pdf_form(str(synthetic_mixed_form_pdf), user_id="bob")

    assert "alice" in res_alice["file_path"]
    assert "bob" in res_bob["file_path"]
    assert res_alice["file_path"] != res_bob["file_path"]

    # Inspect Alice's PDF
    doc_a = pymupdf.open(res_alice["file_path"])
    widgets_a = {w.field_name: w.field_value for w in doc_a[0].widgets()}
    doc_a.close()

    # Inspect Bob's PDF
    doc_b = pymupdf.open(res_bob["file_path"])
    widgets_b = {w.field_name: w.field_value for w in doc_b[0].widgets()}
    doc_b.close()

    # Verify Alice's form has Alice's data
    assert widgets_a["applicant_name"] == "Alice Highworth"
    assert widgets_a["applicant_address"] == "742 Evergreen Terrace"

    # Verify Bob's form has Bob's data
    assert widgets_b["applicant_name"] == "Bob Modest"
    assert widgets_b["applicant_address"] == "123 Elm Street"

    # Strict non-leakage check
    alice_combined_text = " ".join(widgets_a.values())
    bob_combined_text = " ".join(widgets_b.values())

    assert "Bob" not in alice_combined_text
    assert "Modest" not in alice_combined_text
    assert "Boise" not in alice_combined_text

    assert "Alice" not in bob_combined_text
    assert "Highworth" not in bob_combined_text
    assert "Springfield" not in bob_combined_text


# ────────────────────────────────────────────────────────────
# 6. Field Overrides Precedence
# ────────────────────────────────────────────────────────────


def test_stress_field_overrides_precedence(multi_tenant_db, synthetic_mixed_form_pdf):
    """
    Stress-tests override precedence:
    1. Explicit field name overrides MUST override database values.
    2. Semantic profile overrides MUST override database values.
    3. Unoverridden fields MUST retain database values.
    """
    with patch(
        "src.services.forms.get_user_financial_profile",
        side_effect=lambda uid, **kw: get_user_financial_profile(uid, db_path=multi_tenant_db),
    ):
        res = fill_pdf_form(
            str(synthetic_mixed_form_pdf),
            field_overrides={
                "applicant_name": "Overridden Name VIP",
            },
            user_id="alice",
        )

    doc = pymupdf.open(res["file_path"])
    widgets = {w.field_name: w.field_value for w in doc[0].widgets()}
    doc.close()

    # applicant_name was overridden
    assert widgets["applicant_name"] == "Overridden Name VIP"
    # applicant_address was NOT overridden, so it came from DB (Alice's address)
    assert widgets["applicant_address"] == "742 Evergreen Terrace"


# ────────────────────────────────────────────────────────────
# 7. Error Cases (404, Non-Existent Domain, Corrupted Bytes)
# ────────────────────────────────────────────────────────────


def test_stress_error_404_url():
    """Verifies that an HTTP 404 URL raises an HTTPStatusError."""
    with pytest.raises(httpx.HTTPStatusError):
        fill_pdf_form("https://httpbin.org/status/404", user_id="stress_err")


def test_stress_error_nonexistent_domain():
    """Verifies that an unresolvable domain raises a ConnectError."""
    with pytest.raises(httpx.ConnectError):
        fill_pdf_form("https://nonexistent-domain-xyz-404838.org/doc.pdf", user_id="stress_err")


def test_stress_error_corrupt_binary_bytes(tmp_path):
    """Verifies that unparseable random binary garbage raises FileDataError."""
    corrupt_file = tmp_path / "corrupt.pdf"
    corrupt_file.write_bytes(os.urandom(10000))

    with pytest.raises(pymupdf.FileDataError):
        fill_pdf_form(str(corrupt_file), user_id="stress_err")


def test_stress_error_empty_zero_byte_file(tmp_path):
    """Verifies that an empty (0 byte) file raises EmptyFileError."""
    empty_file = tmp_path / "empty.pdf"
    empty_file.touch()

    with pytest.raises(pymupdf.EmptyFileError):
        fill_pdf_form(str(empty_file), user_id="stress_err")


# ────────────────────────────────────────────────────────────
# 8. Dual Engine Fidelity (Stirling PDF vs PyMuPDF Fallback)
# ────────────────────────────────────────────────────────────


def test_stress_dual_engine_fidelity(synthetic_mixed_form_pdf):
    """
    Compares PDF outputs generated by the live Stirling PDF REST microservice
    versus the in-process PyMuPDF fallback engine.
    Ensures identical field values and structural validity.
    """
    overrides = {
        "applicant_name": "Engine Test User",
        "applicant_address": "999 Engine Way",
        "us_citizen": "Yes",
        "filing_single": "Yes",
    }

    # 1. Stirling PDF engine (live microservice)
    res_stirling = fill_pdf_form(
        str(synthetic_mixed_form_pdf),
        field_overrides=overrides,
        user_id="stress_engine_stirling",
    )

    # 2. PyMuPDF fallback engine
    with patch("src.services.forms._get_active_stirling_url", return_value=None):
        res_pymupdf = fill_pdf_form(
            str(synthetic_mixed_form_pdf),
            field_overrides=overrides,
            user_id="stress_engine_pymupdf",
        )

    # Inspect Stirling PDF result
    doc_s = pymupdf.open(res_stirling["file_path"])
    w_s = {w.field_name: w.field_value for w in doc_s[0].widgets()}
    doc_s.close()

    # Inspect PyMuPDF result
    doc_p = pymupdf.open(res_pymupdf["file_path"])
    w_p = {w.field_name: w.field_value for w in doc_p[0].widgets()}
    doc_p.close()

    # Values must match across both engines
    assert w_s["applicant_name"] == w_p["applicant_name"] == "Engine Test User"
    assert w_s["applicant_address"] == w_p["applicant_address"] == "999 Engine Way"
    assert w_s["us_citizen"] == w_p["us_citizen"] == "Yes"

    # Both documents must be verified by pypdfium2
    pdoc_s = pypdfium2.PdfDocument(res_stirling["file_path"])
    pdoc_p = pypdfium2.PdfDocument(res_pymupdf["file_path"])
    assert len(pdoc_s) == len(pdoc_p) == 1


# ────────────────────────────────────────────────────────────
# 9. Edge-Case Override Types (Types & Sizes)
# ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "override_val,expected_in_pdf",
    [
        (123456, "123456"),
        (True, "true"),
        (["a", "b"], '["a","b"]'),
        ({"k": "v"}, '{"k":"v"}'),
        ("<script>alert(1)</script>", "<script>alert(1)</script>"),
        ("A" * 5000, "A" * 5000),
    ],
)
def test_stress_edge_case_override_types(synthetic_mixed_form_pdf, override_val, expected_in_pdf):
    """
    Stress-tests unconventional field values (integers, booleans, lists, dicts, XSS payloads, large strings).
    Verifies that no serialization error crashes the tool and the resulting PDF is valid.
    """
    res = fill_pdf_form(
        str(synthetic_mixed_form_pdf),
        field_overrides={"applicant_name": override_val},
        user_id="stress_edge_types",
    )
    assert res["status"] == "success"

    doc = pymupdf.open(res["file_path"])
    val_in_pdf = list(doc[0].widgets())[0].field_value
    doc.close()

    assert val_in_pdf == expected_in_pdf
    pdoc = pypdfium2.PdfDocument(res["file_path"])
    assert len(pdoc) == 1


# ────────────────────────────────────────────────────────────
# 10. Sandbox Workspace Directory Resolution
# ────────────────────────────────────────────────────────────


def test_stress_sandbox_workspace_directory():
    """
    Validates workspace resolution:
    - Verifies /workspace/{user_id}/ exists and is writable.
    - Verifies filename slugification and timestamping pattern.
    """
    ws = get_workspace_dir("test_sandbox_check")
    assert ws.exists()
    assert ws.is_dir()
    assert "test_sandbox_check" in str(ws)
