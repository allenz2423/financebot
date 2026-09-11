import asyncio, httpx
from src.core.state import JINA_API_KEY
async def run():
    async with httpx.AsyncClient() as client:
        res = await client.get("https://s.jina.ai/latest tech news", headers={"Authorization": f"Bearer {JINA_API_KEY}"})
        print(res.text[:1000])
asyncio.run(run())
