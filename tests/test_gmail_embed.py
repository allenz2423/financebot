"""Tests for email section-splitting in the Qdrant gmail indexer.

Covers `_split_email_sections` (the pure chunking logic behind multi-embedding
emails) — the async index path itself needs a live Qdrant so it is exercised
only through these unit checks of its deterministic pieces.
"""

import pytest

from src.services.qdrant_client import (
    _split_email_sections,
    _gmail_section_point_id,
)


def test_header_is_always_its_own_section():
    sections = _split_email_sections(
        "Order shipped",
        "orders@amazon.com",
        "2026-09-16T12:00:00+00:00",
        "Your package is on the way.",
    )
    assert sections[0][0] == "Header"
    assert "Order shipped" in sections[0][1]
    assert "orders@amazon.com" in sections[0][1]
    # Body becomes one additional section.
    assert [s[0] for s in sections[-1:]] == ["Body 1"]


def test_empty_body_yields_header_only():
    sections = _split_email_sections("Hi", "a@b.c", "date", "")
    assert len(sections) == 1
    assert sections[0][0] == "Header"


def test_body_chunks_on_paragraph_boundaries():
    body = "\n\n".join(["alpha " * 100, "beta " * 100, "gamma " * 100])
    sections = _split_email_sections("S", "x@y.z", "d", body, max_chars=800)
    names = [s[0] for s in sections]
    assert names[0] == "Header"
    # ~1640 chars of paragraphs at max_chars=800 must produce >1 body section.
    assert len([n for n in names if n.startswith("Body")]) >= 3


def test_overlong_paragraph_is_hard_split():
    body = "z" * 4000
    sections = _split_email_sections("S", "x@y.z", "d", body, max_chars=1600)
    body_sections = [s for s in sections if s[0].startswith("Body")]
    assert len(body_sections) == 3  # 4000 / 1600 -> 2.5 -> 3 chunks
    # No section exceeds the cap.
    for _, text in body_sections:
        assert len(text) <= 1600


def test_section_point_id_is_deterministic_and_distinct():
    a = _gmail_section_point_id("u1", "m1", 0)
    b = _gmail_section_point_id("u1", "m1", 0)
    c = _gmail_section_point_id("u1", "m1", 1)
    d = _gmail_section_point_id("u2", "m1", 0)
    assert a == b
    assert a != c
    assert a != d