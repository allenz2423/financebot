import pytest
import asyncio
from src.services.search import fetch_webpage

@pytest.mark.asyncio
async def test_fetch():
    try:
        res = await fetch_webpage("https://www.reuters.com/technology")
        print("Success! Result:")
        print(res[:500])
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(test_fetch())
