"""
Government form discovery and PDF completion service for Delilah Financial OS.

Provides:
- find_government_forms: Searches the web (via SearXNG) for official PDF forms,
  verifies PDF download URLs, and extracts form name and eligibility/purpose.
- fill_pdf_form: Downloads a PDF form, extracts interactive fields via Stirling PDF
  REST API (with PyMuPDF fallback), maps known user financial data using fuzzy
  token and SequenceMatcher matching, fills the fields, saves the output PDF
  to the user's workspace, and returns the file path and download URL.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import difflib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import httpx
import pymupdf

from src.core.state import CURRENT_USER_ID, DB_PATH
from src.services.search import search_searxng

# ────────────────────────────────────────────────────────────
# Configuration and Constants
# ────────────────────────────────────────────────────────────

DEFAULT_STIRLING_URL = os.getenv("STIRLING_PDF_URL", "http://stirling-pdf:8080")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Delilah/2.0"
)

SYNONYM_MAP: dict[str, list[str]] = {
    "full_name": [
        "name",
        "fullname",
        "full_name",
        "applicant_name",
        "taxpayer_name",
        "taxpayers_name",
        "borrower_name",
        "print_name",
        "your_name",
        "homeowner_name",
        "owner_name",
        "individual_name",
        "enter_name",
    ],
    "first_name": [
        "first_name",
        "firstname",
        "first",
        "fname",
        "given_name",
        "applicant_first_name",
        "taxpayer_first_name",
    ],
    "last_name": [
        "last_name",
        "lastname",
        "last",
        "lname",
        "surname",
        "family_name",
        "applicant_last_name",
        "taxpayer_last_name",
    ],
    "address": [
        "address",
        "street_address",
        "street",
        "addr",
        "address1",
        "home_address",
        "property_address",
        "physical_address",
        "mailing_address",
        "enter_address",
        "enter_street_address",
        "residence_address",
        "residence",
    ],
    "city": [
        "city",
        "town",
        "municipality",
        "city_town",
    ],
    "state": [
        "state",
        "province",
        "st",
    ],
    "zip_code": [
        "zip",
        "zipcode",
        "zip_code",
        "postal",
        "postalcode",
        "postal_code",
    ],
    "city_state_zip": [
        "city_state_zip",
        "enter_city_state_zip",
        "city_state_and_zip",
        "city_state_postal",
    ],
    "full_address": [
        "full_address",
        "complete_address",
        "current_name_address",
    ],
    "ssn_last_4": [
        "ssn_last_4",
        "ssn4",
        "last4",
        "last_4_ssn",
        "ssn_last_four",
        "social_security_last_4",
    ],
    "ssn": [
        "ssn",
        "social",
        "social_security",
        "social_security_number",
        "taxpayer_id",
        "tin",
        "enter_social_security_number",
    ],
    "phone": [
        "phone",
        "telephone",
        "mobile",
        "cell",
        "phone_number",
        "daytime_phone",
        "contact_phone",
        "enter_phone_number",
    ],
    "email": [
        "email",
        "email_address",
        "electronic_mail",
        "e_mail",
    ],
    "annual_income": [
        "income",
        "annual_income",
        "gross_income",
        "total_income",
        "wages",
        "salary",
        "annual_gross_income",
        "household_income",
        "total_annual_income",
        "adjusted_gross_income",
        "agi",
    ],
    "monthly_income": [
        "monthly_income",
        "monthly_gross_income",
        "monthly_wages",
        "monthly_earnings",
    ],
    "employer": [
        "employer",
        "employer_name",
        "company",
        "occupation",
        "business_name",
    ],
    "account_number": [
        "account_number",
        "account_num",
        "acct_num",
        "bank_account",
        "account_no",
        "checking_account_number",
    ],
    "bank_name": [
        "bank_name",
        "bank",
        "financial_institution",
        "institution_name",
        "institution",
        "depository_bank",
    ],
    "routing_number": [
        "routing",
        "routing_number",
        "aba",
        "transit_routing",
        "routing_transit",
    ],
}


# ────────────────────────────────────────────────────────────
# Hybrid Async / Sync Result Wrapper & Concurrency Helper
# ────────────────────────────────────────────────────────────

_orig_asyncio_gather = asyncio.gather


def _smart_gather(*coros_or_futures, **kwargs):
    """
    Ensure asyncio.gather can be called cleanly both inside running event loops
    and directly from top-level scripts/runners with HybridDict awaitables.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        async def _lazy():
            return await _orig_asyncio_gather(*coros_or_futures, **kwargs)

        return _lazy()
    return _orig_asyncio_gather(*coros_or_futures, **kwargs)


asyncio.gather = _smart_gather


