import pytest
from unittest.mock import AsyncMock, patch

class MockMessage:
    def __init__(self):
        self.edits = []
    async def edit(self, content):
        self.edits.append(content)

@pytest.mark.asyncio
async def test_bot_mock_message():
    msg = MockMessage()
    await msg.edit("Test message")
    assert msg.edits == ["Test message"]

@pytest.mark.asyncio
async def test_chat_with_delilah_mocked():
    with patch("src.services.llm.chat_with_delilah", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = "Financial analysis completed."
        msg = MockMessage()
        res = await mock_chat("What is my budget?", "test_user", msg)
        assert res == "Financial analysis completed."


@pytest.mark.asyncio
async def test_queued_answer_resumes_its_waiting_task_after_active_turn(monkeypatch):
    import src.bot.commands as commands

    chat = AsyncMock(return_value="resumed")
    monkeypatch.setattr(commands, "chat_with_delilah", chat)
    monkeypatch.setattr(commands, "_set_advisor_status", AsyncMock())
    monkeypatch.setattr(commands, "_send_thinking_placeholder", AsyncMock())
    monkeypatch.setitem(commands.ACTIVE_ADVISOR_TASKS, "owner", None)
    match = {
        "task_id": "task-original",
        "question_id": "question-token",
        "answer": "quarterly",
        "question": "Which report?",
        "objective": "Prepare the requested report",
        "step_states": [{
            "step_id": "step-1", "step_order": 0, "status": "pending",
            "next_action": "prepare_report",
        }],
    }
    await commands._run_queued_advisor_item("owner", {
        "prompt": "quarterly",
        "author_id": 123,
        "message_id": "reply-1",
        "handle": object(),
        "message": object(),
        "resume_match": match,
    })
    chat.assert_awaited_once()
    call = chat.await_args
    assert call.kwargs["resume_task_id"] == "task-original"
    assert call.kwargs["resume_question_id"] == "question-token"
    assert call.kwargs["resume_answer"] == "quarterly"
    assert "Original objective: Prepare the requested report" in call.args[0]
    assert "information only" in call.args[0]
    assert "step-1:pending (prepare_report)" in call.args[0]
