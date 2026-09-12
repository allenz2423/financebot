from src.services.rewards import recommend_best_card, infer_category, get_user_wallet_cards, audit_wallet_rewards
import pytest

def test_rewards_isolation():
    with pytest.raises(ValueError):
        recommend_best_card(user_id=None, category="dining")
    with pytest.raises(ValueError):
        audit_wallet_rewards(user_id=None)

def test_rewards_recommendation():
    res = recommend_best_card(user_id="user1", category="dining")
    assert res["recommended_card"] == "Amex Gold"
    assert res["multiplier"] == 4.0

    res2 = recommend_best_card(user_id="user1", category="other")
    assert res2["recommended_card"] == "Citi Double Cash"
    assert res2["multiplier"] == 2.0

def test_merchant_category_inference():
    assert infer_category("", "Starbucks") == "dining"
    assert infer_category("", "Trader Joe's") == "groceries"
    assert infer_category("", "Uber") == "travel"
    assert infer_category("", "Shell Oil") == "gas"
    assert infer_category("", "Netflix") == "streaming"

def test_wallet_card_lookup_and_audit():
    res = recommend_best_card(user_id="342385739952160769", merchant="Chipotle")
    assert res["status"] == "success"
    assert res["multiplier"] >= 3.0

    audit_res = audit_wallet_rewards("342385739952160769", days_back=90)
    assert "CREDIT CARD REWARDS OPTIMIZATION AUDIT" in audit_res
    assert "Estimated Rewards Earned" in audit_res
