import asyncio
from src.services.llm import chat_with_delilah
import src.core.state as state

# Mock reply message
class MockMessage:
    async def edit(self, content):
        print("BOT TYPING:", content)

async def run():
    # Insert a dummy user context
    state.SESSION_HISTORY["test_user"] = []
    
    # We will ask delilah to search the web
    response = await chat_with_delilah("Can you search the web for the current stock price of Apple?", "test_user", MockMessage())
    print("FINAL BOT RESPONSE:", response)

asyncio.run(run())
