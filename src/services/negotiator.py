import os
import json
from src.core.state import get_db, c

def generate_negotiation_script(*, user_id: str, service_name: str, competitor_name: str = None, competitor_price: float = None) -> dict:
    """
    Generates a negotiation script for calling customer retention.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("generate_negotiation_script: forced isolation violation")

    # In a real scenario, this would use an LLM or a predefined template.
    # For now, we will just construct a template based on the provided inputs.
    script = f"Hi, I've been a loyal customer of {service_name} for a while, but my bill has gotten too high. "
    if competitor_name and competitor_price:
        script += f"I was looking at {competitor_name} and they are offering a similar service for ${competitor_price:.2f}/month. "
    script += "I'd love to stay with you, but I need to know if you can match this or offer a better rate on my current plan?"
    
    return {
        "status": "success",
        "service": service_name,
        "script": script
    }
