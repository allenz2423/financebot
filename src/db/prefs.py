import json
import sqlite3
from src.core.state import DB_PATH, TIMEZONE


_PROFILE_FIELDS = {
    "risk_tolerance",
    "autonomy_limits",
    "notification_preferences",
    "preferred_model",
    "presentation_style",
    "inferred_preference_learning",
}
_PROFILE_ENUMS = {
    "risk_tolerance": {"cautious", "balanced", "growth"},
    "presentation_style": {"concise", "balanced", "detailed"},
}
_AUTONOMY_FIELDS = {"require_mutation_confirmation"}
_NOTIFICATION_FIELDS = {"task_completion", "financial_alerts", "reminders"}
_NOTIFICATION_VALUES = {"all", "important_only", "off"}


def _validate_profile_values(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("Profile values must be an object")
    unknown = set(values) - _PROFILE_FIELDS
    if unknown:
        raise ValueError(f"Unknown operating profile field(s): {', '.join(sorted(map(repr, unknown)))}")

    validated = {}
    for field, value in values.items():
        if field in _PROFILE_ENUMS:
            if not isinstance(value, str) or value not in _PROFILE_ENUMS[field]:
                raise ValueError(f"Invalid value for {field}")
        elif field == "inferred_preference_learning":
            if type(value) is not bool:
                raise ValueError("inferred_preference_learning must be a boolean")
        elif field == "preferred_model":
            if not isinstance(value, str) or len(value) > 120:
                raise ValueError("preferred_model must be a string of at most 120 characters")
        else:
            if not isinstance(value, dict):
                raise ValueError(f"{field} must be an object")
            allowed = _AUTONOMY_FIELDS if field == "autonomy_limits" else _NOTIFICATION_FIELDS
            unknown_nested = set(value) - allowed
            if unknown_nested:
                raise ValueError(
                    f"Unknown {field} field(s): {', '.join(sorted(map(repr, unknown_nested)))}"
                )
            for nested_field, nested_value in value.items():
                if field == "autonomy_limits":
                    if type(nested_value) is not bool:
                        raise ValueError(f"{nested_field} must be a boolean")
                elif not isinstance(nested_value, str) or nested_value not in _NOTIFICATION_VALUES:
                    raise ValueError(f"Invalid value for {field}.{nested_field}")
        validated[field] = value.copy() if isinstance(value, dict) else value
    return validated


def _decode_json_object(value) -> dict:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _is_user_stated_source(entry) -> bool:
    return bool(
        isinstance(entry, dict)
        and entry.get("source") == "user_stated"
        and isinstance(entry.get("source_turn_id"), str)
        and 1 <= len(entry["source_turn_id"]) <= 200
    )


def _validated_profile_state(stored_values, stored_provenance) -> tuple[dict, dict]:
    """Keep only typed fields with valid user-stated provenance."""
    raw_values = _decode_json_object(stored_values)
    raw_provenance = _decode_json_object(stored_provenance)
    values = {}
    provenance = {}
    for field, value in raw_values.items():
        entry = raw_provenance.get(field)
        if not _is_user_stated_source(entry):
            continue
        try:
            validated = _validate_profile_values({field: value})[field]
        except (TypeError, ValueError):
            continue
        root_source = {
            "source": "user_stated",
            "source_turn_id": entry["source_turn_id"],
        }
        if isinstance(validated, dict):
            saved_sources = entry.get("fields")
            filtered_values = {}
            field_sources = {}
            for nested_field, nested_value in validated.items():
                nested_source = (
                    saved_sources.get(nested_field)
                    if isinstance(saved_sources, dict) else None
                )
                if not _is_user_stated_source(nested_source):
                    # Older profile rows used one source turn for the entire
                    # nested object. Preserve that valid provenance on upgrade.
                    nested_source = root_source
                filtered_values[nested_field] = nested_value
                field_sources[nested_field] = {
                    "source": "user_stated",
                    "source_turn_id": nested_source["source_turn_id"],
                }
            values[field] = filtered_values
            provenance[field] = {**root_source, "fields": field_sources}
        else:
            values[field] = validated
            provenance[field] = root_source
    return values, provenance

def init_prefs_schema():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                timezone TEXT DEFAULT 'America/New_York'
            )
        """)
        columns = {row[1] for row in c.execute("PRAGMA table_info(user_preferences)")}
        if "operating_profile" not in columns:
            c.execute("ALTER TABLE user_preferences ADD COLUMN operating_profile TEXT NOT NULL DEFAULT '{}'")
        if "operating_profile_provenance" not in columns:
            c.execute(
                "ALTER TABLE user_preferences "
                "ADD COLUMN operating_profile_provenance TEXT NOT NULL DEFAULT '{}'"
            )
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


def get_operating_profile(user_id: str) -> dict:
    """Return the user's configured operating profile and per-field provenance."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT operating_profile, operating_profile_provenance "
            "FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {"values": {}, "provenance": {}}
    values, provenance = _validated_profile_state(row[0], row[1])
    return {
        "values": values,
        "provenance": provenance,
    }


