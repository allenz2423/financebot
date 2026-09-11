import asyncio
from src.db.memory import save_epistemic_memory, semantic_search_memory

async def main():
    await save_epistemic_memory(
        "test_user", 
        "User prefers Starbucks over Dunkin.", 
        memory_type="preference", 
        provenance_type="user_stated",
        confidence=1.0,
        evidence_refs=["chat_123"]
    )
    res = await semantic_search_memory("test_user", "coffee preference")
    print(res)

asyncio.run(main())
