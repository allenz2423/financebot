import pytest
from src.services.reconciler import auto_reconcile_ledger, string_similarity, clean_merchant_string

def test_string_similarity():
    assert string_similarity("UBER EATS", "UBER") > 0.4

def test_clean_merchant():
    assert clean_merchant_string("AMZN Mktp Shoes") == "shoes"

def test_auto_reconcile():
    res = auto_reconcile_ledger('342385739952160769')
    assert "Ledger is clean" in res or "AUTO-RECONCILIATION COMPLETE" in res
