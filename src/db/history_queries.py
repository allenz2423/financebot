import json
from datetime import datetime
from src.core.state import c, conn, CHAT_HISTORY_TURNS

def save_chat_turn(user_id: str, role: str, content: str):
    """Save a single chat turn (user or bot) to the chat_history table."""
    # Ensure isolation
    if not user_id or not isinstance(user_id, str):
        raise ValueError("save_chat_turn: forced isolation violation")
        
    c.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, json.dumps(content) if isinstance(content, dict) else str(content))
    )
    conn.commit()

def get_recent_chat_history(*, user_id: str, limit: int = CHAT_HISTORY_TURNS) -> list:
    """Retrieve the most recent chat turns for a user to maintain conversation context."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_recent_chat_history: forced isolation violation")
        
    c.execute(
        "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    )
    
    rows = c.fetchall()
    history = []
    
    # Rows are returned newest first, but we usually want them in chronological order for the LLM
    for role, content_str in reversed(rows):
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            content = content_str
        history.append({"role": role, "content": content})
        
    return history
