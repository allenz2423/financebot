import asyncio
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sandbox_client


class _ReadResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "success": True,
            "path": "workspace/42/reports/final.csv",
            "filename": "final.csv",
            "content_base64": base64.b64encode(b"a,b\n1,2\n").decode("ascii"),
        }


def _mock_workspace_read():
    client_factory = MagicMock()
    client = AsyncMock()
    client_factory.return_value.__aenter__.return_value = client
    client_factory.return_value.__aexit__.return_value = None
    client.post.return_value = _ReadResponse()
    return client_factory


def test_send_workspace_file_returns_flat_exact_byte_evidence_and_canonical_path():
    payload = b"a,b\n1,2\n"
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=987654)))
    client_factory = _mock_workspace_read()

    with patch.object(sandbox_client, "SANDBOX_INTERNAL_TOKEN", "test-token"), patch(
        "sandbox_client.httpx.AsyncClient", client_factory
    ):
        result = asyncio.run(
            sandbox_client.send_workspace_file(
                channel,
                "42",
                "latest.csv",  # caller alias differs from the canonical response path
                return_evidence=True,
            )
        )

    assert result == {
        "path": "reports/final.csv",
        "filename": "final.csv",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "message_id": "987654",
        "valid_csv": True,
    }
    assert set(result) == {
        "path", "filename", "sha256", "byte_size", "message_id", "valid_csv",
    }
    sent_file = channel.send.await_args.kwargs["file"]
    assert sent_file.fp.read() == payload


def test_nonempty_content_changes_produce_different_hashes():
    async def send_with(content):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))
        with patch.object(
            sandbox_client,
            "_read_workspace_file_details",
            AsyncMock(return_value=("report.bin", content, "reports/report.bin")),
        ):
            return await sandbox_client.send_workspace_file(
                channel, "42", "report.bin", return_evidence=True
            )

    first, second = asyncio.run(
        _send_two(send_with, b"first content", b"second content")
    )
    assert first["sha256"] == hashlib.sha256(b"first content").hexdigest()
    assert second["sha256"] == hashlib.sha256(b"second content").hexdigest()
    assert first["sha256"] != second["sha256"]
    assert first["byte_size"] > 0


def test_json_artifact_evidence_validates_the_delivered_bytes():
    async def send(content):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=8)))
        with patch.object(
            sandbox_client,
            "_read_workspace_file_details",
            AsyncMock(return_value=("transactions.json", content, "transactions.json")),
        ):
            return await sandbox_client.send_workspace_file(
                channel, "42", "transactions.json", return_evidence=True
            )

    valid, invalid = asyncio.run(
        _send_two(send, b'{"transactions":[]}', b'{"transactions":[NaN]}')
    )
    duplicate_keys = asyncio.run(send(b'{"amount":1,"amount":999}'))
    assert valid["valid_json"] is True
    assert invalid["valid_json"] is False
    assert duplicate_keys["valid_json"] is False


def test_csv_artifact_evidence_validates_delivered_structure_and_rejects_json():
    async def send(content):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=9)))
        with patch.object(
            sandbox_client,
            "_read_workspace_file_details",
            AsyncMock(return_value=("transactions.csv", content, "transactions.csv")),
        ):
            return await sandbox_client.send_workspace_file(
                channel, "42", "transactions.csv", return_evidence=True
            )

    valid, json_as_csv = asyncio.run(_send_two(
        send,
        b"date,amount\n2026-01-01,12.50\n",
        b'{"transactions":[]}',
    ))
    assert valid["valid_csv"] is True
    assert json_as_csv["valid_csv"] is False
    assert asyncio.run(send(b"123"))["valid_csv"] is False
    ragged_result = asyncio.run(send(b"a,b\n1\n"))
    assert ragged_result["valid_csv"] is False


async def _send_two(send_with, first, second):
    return await send_with(first), await send_with(second)


def test_missing_file_returns_false_without_sending():
    channel = SimpleNamespace(send=AsyncMock())
    with patch.object(
        sandbox_client, "_read_workspace_file_details", AsyncMock(return_value=None)
    ):
        result = asyncio.run(
            sandbox_client.send_workspace_file(
                channel, "42", "missing.csv", return_evidence=True
            )
        )
    assert result is False
    channel.send.assert_not_awaited()


def test_server_read_without_canonical_path_fails_before_discord_send():
    response = _ReadResponse()
    response.json = lambda: {
        "success": True,
        "filename": "final.csv",
        "content_base64": base64.b64encode(b"a,b\n1,2\n").decode("ascii"),
    }
    client_factory = _mock_workspace_read()
    client_factory.return_value.__aenter__.return_value.post.return_value = response
    channel = SimpleNamespace(send=AsyncMock())

    with patch.object(sandbox_client, "SANDBOX_INTERNAL_TOKEN", "test-token"), patch(
        "sandbox_client.httpx.AsyncClient", client_factory
    ):
        result = asyncio.run(
            sandbox_client.send_workspace_file(
                channel, "42", "latest.csv", return_evidence=True,
            )
        )

    assert result is False
    channel.send.assert_not_awaited()


def test_ambiguous_send_failure_is_unknown_and_default_contract_remains_bool():
    channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("send failed")))
    read = AsyncMock(return_value=("report.csv", b"x,y\n", "reports/report.csv"))
    with patch.object(sandbox_client, "_read_workspace_file_details", read):
        evidence_result = asyncio.run(
            sandbox_client.send_workspace_file(
                channel, "42", "report.csv", return_evidence=True
            )
        )
        default_result = asyncio.run(
            sandbox_client.send_workspace_file(channel, "42", "report.csv")
        )

    assert evidence_result == {"status": "unknown"}
    assert default_result is False


def test_default_success_result_remains_true():
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=7)))
    with patch.object(
        sandbox_client,
        "_read_workspace_file_details",
        AsyncMock(return_value=("report.csv", b"x", "reports/report.csv")),
    ):
        result = asyncio.run(
            sandbox_client.send_workspace_file(channel, "42", "report.csv")
        )
    assert result is True


def test_canonical_workspace_path_rejects_a_different_owner_prefix():
    assert sandbox_client._canonical_owner_relative_path(
        "workspace/99/reports/report.csv", "42"
    ) == ""
    assert sandbox_client._canonical_owner_relative_path(
        "workspace/42/reports/report.csv", "42"
    ) == "reports/report.csv"
