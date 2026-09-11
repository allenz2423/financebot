import pytest
from unittest.mock import AsyncMock, MagicMock
import discord
from src.bot.commands import _TransactionTriageView, _TriageNoteModal, _TransactionCategorySelect

def test_triage_view_structure():
    mock_ctx = MagicMock()
    mock_ctx.author.id = 12345

    tx_list = [
        {
            "id": 101,
            "date": "2026-09-08",
            "merchant": "TARGET T-0987",
            "clean_merchant": "Target",
            "amount": 45.50,
            "account_used": "Credit Card (...1234)",
            "category": "Shopping",
            "status": "Pending Evaluation",
            "judgment": None,
            "is_locked": 0
        },
        {
            "id": 102,
            "date": "2026-09-09",
            "merchant": "CHIPOTLE 1234",
            "clean_merchant": "Chipotle",
            "amount": 14.25,
            "account_used": "Checking (...5678)",
            "category": "Dining Out",
            "status": "Pending Evaluation",
            "judgment": None,
            "is_locked": 0
        }
    ]

    view = _TransactionTriageView(mock_ctx, tx_list)
    assert len(view.children) == 6  # 1 select dropdown + 5 buttons

    embed = view.build_embed()
    assert "Transaction Triage Queue (1 of 2)" in embed.title
    assert "Target" in embed.fields[0].value
    assert "$45.50" in embed.fields[1].value

    # Skip to next
    view.index += 1
    view._rebuild_components()
    embed2 = view.build_embed()
    assert "Transaction Triage Queue (2 of 2)" in embed2.title
    assert "Chipotle" in embed2.fields[0].value