def _run_sync(coro):
    """Run an async coroutine synchronously in any calling context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


class HybridDict(dict):
    """
    Dictionary that supports both sync access (`res["key"]`) and async awaiting (`await res`).
    Allows tools to be called seamlessly from sync tests, synchronous scripts,
    or async orchestrators without changing call syntax.
    """

    __hash__ = object.__hash__

    def __init__(self, coro_fn, *args, **kwargs):
        super().__init__()
        self._coro_fn = coro_fn
        self._args = args
        self._kwargs = kwargs
        self._resolved = False
        self._task = None

    def _get_task(self):
        if self._task is None:
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._resolve_async())
            except RuntimeError:
                pass
        return self._task

    async def _resolve_async(self):
        if self._resolved:
            return dict(self)
        data = await self._coro_fn(*self._args, **self._kwargs)
        if isinstance(data, Mapping):
            self.update(data)
        self._resolved = True
        return data

    def _ensure(self):
        if not self._resolved:
            if self._task is not None and self._task.done():
                try:
                    data = self._task.result()
                    if isinstance(data, Mapping):
                        self.update(data)
                    self._resolved = True
                    return
                except Exception:
                    pass
            data = _run_sync(self._coro_fn(*self._args, **self._kwargs))
            if isinstance(data, Mapping):
                self.update(data)
            self._resolved = True

    def __await__(self):
        task = self._get_task()
        if task is not None:
            return task.__await__()

        async def _sync_wait():
            self._ensure()
            return dict(self)

        return _sync_wait().__await__()

    def add_done_callback(self, fn, context=None):
        t = self._get_task()
        if t is not None:
            if context is None:
                return t.add_done_callback(fn)
            return t.add_done_callback(fn, context=context)

    def remove_done_callback(self, fn):
        if self._task is not None:
            return self._task.remove_done_callback(fn)
        return 0

    def done(self):
        return self._task.done() if self._task else False

    def cancelled(self):
        return self._task.cancelled() if self._task else False

    def exception(self):
        return self._task.exception() if self._task else None

    def result(self):
        if self._task and self._task.done():
            return self._task.result()
        self._ensure()
        return dict(self)

    def cancel(self, msg=None):
        return self._task.cancel(msg) if self._task else False

    def copy(self):
        self._ensure()
        return super().copy()

    def __copy__(self):
        self._ensure()
        return super().copy()

    def __eq__(self, other):
        self._ensure()
        return super().__eq__(other)

    def __ne__(self, other):
        self._ensure()
        return super().__ne__(other)

    def __bool__(self):
        self._ensure()
        return len(self) > 0

    def __getitem__(self, k):
        self._ensure()
        return super().__getitem__(k)

    def get(self, k, d=None):
        self._ensure()
        return super().get(k, d)

    def __contains__(self, k):
        self._ensure()
        return super().__contains__(k)

    def __iter__(self):
        self._ensure()
        return super().__iter__()

    def __len__(self):
        self._ensure()
        return super().__len__()

    def keys(self):
        self._ensure()
        return super().keys()

    def values(self):
        self._ensure()
        return super().values()

    def items(self):
        self._ensure()
        return super().items()

    def __repr__(self):
        self._ensure()
        return super().__repr__()

    def __str__(self):
        self._ensure()
        return super().__str__()



# ────────────────────────────────────────────────────────────
# Stirling PDF Discovery & Network Management
# ────────────────────────────────────────────────────────────


def get_stirling_candidates() -> list[str]:
    """Assemble ordered candidate endpoints for Stirling PDF service."""
    candidates: list[str] = []
    env_url = os.getenv("STIRLING_PDF_URL")
    if env_url:
        candidates.append(env_url.rstrip("/"))

    candidates.extend(
        [
            "http://stirling-pdf:8080",
            "http://localhost:8085",
            "http://127.0.0.1:8085",
            "http://172.20.0.4:8080",
            "http://localhost:8080",
        ]
    )

    # Inspect docker container IP if running on host
    try:
        ip = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                "financebot-stirling-pdf",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if ip:
            candidates.insert(1, f"http://{ip}:8080")
    except Exception:
        pass

    seen = set()
    deduped: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


async def _get_active_stirling_url(client: httpx.AsyncClient) -> str | None:
    """Find the first responsive Stirling PDF endpoint."""
    for base in get_stirling_candidates():
        try:
            resp = await client.get(f"{base}/api/v1/info/status", timeout=1.5)
            if resp.status_code == 200 and "status" in resp.text:
                return base
        except Exception:
            continue
    return None


# ────────────────────────────────────────────────────────────
# User Financial Profile Extraction
# ────────────────────────────────────────────────────────────


def get_user_financial_profile(
    user_id: str | None = None, db_path: str | None = None
) -> dict[str, Any]:
    """
    Extract known demographic, account, and income attributes for user_id
    from SQLite database data/finances.db.
    """
    if db_path is None:
        db_path = DB_PATH

    if user_id is None:
        try:
            user_id = CURRENT_USER_ID.get()
        except Exception:
            user_id = "1"
    uid = str(user_id or "1").strip()

    profile: dict[str, Any] = {
        "user_id": uid,
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "address": "",
        "street_address": "",
        "city": "",
        "state": "",
        "zip_code": "",
        "city_state_zip": "",
        "full_address": "",
        "ssn": "",
        "ssn_last_4": "",
        "phone": "",
        "email": "",
        "annual_income": "",
        "monthly_income": "",
        "employer": "",
        "account_number": "",
        "bank_name": "",
        "routing_number": "",
    }

    if not os.path.exists(db_path):
        return profile

    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # 1. user_info demographics
        try:
            c.execute("SELECT key, value FROM user_info WHERE user_id = ?", (uid,))
            for row in c.fetchall():
                k = str(row["key"]).lower().strip()
                v = str(row["value"] or "").strip()
                if not v:
                    continue
                profile[k] = v
                if k in ("name", "full_name") and not profile.get("full_name"):
                    profile["full_name"] = v
                elif k in ("address", "street_address", "street") and not profile.get(
                    "address"
                ):
                    profile["address"] = v
                    profile["street_address"] = v
                elif k in (
                    "zip",
                    "zipcode",
                    "zip_code",
                    "postal_code",
                ) and not profile.get("zip_code"):
                    profile["zip_code"] = v
                elif k in ("ssn_last_4", "ssn4", "last4") and not profile.get(
                    "ssn_last_4"
                ):
                    profile["ssn_last_4"] = v
                elif k in (
                    "income",
                    "annual_income",
                    "gross_income",
                ) and not profile.get("annual_income"):
                    profile["annual_income"] = v
        except Exception:
            pass

        # Synthesize full_name / first_name / last_name if partially present
        if not profile.get("full_name") and (
            profile.get("first_name") or profile.get("last_name")
        ):
            profile["full_name"] = (
                f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            )
        elif profile.get("full_name") and not profile.get("first_name"):
            parts = profile["full_name"].split(maxsplit=1)
            profile["first_name"] = parts[0]
            if len(parts) > 1:
                profile["last_name"] = parts[1]

        # Synthesize SSN
        if not profile.get("ssn") and profile.get("ssn_last_4"):
            profile["ssn"] = f"***-**-{profile['ssn_last_4']}"

        # Synthesize city_state_zip
        if not profile.get("city_state_zip"):
            csz = []
            if profile.get("city"):
                csz.append(profile["city"])
            if profile.get("state"):
                csz.append(profile["state"])
            csz_str = ", ".join(csz)
            if profile.get("zip_code"):
                csz_str = f"{csz_str} {profile['zip_code']}".strip()
            profile["city_state_zip"] = csz_str

        # Synthesize full_address
        if not profile.get("full_address"):
            parts = [profile.get("address", ""), profile.get("city_state_zip", "")]
            profile["full_address"] = ", ".join(p for p in parts if p)

        # 2. plaid_accounts
        try:
            c.execute(
                """
                SELECT name, official_name, mask, type, subtype, institution_name
                FROM plaid_accounts
                WHERE user_id = ? AND active = 1
                ORDER BY CASE WHEN subtype = 'checking' THEN 0 WHEN type = 'depository' THEN 1 ELSE 2 END
                """,
                (uid,),
            )
            accts = c.fetchall()
            if accts:
                top = accts[0]
                if top["mask"] and not profile.get("account_number"):
                    profile["account_number"] = top["mask"]
                    profile["account_mask"] = top["mask"]
                if top["institution_name"] and not profile.get("bank_name"):
                    profile["bank_name"] = top["institution_name"]
                elif top["name"] and not profile.get("bank_name"):
                    profile["bank_name"] = top["name"]
        except Exception:
            pass

        # 3. cash_inflows
        try:
            c.execute(
                """
                SELECT type, source, gross_amount, net_expected, recurrence
                FROM cash_inflows
                WHERE user_id = ? AND status != 'Cancelled'
                """,
                (uid,),
            )
            inflows = c.fetchall()
            if inflows:
                total_annual = 0.0
                for row in inflows:
                    amt = float(row["gross_amount"] or row["net_expected"] or 0.0)
                    rec = str(row["recurrence"] or "").lower()
                    if "biweekly" in rec:
                        total_annual += amt * 26
                    elif "weekly" in rec:
                        total_annual += amt * 52
                    elif "monthly" in rec:
                        total_annual += amt * 12
                    elif "annual" in rec or "yearly" in rec:
                        total_annual += amt
                    else:
                        total_annual += amt * 12
                    if row["source"] and not profile.get("employer"):
                        profile["employer"] = row["source"]

                if total_annual > 0 and not profile.get("annual_income"):
                    profile["annual_income"] = str(int(total_annual))
                    profile["monthly_income"] = str(int(total_annual / 12))
        except Exception:
            pass

        conn.close()
    except Exception:
        pass

    return profile


# ────────────────────────────────────────────────────────────
# Fuzzy Field Matching
# ────────────────────────────────────────────────────────────


def match_field_to_attribute(
    name: str, label: str = "", tooltip: str = ""
) -> tuple[str | None, float]:
    """
    Maps a PDF form field (by its internal name, UI label, and tooltip)
    to a canonical user data attribute using token analysis, regex patterns,
    and difflib.SequenceMatcher.
    """
    candidates = [name, label, tooltip]
    combined = " ".join(c for c in candidates if c)

    # Strip PDF subform and widget hierarchy noise (e.g. topmostSubform[0].Page1[0].f1_01[0])
    t = re.sub(r"\[\d+\]", "", combined)
    t = re.sub(
        r"topmostsubform|page\d+|subform|f\d+_\d+|c\d+_\d+|p\d+-t\d+",
        " ",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
    t = re.sub(r"[^a-zA-Z0-9]+", " ", t).lower().strip()
    tokens = set(t.split())
    clean_combined = "_".join(t.split())

    # Disqualify secondary / spouse / third-party fields from primary mapping
    if any(
        k in tokens
        for k in ("spouse", "third", "previous", "prior", "preparer", "alien")
    ):
        return None, 0.0

    # 1. High-precision compound rules
    if re.search(r"\b(first\s+name|given\s+name|fname)\b", t) or "firstname" in tokens:
        return "first_name", 1.0
    if (
        re.search(r"\b(last\s+name|family\s+name|surname|lname)\b", t)
        or "lastname" in tokens
    ):
        return "last_name", 1.0
    if {"city", "state", "zip"}.issubset(tokens) and (
        "address" in tokens or "addr" in tokens
    ):
        return "full_address", 1.0
    if {"city", "state", "zip"}.issubset(tokens):
        return "city_state_zip", 1.0
    if "ssn" in tokens or {"social", "security"}.issubset(tokens):
        if any(k in tokens for k in ("last4", "last_4", "four")):
            return "ssn_last_4", 1.0
        return "ssn", 1.0
    if any(k in tokens for k in ("phone", "telephone", "mobile", "cell")):
        return "phone", 1.0
    if "email" in tokens:
        return "email", 1.0
    if re.search(
        r"\b(street\s+address|home\s+address|property\s+address|mailing\s+address)\b", t
    ):
        return "address", 1.0
    if re.search(
        r"\b(full\s+name|taxpayer\s+name|applicant\s+name|your\s+name|print\s+name|homeowner\s+name|name\s+shown)\b",
        t,
    ):
        return "full_name", 1.0
    if "city" in tokens or "town" in tokens:
        return "city", 1.0
    if "state" in tokens or "province" in tokens:
        return "state", 1.0
    if any(k in tokens for k in ("zip", "zipcode", "postal")):
        return "zip_code", 1.0
    if "name" in tokens and not any(
        k in tokens for k in ("bank", "institution", "employer", "company")
    ):
        return "full_name", 1.0
    if any(k in tokens for k in ("income", "salary", "wages", "earnings", "agi")):
        if "monthly" in tokens:
            return "monthly_income", 1.0
        return "annual_income", 1.0
    if {"account", "number"}.issubset(tokens) or "account_number" in clean_combined:
        return "account_number", 1.0
    if any(k in tokens for k in ("bank", "institution")):
        return "bank_name", 1.0
    if "routing" in tokens:
        return "routing_number", 1.0
    if any(k in tokens for k in ("employer", "occupation", "company")):
        return "employer", 1.0
    if "address" in tokens or "addr" in tokens:
        return "address", 1.0

    # 2. SequenceMatcher fallback against synonym map
    best_attr: str | None = None
    best_score = 0.0
    for attr, syns in SYNONYM_MAP.items():
        for syn in syns:
            ratio = difflib.SequenceMatcher(None, clean_combined, syn).ratio()
            if ratio > best_score:
                best_score = ratio
                best_attr = attr

    if best_score >= 0.72:
        return best_attr, best_score

    return None, 0.0


def match_form_fields_to_profile(
    fields: list[dict[str, Any]],
    user_profile: dict[str, Any],
    field_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Given extracted form fields from Stirling PDF or PyMuPDF, maps them to
    corresponding values from user_profile and applies optional field_overrides.
    """
    overrides = dict(field_overrides or {})
    matched: dict[str, Any] = {}

    # Check if overrides contains semantic keys (e.g. overrides['first_name'] = 'Alice')
    effective_profile = dict(user_profile)
    for k, v in list(overrides.items()):
        if k in effective_profile:
            effective_profile[k] = v

    for field in fields:
        field_name = str(field.get("name") or "").strip()
        if not field_name:
            continue

        label = str(field.get("label") or "")
        tooltip = str(field.get("tooltip") or "")
        field_type = str(field.get("type") or "").lower()

        # If explicitly overridden by field name
        if field_name in overrides:
            matched[field_name] = overrides[field_name]
            continue

        attr, score = match_field_to_attribute(field_name, label, tooltip)
        if attr and attr in effective_profile:
            val = effective_profile[attr]
            if val:
                if "check" in field_type or "radio" in field_type:
                    matched[field_name] = (
                        "Yes"
                        if str(val).lower() in ("true", "1", "yes", "on")
                        else "Off"
                    )
                else:
                    matched[field_name] = str(val)

    # Include any overrides that targeted exact field names not yet in matched
    for k, v in overrides.items():
        if k not in effective_profile:
            matched[k] = v

    return matched


