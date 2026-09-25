from src.services.browser_evidence import browser_evidence, browser_tool_ran, observed_page


def test_navigation_is_not_page_observation():
    trace = [{"name": "navigate_browser", "ok": True, "status": "confirmed"}]
    assert browser_tool_ran(trace)
    assert not observed_page(trace)


def test_confirmed_observation_supports_live_page_guard():
    trace = [{
        "name": "scrape_rendered_page",
        "ok": True,
        "status": "confirmed",
        "complete": True,
        "receipt_id": "receipt-page",
    }]
    evidence = browser_evidence(trace)
    assert observed_page(trace)
    assert evidence[0].event_type == "observation"
    assert evidence[0].receipt_id == "receipt-page"


def test_unknown_mutation_is_not_browser_evidence():
    trace = [{
        "name": "browser_click",
        "ok": True,
        "status": "unknown",
        "complete": False,
    }]
    assert not browser_tool_ran(trace)