def set_operating_profile(user_id: str, values: dict, *, source_turn_id: str) -> dict:
    """Merge validated profile values and record that the user stated each field."""
    validated = _validate_profile_values(values)
    if not isinstance(source_turn_id, str) or not 1 <= len(source_turn_id.strip()) <= 200:
        raise ValueError("source_turn_id must contain 1-200 characters")
    source_turn_id = source_turn_id.strip()
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")
    user_id = user_id.strip()

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT operating_profile, operating_profile_provenance "
            "FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        profile, provenance = _validated_profile_state(row[0], row[1]) if row else ({}, {})
        for field, value in validated.items():
            if isinstance(value, dict) and isinstance(profile.get(field), dict):
                profile[field] = {**profile[field], **value}
                prior_sources = provenance.get(field, {}).get("fields", {})
                field_sources = dict(prior_sources) if isinstance(prior_sources, dict) else {}
                for nested_field in value:
                    field_sources[nested_field] = {
                        "source": "user_stated",
                        "source_turn_id": source_turn_id,
                    }
                provenance[field] = {
                    "source": "user_stated",
                    "source_turn_id": source_turn_id,
                    "fields": field_sources,
                }
            elif isinstance(value, dict):
                profile[field] = value
                provenance[field] = {
                    "source": "user_stated",
                    "source_turn_id": source_turn_id,
                    "fields": {
                        nested_field: {
                            "source": "user_stated",
                            "source_turn_id": source_turn_id,
                        }
                        for nested_field in value
                    },
                }
            else:
                profile[field] = value
                provenance[field] = {
                    "source": "user_stated",
                    "source_turn_id": source_turn_id,
                }
        conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, operating_profile, operating_profile_provenance) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "operating_profile = excluded.operating_profile, "
            "operating_profile_provenance = excluded.operating_profile_provenance",
            (user_id, json.dumps(profile, sort_keys=True), json.dumps(provenance, sort_keys=True)),
        )
    return get_operating_profile(user_id)


def forget_operating_profile(user_id: str, fields=None) -> dict:
    """Forget all or selected profile fields without changing timezone preferences."""
    if fields is None:
        selected = set(_PROFILE_FIELDS)
    else:
        if isinstance(fields, str):
            raise ValueError("fields must be an iterable of profile field names")
        try:
            selected = set(fields)
        except TypeError as exc:
            raise ValueError("fields must be an iterable of profile field names") from exc
        unknown = selected - _PROFILE_FIELDS
        if unknown:
            raise ValueError(f"Unknown operating profile field(s): {', '.join(sorted(map(repr, unknown)))}")

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT operating_profile, operating_profile_provenance "
            "FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        profile, provenance = _validated_profile_state(row[0], row[1]) if row else ({}, {})
        for field in selected:
            profile.pop(field, None)
            provenance.pop(field, None)
        if row:
            conn.execute(
                "UPDATE user_preferences SET operating_profile = ?, "
                "operating_profile_provenance = ? WHERE user_id = ?",
                (json.dumps(profile, sort_keys=True), json.dumps(provenance, sort_keys=True), user_id),
            )
    return get_operating_profile(user_id)

init_prefs_schema()