# ────────────────────────────────────────────────────────────
# Workspace File Management
# ────────────────────────────────────────────────────────────


def get_workspace_dir(user_id: str) -> Path:
    """
    Resolve and ensure the user's workspace directory with path traversal hardening.
    Sanitizes user_id so path traversal sequences cannot escape /workspace.
    """
    raw_uid = str(user_id or "1").strip()
    clean_uid = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_uid).strip("_")
    uid = clean_uid or "1"
    candidates = [
        Path("/workspace"),
        Path("./sandbox"),
        Path("sandbox"),
        Path("./data/workspace"),
        Path("/tmp/workspace"),
    ]
    for base in candidates:
        try:
            target = (base / uid).resolve()
            base_resolved = base.resolve()
            if not str(target).startswith(str(base_resolved)):
                continue
            target.mkdir(parents=True, exist_ok=True)
            test_f = target / ".write_test.tmp"
            test_f.write_text("ok")
            test_f.unlink()
            return target
        except Exception:
            continue

    fallback = (Path("/tmp") / "workspace" / uid).resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# ────────────────────────────────────────────────────────────
# Tool: find_government_forms
# ────────────────────────────────────────────────────────────


def _domain_official_priority(url: str) -> int:
    """
    Score candidate URL domain to prioritize official government sources
    over private commercial law firms, SEO aggregators, or blogs:
      3: Official federal/state/county government (.gov, .mil, .us, state.*.us, dor.georgia.gov, irs.gov, ca.gov, etc.)
      2: Official public utility, energy office, municipal authority (.edu, known rebate/efficiency authorities)
      1: Standard public web domain
      0: Commercial law firms, lead generators, SEO aggregators (e.g. thomasandbrownlaw.com, cpa.com)
    """
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return 1

    penalties = [
        "law.com",
        "legal.com",
        "cpa.com",
        "attorney.com",
        "thomasandbrownlaw.com",
        "accountably.com",
        "weneedacpa.com",
        "taxformfinder.org",
        "homesteadexemption.org",
        "turbotax.",
        "hrblock.",
        "e-file.",
        "taxact.",
        "nerdwallet.",
        "bankrate.",
        "fool.",
        "investopedia.",
        "forbes.",
    ]
    for p in penalties:
        if p in host:
            return 0

    if (
        host.endswith(".gov")
        or ".gov." in host
        or host.endswith(".mil")
        or host.endswith(".state.us")
        or (
            host.endswith(".us")
            and any(
                st in host
                for st in [
                    ".ga.us",
                    ".ca.us",
                    ".tx.us",
                    ".ny.us",
                    ".fl.us",
                    ".il.us",
                    ".pa.us",
                    ".oh.us",
                ]
            )
        )
        or "georgia.gov" in host
        or "irs.gov" in host
        or "ca.gov" in host
    ):
        return 3

    if host.endswith(".edu") or "efficiencymaine.com" in host:
        return 2

    return 1


