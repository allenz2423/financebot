import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sandbox_client
from src.services.sandbox import run_what_if_scenario

def test_sandbox_what_if():
    mutations = [
        {"type": "add_expense", "name": "Test Expense", "amount": 1000},
        {"type": "add_cash", "amount": 500}
    ]
    res = run_what_if_scenario('342385739952160769', "Test Scenario", mutations, days_ahead=15)

    assert "WHAT-IF SCENARIO: Test Scenario" in res
    assert "BASELINE REALITY" in res
    assert "ALTERNATE REALITY" in res
    assert "Test Expense ($1000)" in res
    assert "add_cash" in res


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"success": True, "stdout": "done"}


def test_install_python_package_sends_auth_header():
    """install_python_package must authenticate like every other sandbox call.

    Regression: this was the only POST missing headers=_sandbox_headers(),
    so it would 401 in production (and raise a confusing RuntimeError when
    the token was unset) while every sibling call authenticated.
    """
    mock_client = MagicMock()
    instance = AsyncMock()
    mock_client.return_value.__aenter__.return_value = instance
    mock_client.return_value.__aexit__.return_value = None
    instance.post.return_value = _FakeResp()

    with patch.object(sandbox_client, "SANDBOX_INTERNAL_TOKEN", "test-token"), \
         patch("sandbox_client.httpx.AsyncClient", mock_client):
        result = asyncio.run(sandbox_client.install_python_package("requests"))

    assert "Installed" in result
    instance.post.assert_awaited_once()
    kwargs = instance.post.await_args.kwargs
    assert kwargs["headers"] == {"X-Sandbox-Token": "test-token"}
    assert kwargs["json"]["package"] == "requests"