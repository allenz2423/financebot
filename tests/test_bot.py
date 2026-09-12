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