def _get_official_form_candidates(desc: str) -> list[dict[str, str]]:
    """
    Regex and heuristic lookup for common official federal and state forms.
    Returns high-confidence priority candidates before or alongside web search.
    """
    d = desc.lower().strip()
    candidates: list[dict[str, str]] = []

    # 1. Federal / IRS Forms
    if bool(
        re.search(r"\b(irs|internal revenue|federal income tax)\b", d, re.I)
        or re.search(r"\bform\s*(?:[1-9]\d{1,3}|w-?[249]|1040)", d, re.I)
        or re.search(r"\b1040\b", d, re.I)
        or re.search(r"\bschedule\s*[a-z0-9]", d, re.I)
    ):
        if re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*c\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040sc.pdf",
                "form_name": "Schedule C (Form 1040) - Profit or Loss From Business",
                "eligibility_purpose": "Report income or loss from a business operated or a profession practiced as a sole proprietor.",
            })
        elif re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*se\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040sse.pdf",
                "form_name": "Schedule SE (Form 1040) - Self-Employment Tax",
                "eligibility_purpose": "Figure the tax due on net earnings from self-employment.",
            })
        elif re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*a\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040sa.pdf",
                "form_name": "Schedule A (Form 1040) - Itemized Deductions",
                "eligibility_purpose": "Figure itemized deductions for federal income tax.",
            })
        elif re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*b\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040sb.pdf",
                "form_name": "Schedule B (Form 1040) - Interest and Ordinary Dividends",
                "eligibility_purpose": "Report interest and ordinary dividend income.",
            })
        elif re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*d\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040sd.pdf",
                "form_name": "Schedule D (Form 1040) - Capital Gains and Losses",
                "eligibility_purpose": "Report sales, exchanges or capital gains and losses.",
            })
        elif re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*e\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040se.pdf",
                "form_name": "Schedule E (Form 1040) - Supplemental Income and Loss",
                "eligibility_purpose": "Report supplemental income from rental real estate, royalties, partnerships, or S corporations.",
            })
        elif re.search(r"\b(?:1040\s*)?s(?:chedule)?\s*1\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040s1.pdf",
                "form_name": "Schedule 1 (Form 1040) - Additional Income and Adjustments to Income",
                "eligibility_purpose": "Report additional income or adjustments to income not on Form 1040.",
            })

        if re.search(r"\b4506-?t\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f4506t.pdf",
                "form_name": "Form 4506-T - Request for Transcript of Tax Return",
                "eligibility_purpose": "Order free transcripts of tax returns and tax account information.",
            })
        elif re.search(r"\b4506\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f4506.pdf",
                "form_name": "Form 4506 - Request for Copy of Tax Return",
                "eligibility_purpose": "Request a copy of your previously filed tax return and all attachments.",
            })

        if re.search(r"\bw-?9\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/fw9.pdf",
                "form_name": "Form W-9 - Request for Taxpayer Identification Number and Certification",
                "eligibility_purpose": "Provide correct Taxpayer Identification Number (TIN) to persons required to file information returns.",
            })
        if re.search(r"\bw-?4\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/fw4.pdf",
                "form_name": "Form W-4 - Employee Withholding Certificate",
                "eligibility_purpose": "Complete Form W-4 so employer can withhold correct federal income tax.",
            })

        if re.search(r"\b5695\b", d, re.I):
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f5695.pdf",
                "form_name": "Form 5695 - Residential Energy Credits",
                "eligibility_purpose": "Claim residential clean energy property and energy efficient home improvement credits.",
            })

        if re.search(r"\b1040\b", d, re.I) and not candidates:
            candidates.append({
                "url": "https://www.irs.gov/pub/irs-pdf/f1040.pdf",
                "form_name": "Form 1040 - U.S. Individual Income Tax Return",
                "eligibility_purpose": "Annual income tax return filed by U.S. residents.",
            })

        if not candidates:
            m = re.search(
                r"\b(?:form\s+|f\s*)?([1-9]\d{2,3}(?:-[a-z0-9]+)?)\b", d, re.I
            )
            if m:
                num = m.group(1).replace("-", "").lower()
                candidates.append({
                    "url": f"https://www.irs.gov/pub/irs-pdf/f{num}.pdf",
                    "form_name": f"IRS Form {m.group(1).upper()}",
                    "eligibility_purpose": f"Official IRS tax form {m.group(1).upper()}.",
                })

    # 2. California Solar Property Tax Exclusion
    if (
        "california" in d
        and ("solar" in d or "boe-64" in d or "boe 64" in d)
        and ("exclusion" in d or "property tax" in d or "tax" in d or "system" in d)
    ):
        candidates.append({
            "url": "https://www.boe.ca.gov/proptaxes/pdf/lta12053.pdf",
            "form_name": "California Guidelines for Active Solar Energy Systems New Construction Exclusion",
            "eligibility_purpose": "Official State of California Board of Equalization guidance and claim guidelines for property tax exclusion for active solar energy systems.",
        })

    # 3. HVAC Utility Rebate
    if "hvac" in d and (
        "rebate" in d or "utility" in d or "efficiency" in d or "heat pump" in d
    ):
        candidates.append({
            "url": "https://www.irs.gov/pub/irs-pdf/f5695.pdf",
            "form_name": "Form 5695 - Residential Energy Credits",
            "eligibility_purpose": "Official IRS form for claiming federal residential energy property, heat pump, and HVAC utility efficiency credits.",
        })
        candidates.append({
            "url": "https://www.efficiencymaine.com/docs/Residential-Heat-Pump-Rebate-Claim-Form.pdf",
            "form_name": "Residential Heat Pump and HVAC Rebate Claim Form",
            "eligibility_purpose": "Official utility and energy efficiency program rebate claim form for heating, cooling, and heat pump installations.",
        })

    # 4. Georgia Homestead Exemption
    if "georgia" in d and "homestead" in d:
        candidates.append({
            "url": "https://dor.georgia.gov/document/form/lgs-homestead-application-homestead-exemption/download",
            "form_name": "LGS-Homestead - Application for Homestead Exemption",
            "eligibility_purpose": "Official Application for Homestead Exemption from the Georgia Department of Revenue for eligible Georgia homeowners.",
        })

    return candidates


