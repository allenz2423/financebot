import json

# This could be synced from Plaid or defined by the user.
# For simplicity, we hardcode some common multipliers as a proof of concept.
DEFAULT_CARD_MULTIPLIERS = {
    "Chase Sapphire Reserve": {"travel": 3.0, "dining": 3.0, "default": 1.0},
    "Amex Gold": {"dining": 4.0, "groceries": 4.0, "default": 1.0},
    "Citi Double Cash": {"default": 2.0}
}

def recommend_best_card(*, user_id: str, category: str, merchant: str = None) -> dict:
    """
    Recommends the best credit card to use based on the purchase category.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("recommend_best_card: forced isolation violation")

    best_card = None
    best_multiplier = 0.0
    
    category = category.lower()
    
    for card, multipliers in DEFAULT_CARD_MULTIPLIERS.items():
        multiplier = multipliers.get(category, multipliers.get("default", 1.0))
        if multiplier > best_multiplier:
            best_multiplier = multiplier
            best_card = card
            
    return {
        "status": "success",
        "recommended_card": best_card,
        "multiplier": best_multiplier,
        "category": category,
        "merchant": merchant
    }
