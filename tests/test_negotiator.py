from src.services.negotiator import generate_negotiation_script
import pytest

def test_generate_negotiation_script_isolation():
    with pytest.raises(ValueError):
        generate_negotiation_script(user_id=None, service_name="Netflix")

def test_generate_negotiation_script_success():
    res = generate_negotiation_script(user_id="user1", service_name="Comcast", competitor_name="Verizon", competitor_price=49.99)
    assert res["status"] == "success"
    assert "Comcast" in res["script"]
    assert "Verizon" in res["script"]
    assert "49.99" in res["script"]

def test_generate_negotiation_script_no_competitor():
    res = generate_negotiation_script(user_id="user1", service_name="Netflix")
    assert res["status"] == "success"
    assert "Netflix" in res["script"]
    assert "Verizon" not in res["script"]
