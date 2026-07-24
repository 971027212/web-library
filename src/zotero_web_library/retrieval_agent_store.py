from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import app_store
from .utils import new_key, now_iso


MESSAGE_RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS retrieval_agent_sessions (
  job_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_agent_messages (
  message_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'session_only',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  retention_until TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS retrieval_agent_turns (
  turn_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  user_message_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS retrieval_agent_memory (
  memory_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'suggested',
  source_job_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_agent_feedback (
  feedback_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  feedback_type TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS retrieval_agent_sessions_library_idx
ON retrieval_agent_sessions (library_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS retrieval_agent_messages_job_idx
ON retrieval_agent_messages (library_id, job_id, created_at);

CREATE INDEX IF NOT EXISTS retrieval_agent_turns_job_idx
ON retrieval_agent_turns (library_id, job_id, created_at DESC);

CREATE INDEX IF NOT EXISTS retrieval_agent_memory_library_idx
ON retrieval_agent_memory (library_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS retrieval_agent_feedback_job_idx
ON retrieval_agent_feedback (library_id, job_id, created_at DESC);
"""

TURN_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS retrieval_agent_turns (
  turn_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  user_message_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);
"""


def _table_columns(conn: Any, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_legacy_store(conn: Any) -> None:
    message_columns = _table_columns(conn, "retrieval_agent_messages")
    if "metadata_json" not in message_columns:
        conn.execute(
            "ALTER TABLE retrieval_agent_messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )

    memory_columns = _table_columns(conn, "retrieval_agent_memory")
    if "status" not in memory_columns:
        conn.execute(
            "ALTER TABLE retrieval_agent_memory ADD COLUMN status TEXT NOT NULL DEFAULT 'suggested'"
        )
        if "enabled" in memory_columns:
            conn.execute(
                """
                UPDATE retrieval_agent_memory
                SET status = CASE WHEN enabled = 1 THEN 'enabled' ELSE 'suggested' END
                """
            )

    turn_columns = _table_columns(conn, "retrieval_agent_turns")
    if "turn_id" in turn_columns and "result_json" in turn_columns:
        return
    conn.execute("DROP TABLE IF EXISTS retrieval_agent_turns_legacy")
    conn.execute("ALTER TABLE retrieval_agent_turns RENAME TO retrieval_agent_turns_legacy")
    conn.executescript(TURN_TABLE_SCHEMA)
    legacy_columns = _table_columns(conn, "retrieval_agent_turns_legacy")
    if "turn_request_id" in legacy_columns:
        conn.execute(
            """
            INSERT OR IGNORE INTO retrieval_agent_turns
              (turn_id, library_id, job_id, user_message_id, status, result_json, error,
               created_at, updated_at, started_at, finished_at)
            SELECT turn_request_id, library_id, job_id, user_message_id, status, '{}', error,
                   created_at, updated_at, started_at, finished_at
            FROM retrieval_agent_turns_legacy
            """
        )
    conn.execute("DROP TABLE retrieval_agent_turns_legacy")


def ensure_store() -> None:
    app_store.ensure_app_store()
    with app_store.connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_store(conn)
        conn.executescript(INDEX_SCHEMA)
        conn.commit()


def _decode_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError):
        return fallback


def _session_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["state"] = _decode_json(item.pop("state_json", ""), {})
    return item


def get_session(library_id: str, job_id: str) -> dict[str, Any] | None:
    ensure_store()
    with app_store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_agent_sessions WHERE library_id = ? AND job_id = ?",
            (library_id, job_id),
        ).fetchone()
    return _session_from_row(row) if row else None


def upsert_session(library_id: str, job_id: str, state: dict[str, Any]) -> dict[str, Any]:
    ensure_store()
    timestamp = now_iso()
    with app_store.connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM retrieval_agent_sessions WHERE library_id = ? AND job_id = ?",
            (library_id, job_id),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing else timestamp
        conn.execute(
            """
            INSERT INTO retrieval_agent_sessions
              (job_id, library_id, state_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              library_id = excluded.library_id,
              state_json = excluded.state_json,
              updated_at = excluded.updated_at
            """,
            (job_id, library_id, json.dumps(state, ensure_ascii=False), created_at, timestamp),
        )
        conn.commit()
    session = get_session(library_id, job_id)
    if not session:
        raise RuntimeError("智能体会话保存失败。")
    return session


def _message_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item.pop("_sequence", None)
    item["metadata"] = _decode_json(item.pop("metadata_json", ""), {})
    return item


def purge_expired_messages() -> None:
    ensure_store()
    timestamp = now_iso()
    with app_store.connect() as conn:
        conn.execute(
            """
            DELETE FROM retrieval_agent_messages
            WHERE visibility = 'session_only'
              AND retention_until != ''
              AND retention_until < ?
            """,
            (timestamp,),
        )
        conn.commit()


def add_message(
    library_id: str,
    job_id: str,
    role: str,
    content: str,
    *,
    visibility: str = "session_only",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_store()
    clean_role = str(role or "").strip().lower()
    if clean_role not in {"user", "assistant", "system"}:
        raise ValueError("不支持的智能体消息角色。")
    clean_content = str(content or "").strip()
    if not clean_content:
        raise ValueError("消息不能为空。")
    clean_visibility = "persisted" if visibility == "persisted" else "session_only"
    timestamp = now_iso()
    retention_until = ""
    if clean_visibility == "session_only":
        retention_until = (
            datetime.now(timezone.utc) + timedelta(days=MESSAGE_RETENTION_DAYS)
        ).isoformat()
    message_id = f"agent-message-{new_key(14).lower()}"
    with app_store.connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_agent_messages
              (message_id, library_id, job_id, role, content, visibility, metadata_json, created_at, retention_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                library_id,
                job_id,
                clean_role,
                clean_content,
                clean_visibility,
                json.dumps(metadata or {}, ensure_ascii=False),
                timestamp,
                retention_until,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM retrieval_agent_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return _message_from_row(row)


def list_messages(library_id: str, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    purge_expired_messages()
    clean_limit = max(1, min(int(limit or 100), 300))
    with app_store.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
              SELECT rowid AS _sequence, * FROM retrieval_agent_messages
              WHERE library_id = ? AND job_id = ?
              ORDER BY created_at DESC, rowid DESC
              LIMIT ?
            )
            ORDER BY created_at ASC, _sequence ASC
            """,
            (library_id, job_id, clean_limit),
        ).fetchall()
    return [_message_from_row(row) for row in rows]


def _turn_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["result"] = _decode_json(item.pop("result_json", ""), {})
    return item


def create_turn(library_id: str, job_id: str, user_message_id: str) -> dict[str, Any]:
    ensure_store()
    active = latest_turn(library_id, job_id)
    if active and active.get("status") in {"queued", "running"}:
        raise ValueError("智能体正在处理上一条消息，请稍候。")
    timestamp = now_iso()
    turn_id = f"agent-turn-{new_key(14).lower()}"
    with app_store.connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_agent_turns
              (turn_id, library_id, job_id, user_message_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (turn_id, library_id, job_id, user_message_id, timestamp, timestamp),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM retrieval_agent_turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
    return _turn_from_row(row)


def get_turn(library_id: str, job_id: str, turn_id: str) -> dict[str, Any] | None:
    ensure_store()
    with app_store.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM retrieval_agent_turns
            WHERE library_id = ? AND job_id = ? AND turn_id = ?
            """,
            (library_id, job_id, turn_id),
        ).fetchone()
    return _turn_from_row(row) if row else None


def latest_turn(library_id: str, job_id: str) -> dict[str, Any] | None:
    ensure_store()
    with app_store.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM retrieval_agent_turns
            WHERE library_id = ? AND job_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (library_id, job_id),
        ).fetchone()
    return _turn_from_row(row) if row else None


def update_turn(
    library_id: str,
    job_id: str,
    turn_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    ensure_store()
    current = get_turn(library_id, job_id, turn_id)
    if not current:
        raise ValueError("智能体处理记录不存在。")
    timestamp = now_iso()
    started_at = current.get("started_at") or (timestamp if status == "running" else "")
    finished_at = current.get("finished_at") or (
        timestamp if status in {"completed", "failed", "interrupted"} else ""
    )
    with app_store.connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_agent_turns
            SET status = ?, result_json = ?, error = ?, updated_at = ?, started_at = ?, finished_at = ?
            WHERE library_id = ? AND job_id = ? AND turn_id = ?
            """,
            (
                status,
                json.dumps(result if result is not None else current.get("result") or {}, ensure_ascii=False),
                str(error or ""),
                timestamp,
                started_at,
                finished_at,
                library_id,
                job_id,
                turn_id,
            ),
        )
        conn.commit()
    updated = get_turn(library_id, job_id, turn_id)
    if not updated:
        raise RuntimeError("智能体处理记录更新失败。")
    return updated


def interrupt_active_turn(library_id: str, job_id: str) -> dict[str, Any] | None:
    turn = latest_turn(library_id, job_id)
    if not turn or turn.get("status") not in {"queued", "running"}:
        return turn
    return update_turn(
        library_id,
        job_id,
        str(turn["turn_id"]),
        status="interrupted",
        error="用户中断了本轮智能体处理。",
    )


def _memory_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["content"] = _decode_json(item.pop("content_json", ""), {})
    item["enabled"] = item.get("status") == "enabled"
    return item


def create_memory_suggestion(
    library_id: str,
    *,
    kind: str,
    content: dict[str, Any],
    source_job_id: str,
) -> dict[str, Any]:
    ensure_store()
    clean_content = content if isinstance(content, dict) else {}
    if not clean_content:
        raise ValueError("记忆建议不能为空。")
    timestamp = now_iso()
    memory_id = f"agent-memory-{new_key(14).lower()}"
    with app_store.connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_agent_memory
              (memory_id, library_id, kind, content_json, status, source_job_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'suggested', ?, ?, ?)
            """,
            (
                memory_id,
                library_id,
                str(kind or "preference").strip() or "preference",
                json.dumps(clean_content, ensure_ascii=False),
                source_job_id,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM retrieval_agent_memory WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
    return _memory_from_row(row)


def list_memory(
    library_id: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_store()
    clean_limit = max(1, min(int(limit or 100), 300))
    query = "SELECT * FROM retrieval_agent_memory WHERE library_id = ?"
    params: list[Any] = [library_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(clean_limit)
    with app_store.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_memory_from_row(row) for row in rows]


def update_memory(
    library_id: str,
    memory_id: str,
    *,
    status: str | None = None,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_store()
    with app_store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_agent_memory WHERE library_id = ? AND memory_id = ?",
            (library_id, memory_id),
        ).fetchone()
        if not row:
            raise ValueError("记忆不存在。")
        current = _memory_from_row(row)
        next_status = str(status or current.get("status") or "suggested").strip().lower()
        if next_status not in {"suggested", "enabled", "disabled"}:
            raise ValueError("不支持的记忆状态。")
        next_content = content if isinstance(content, dict) else current.get("content") or {}
        conn.execute(
            """
            UPDATE retrieval_agent_memory
            SET status = ?, content_json = ?, updated_at = ?
            WHERE library_id = ? AND memory_id = ?
            """,
            (
                next_status,
                json.dumps(next_content, ensure_ascii=False),
                now_iso(),
                library_id,
                memory_id,
            ),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM retrieval_agent_memory WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
    return _memory_from_row(updated)


def delete_memory(library_id: str, memory_id: str) -> bool:
    ensure_store()
    with app_store.connect() as conn:
        result = conn.execute(
            "DELETE FROM retrieval_agent_memory WHERE library_id = ? AND memory_id = ?",
            (library_id, memory_id),
        )
        conn.commit()
    return bool(result.rowcount)


def _feedback_from_row(row: Any) -> dict[str, Any]:
    return dict(row)


def add_feedback(
    library_id: str,
    job_id: str,
    candidate_id: str,
    feedback_type: str,
    *,
    note: str = "",
) -> dict[str, Any]:
    ensure_store()
    allowed = {
        "irrelevant",
        "duplicate",
        "too_old",
        "already_known",
        "accepted",
        "imported",
        "useful_code",
        "weak_metadata",
    }
    clean_type = str(feedback_type or "").strip().lower()
    if clean_type not in allowed:
        raise ValueError("不支持的候选反馈类型。")
    clean_candidate = str(candidate_id or "").strip()
    if not clean_candidate:
        raise ValueError("候选 ID 不能为空。")
    feedback_id = f"agent-feedback-{new_key(14).lower()}"
    with app_store.connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_agent_feedback
              (feedback_id, library_id, job_id, candidate_id, feedback_type, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                library_id,
                job_id,
                clean_candidate,
                clean_type,
                str(note or "").strip(),
                now_iso(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM retrieval_agent_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
    return _feedback_from_row(row)


def list_feedback(library_id: str, job_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    ensure_store()
    clean_limit = max(1, min(int(limit or 200), 500))
    with app_store.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM retrieval_agent_feedback
            WHERE library_id = ? AND job_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (library_id, job_id, clean_limit),
        ).fetchall()
    return [_feedback_from_row(row) for row in rows]


def delete_library_data(library_id: str) -> None:
    ensure_store()
    with app_store.connect() as conn:
        for table in (
            "retrieval_agent_sessions",
            "retrieval_agent_messages",
            "retrieval_agent_turns",
            "retrieval_agent_memory",
            "retrieval_agent_feedback",
        ):
            conn.execute(f"DELETE FROM {table} WHERE library_id = ?", (library_id,))
        conn.commit()
