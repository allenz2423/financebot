import sqlite3
from src.core.state import DB_PATH, TIMEZONE

def init_prefs_schema():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                timezone TEXT DEFAULT 'America/New_York'
            )
        """)
        conn.commit()

def get_user_timezone(user_id: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT timezone FROM user_preferences WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row and row[0]:
            return row[0]
    return TIMEZONE

def set_user_timezone(user_id: str, tz: str):
    # Validate timezone
    import zoneinfo
    try:
        zoneinfo.ZoneInfo(tz)
    except Exception:
        raise ValueError(f"Invalid timezone: {tz}")
        
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO user_preferences (user_id, timezone) 
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone
        """, (user_id, tz))
        conn.commit()

init_prefs_schema()
