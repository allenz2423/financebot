import pytest
import asyncio
from src.services.search import search_searxng

@pytest.mark.asyncio
async def test_search():
    try:
        res = await search_searxng("who won the 2024 election?")
        print("Success! Result:")
        print(str(res)[:500])
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(test_search())