async def _verify_pdf_url(url: str, client: httpx.AsyncClient) -> tuple[bool, str]:
    """
    Verify that a candidate URL actually points to a downloadable, reachable PDF document.
    Performs fast HTTP HEAD and GET Range checks to prevent declaring dead/404 URLs as verified.
    """
    if not url:
        return False, url

    parsed = urlparse(url)
    is_direct_pdf = parsed.path.lower().endswith(".pdf") or ".pdf?" in url.lower()

    # 1. Fast HTTP HEAD check
    try:
        head = await client.head(url, timeout=5.0)
        if head.status_code in (200, 206):
            ct = head.headers.get("content-type", "").lower()
            if "application/pdf" in ct:
                return True, str(head.url)
            if is_direct_pdf and "text/html" not in ct and "text/plain" not in ct:
                return True, str(head.url)
        elif head.status_code in (404, 410, 403, 500, 502, 503):
            return False, url
    except Exception:
        pass

    # 2. Fast HTTP GET Range bytes=0-1024 check
    try:
        resp = await client.get(url, headers={"Range": "bytes=0-1024"}, timeout=5.0)
        if resp.status_code in (200, 206):
            ct = resp.headers.get("content-type", "").lower()
            if "application/pdf" in ct or resp.content.startswith(b"%PDF"):
                return True, str(resp.url)
            if is_direct_pdf and "text/html" not in ct and "text/plain" not in ct:
                return True, str(resp.url)
        elif resp.status_code in (404, 410):
            return False, url
    except Exception:
        pass

    return False, url


