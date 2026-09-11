from src.services.price_drop import analyze_price_drop_and_draft_refund
import pytest

def test_price_drop_isolation():
    with pytest.raises(ValueError):
        analyze_price_drop_and_draft_refund(user_id=None, item_name="TV", merchant="Best Buy", purchase_price=500.0, current_price=400.0)

def test_price_drop_success():
    res = analyze_price_drop_and_draft_refund(
        user_id="user1", item_name="Sony Headphones", merchant="Amazon", purchase_price=250.0, current_price=199.99
    )
    assert res["status"] == "success"
    assert res["refund_amount"] == 50.01
    assert "Amazon" in res["draft_email"]
    assert "$50.01" in res["draft_email"]

def test_no_price_drop():
    res = analyze_price_drop_and_draft_refund(
        user_id="user1", item_name="Sony Headphones", merchant="Amazon", purchase_price=199.99, current_price=250.0
    )
    assert res["status"] == "no_drop"
    assert res["refund_amount"] == 0.0
