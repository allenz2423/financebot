"""Regression tests for the Discord UI layer of the questionnaire feature.

The service layer (tests/test_form_flow.py) validates schemas; this module
guards the presentation layer against Discord's hard API limits (label <= 45
chars, placeholder <= 100 chars). An over-long label/placeholder makes
`send_modal` fail with HTTP 400, which previously made the "Start Form"
button look completely dead.
"""

from unittest.mock import AsyncMock

import pytest

from src.services import form_flow as ff
from src.bot.form_flow import FormModal


def create_auto_mock(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


PAGE_WITH_LONG_FIELDS = {
    "title": "Long-Field Test",
    "pages": [
        {
            "page_title": "Page One",
            "questions": [
                {
                    "key": "long_label",
                    "label": "What is your total current savings balance across all accounts combined?",
                    "input_type": "short",
                    "help_text": "Include checking, savings, and any cash you have on hand right now, even if it is less than ten dollars.",
                },
                {
                    "key": "short_key",
                    "label": "Age",
                    "help_text": "Years",
                },
            ],
        }
    ],
}


def _modal_for_question(key: str, q_index: int) -> "FormModal":
    result = ff.queue_form("4242", 123456789, PAGE_WITH_LONG_FIELDS)
    session_id = result["session_id"]
    modal = FormModal(session_id, 0, 4242)
    # children order follows question order
    return modal


def test_modal_label_respects_discord_45_char_limit():
    modal = _modal_for_question("long_label", 0)
    child = modal.children[0]
    assert len(child.label) <= 45
    # truncation keeps the beginning of the question plus an ellipsis
    assert child.label.endswith("…")


def test_modal_placeholder_respects_discord_100_char_limit():
    modal = _modal_for_question("long_label", 0)
    child = modal.children[0]
    assert child.placeholder is not None
    assert len(child.placeholder) <= 100
    assert child.placeholder.endswith("…")


def test_modal_short_fields_pass_through_untouched():
    modal = _modal_for_question("short_key", 1)
    child = modal.children[1]
    assert child.label == "Age"
    assert child.placeholder == "Years"
    assert len(child.label) <= 45
    assert len(child.placeholder) <= 100


def test_modal_custom_ids_still_match_question_keys():
    # on_submit locates answers by custom_id == question key; truncation must
    # never change the key mapping.
    modal = _modal_for_question("long_label", 0)
    keys = [child.custom_id for child in modal.children]
    assert keys == ["long_label", "short_key"]


def test_modal_title_counts_pages_from_session():
    result = ff.queue_form("7", 9, PAGE_WITH_LONG_FIELDS)
    modal = FormModal(result["session_id"], 0, 7)
    assert modal.title == "Page 1 of 1"


TWO_PAGE_SCHEMA = {
    "title": "Two-Pager",
    "pages": [
        {
            "page_title": "Page One",
            "questions": [
                {"key": "q1", "label": "Q1", "input_type": "short"},
                {"key": "q2", "label": "Q2", "input_type": "short"},
            ],
        },
        {
            "page_title": "Page Two",
            "questions": [
                {"key": "q3", "label": "Q3", "input_type": "short"},
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_start_after_partial_fill_resumes_unanswered_page():
    """Clicking Start again must resume, not reset: page 0 answered -> Start
    opens page 1's modal instead of re-opening page 0."""
    from src.bot.form_flow import StartFormView

    result = ff.queue_form("4242", 123456789, TWO_PAGE_SCHEMA)
    session_id = result["session_id"]
    ff.record_answers(session_id, {"q1": "a", "q2": "b"})

    view = StartFormView(session_id, 4242)
    interaction = create_auto_mock(4242)
    await StartFormView._start(view, interaction, None)

    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, FormModal)
    assert modal.page_index == 1
    # the page-1 modal contains the still-unanswered question
    assert [c.custom_id for c in modal.children] == ["q3"]


@pytest.mark.asyncio
async def test_start_on_fresh_form_opens_page_zero():
    from src.bot.form_flow import StartFormView

    result = ff.queue_form("4242", 123456789, TWO_PAGE_SCHEMA)
    view = StartFormView(result["session_id"], 4242)
    interaction = create_auto_mock(4242)
    await StartFormView._start(view, interaction, None)

    modal = interaction.response.send_modal.await_args.args[0]
    assert modal.page_index == 0
    assert [c.custom_id for c in modal.children] == ["q1", "q2"]


@pytest.mark.asyncio
async def test_start_tells_user_when_session_gone():
    from src.bot.form_flow import StartFormView

    view = StartFormView("missing-session", 4242)
    interaction = create_auto_mock(4242)
    await StartFormView._start(view, interaction, None)

    interaction.response.send_modal.assert_not_awaited()
    msg = interaction.response.send_message.await_args.args[0]
    assert "no longer available" in msg