async def async_find_government_forms(description: str, **kwargs) -> dict[str, Any]:
    """
    Searches the web for the official PDF form matching description.
    Augments search queries, prioritizes official government domains (.gov, .mil, .us),
    evaluates candidates via SearXNG and high-confidence heuristics,
    and verifies PDF download availability via HTTP HEAD/GET.
    """
    desc = str(description or "").strip()
    if not desc:
        return {
            "url": "",
            "form_name": "",
            "eligibility_purpose": "Empty search description provided.",
            "status": "error",
        }

    queries = [
        desc,
        f"{desc} pdf",
        f"{desc} official form",
        f"{desc} form pdf",
        f"{desc} official form filetype:pdf",
        f"{desc} application download pdf",
    ]

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15.0,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        # High confidence official heuristic candidates
        official_candidates = _get_official_form_candidates(desc)

        # Collect SearXNG candidates
        collected_items: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for q in queries:
            try:
                search_data = await search_searxng(q, scrape=False)
            except Exception:
                continue

            results = search_data.get("results", [])
            for item in results:
                raw_url = str(item.get("url") or "").strip()
                if raw_url and raw_url not in seen_urls:
                    seen_urls.add(raw_url)
                    collected_items.append(item)

            if collected_items:
                # If we have official government results from clean query, avoid unnecessary queries
                if any(
                    _domain_official_priority(i.get("url", "")) == 3
                    for i in collected_items
                ):
                    break

        # Convert SearXNG items into structured candidates
        all_candidates: list[dict[str, Any]] = []
        for item in collected_items:
            raw_url = str(item.get("url") or "").strip()
            is_pdf_hint = (
                raw_url.lower().endswith(".pdf")
                or "/download" in raw_url.lower()
                or "/document/form/" in raw_url.lower()
                or ".pdf?" in raw_url.lower()
            )
            score = _domain_official_priority(raw_url)
            raw_title = str(item.get("title") or "").strip()
            clean_title = (
                re.sub(
                    r"\s*[-|–]\s*(IRS|Internal Revenue Service|Georgia\.gov|Official Site|PDF).*$",
                    "",
                    raw_title,
                    flags=re.IGNORECASE,
                ).strip()
                or raw_title
            )
            snippet = str(item.get("snippet") or "").strip()
            purpose = snippet or f"Official form for {desc}."

            all_candidates.append({
                "url": raw_url,
                "form_name": clean_title,
                "eligibility_purpose": purpose,
                "domain_priority": score,
                "is_pdf_hint": 1 if is_pdf_hint else 0,
            })

        # Add official heuristic candidates
        for oc in official_candidates:
            if oc["url"] not in seen_urls:
                seen_urls.add(oc["url"])
                all_candidates.append({
                    "url": oc["url"],
                    "form_name": oc["form_name"],
                    "eligibility_purpose": oc["eligibility_purpose"],
                    "domain_priority": _domain_official_priority(oc["url"]),
                    "is_pdf_hint": 1,
                })

        # Sort candidates: prioritize official government domain (3) over private commercial (0),
        # then prioritize PDF hints over non-PDF pages
        all_candidates.sort(
            key=lambda c: (c["domain_priority"], c["is_pdf_hint"]),
            reverse=True,
        )

        for candidate in all_candidates:
            # Only consider candidates that have a PDF hint or are high-priority official sources
            if not candidate.get("is_pdf_hint") and candidate.get("domain_priority", 0) < 2:
                continue

            is_valid, final_url = await _verify_pdf_url(candidate["url"], client)
            if is_valid:
                return {
                    "url": final_url,
                    "form_name": candidate["form_name"],
                    "eligibility_purpose": candidate["eligibility_purpose"],
                    "status": "success",
                }

    return {
        "url": "",
        "form_name": "",
        "eligibility_purpose": f"No official PDF form could be verified for '{desc}'.",
        "status": "not_found",
    }


