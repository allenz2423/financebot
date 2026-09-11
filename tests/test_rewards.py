from src.services.rewards import recommend_best_card
import pytest

def test_rewards_isolation():
    with pytest.raises(ValueError):
        recommend_best_card(user_id=None, category="dining")

def test_rewards_recommendation():
    res = recommend_best_card(user_id="user1", category="dining")
    assert res["recommended_card"] == "Amex Gold"
    assert res["multiplier"] == 4.0

    res2 = recommend_best_card(user_id="user1", category="other")
    assert res2["recommended_card"] == "Citi Double Cash"
    assert res2["multiplier"] == 2.0