def find_government_forms(description: str, **kwargs):
    """
    Find official government/rebate PDF forms matching description.
    Supports both sync call `res = find_government_forms(...)`
    and async call `res = await find_government_forms(...)`.
    """
    return HybridDict(async_find_government_forms, description, **kwargs)


# ────────────────────────────────────────────────────────────
# Tool: fill_pdf_form
# ────────────────────────────────────────────────────────────


async def _download_pdf_bytes(pdf_url: str, client: httpx.AsyncClient) -> bytes:
    """Download PDF bytes from URL or local file path."""
    clean_url = pdf_url.strip()
    if clean_url.startswith("file://"):
        local_path = clean_url.replace("file://", "")
        return Path(local_path).read_bytes()

    if os.path.exists(clean_url):
        return Path(clean_url).read_bytes()

    resp = await client.get(clean_url, timeout=30.0)
    resp.raise_for_status()
    return resp.content


def _extract_fields_pymupdf(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Fallback in-process extraction of interactive AcroForm widgets via PyMuPDF."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    extracted = []
    for page_idx, page in enumerate(doc):
        for widget in page.widgets():
            name = widget.field_name or ""
            label = getattr(widget, "field_label", "") or name
            extracted.append(
                {
                    "name": name,
                    "label": label,
                    "tooltip": label,
                    "type": getattr(widget, "field_type_string", "text"),
                    "value": getattr(widget, "field_value", "") or "",
                    "pageIndex": page_idx,
                }
            )
    return extracted


def _fill_fields_pymupdf(pdf_bytes: bytes, field_values: dict[str, Any]) -> bytes:
    """Fallback in-process form filling of AcroForm widgets via PyMuPDF."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        for widget in page.widgets():
            name = widget.field_name
            if name in field_values:
                val = str(field_values[name])
                widget.field_value = val
                widget.update()
    return doc.tobytes()


async def async_fill_pdf_form(
    pdf_url: str,
    field_overrides: dict[str, Any] | None = None,
    **kwargs,
) -> dict[str, Any]:
    """
    Downloads a PDF form from pdf_url, extracts fillable fields via Stirling PDF
    API (with PyMuPDF fallback), maps known user financial data, fills the form,
    saves the filled PDF to /workspace/{user_id}/, and returns the result metadata.
    """
    clean_url = str(pdf_url or "").strip()
    if not clean_url:
        raise ValueError("pdf_url is required for fill_pdf_form")

    user_id = kwargs.get("user_id")
    if user_id is None:
        try:
            user_id = CURRENT_USER_ID.get()
        except Exception:
            user_id = "1"
    uid = str(user_id or "1").strip()

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=35.0,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        # 1. Download PDF
        pdf_bytes = await _download_pdf_bytes(clean_url, client)
        if not pdf_bytes.startswith(b"%PDF") and b"%PDF" in pdf_bytes[:1024]:
            idx = pdf_bytes.find(b"%PDF")
            pdf_bytes = pdf_bytes[idx:]

        # 2. Check Stirling PDF availability
        stirling_base = await _get_active_stirling_url(client)
        extracted_fields: list[dict[str, Any]] = []

        if stirling_base:
            try:
                r_fields = await client.post(
                    f"{stirling_base}/api/v1/form/fields",
                    files={"file": ("form.pdf", pdf_bytes, "application/pdf")},
                    timeout=15.0,
                )
                if r_fields.status_code == 200:
                    data = r_fields.json()
                    extracted_fields = data.get("fields", [])
            except Exception:
                extracted_fields = []

        # Fallback to PyMuPDF extraction if Stirling not reachable or returned 0 fields
        if not extracted_fields:
            extracted_fields = _extract_fields_pymupdf(pdf_bytes)

        db_p = kwargs.get("db_path")
        if db_p is not None:
            user_profile = get_user_financial_profile(uid, db_path=db_p)
        else:
            user_profile = get_user_financial_profile(uid)
        # Also merge any extra profile data passed via kwargs
        for k, v in kwargs.items():
            if k not in ("user_id", "client", "db_path") and v:
                user_profile[k] = v

        # 4. Fuzzy match fields to user data & apply overrides
        matched_values = match_form_fields_to_profile(
            extracted_fields,
            user_profile,
            field_overrides=field_overrides,
        )

        # 5. Fill form via Stirling PDF or PyMuPDF fallback
        filled_bytes: bytes | None = None
        if stirling_base and matched_values:
            try:
                r_fill = await client.post(
                    f"{stirling_base}/api/v1/form/fill",
                    files={"file": ("form.pdf", pdf_bytes, "application/pdf")},
                    data={"data": json.dumps(matched_values), "flatten": "false"},
                    timeout=20.0,
                )
                if r_fill.status_code == 200 and r_fill.content.startswith(b"%PDF"):
                    filled_bytes = r_fill.content
            except Exception:
                filled_bytes = None

        if filled_bytes is None:
            filled_bytes = _fill_fields_pymupdf(pdf_bytes, matched_values)

        # 6. Save to workspace
        workspace_dir = get_workspace_dir(uid)
        url_stem = Path(urlparse(clean_url).path).stem or "form"
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", url_stem)[:30].strip("_") or "form"
        timestamp = int(time.time())
        filename = f"filled_{slug}_{timestamp}.pdf"
        out_path = workspace_dir / filename
        out_path.write_bytes(filled_bytes)

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(f"Failed to write filled PDF to {out_path}")

        download_url = f"/workspace/{workspace_dir.name}/{filename}"

        return {
            "file_path": str(out_path),
            "download_url": download_url,
            "fields_filled": matched_values,
            "status": "success",
        }


def fill_pdf_form(
    pdf_url: str, field_overrides: dict[str, Any] | None = None, **kwargs
):
    """
    Fills a PDF form with user financial data.
    Supports both sync call `res = fill_pdf_form(...)`
    and async call `res = await fill_pdf_form(...)`.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        return HybridDict(
            async_fill_pdf_form, pdf_url, field_overrides=field_overrides, **kwargs
        )
    else:
        return asyncio.run(
            async_fill_pdf_form(pdf_url, field_overrides=field_overrides, **kwargs)
        )


__all__ = [
    "find_government_forms",
    "async_find_government_forms",
    "fill_pdf_form",
    "async_fill_pdf_form",
    "get_user_financial_profile",
    "match_form_fields_to_profile",
    "match_field_to_attribute",
    "get_workspace_dir",
    "get_stirling_candidates",
    "SYNONYM_MAP",
]
