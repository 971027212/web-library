from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import app_db_path, app_data_dir, libraries_dir
from .semantic_tags import normalize_hash_tag, stable_tag_color
from .utils import new_key, now_iso


GUIDED_JOB_EVENT_LIMIT = 40
GUIDED_JOB_EVENT_MESSAGE_LIMIT = 220


SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
  library_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  mode TEXT NOT NULL,
  source_path TEXT NOT NULL,
  data_path TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
  library_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (library_id, key)
);

CREATE TABLE IF NOT EXISTS semantic_rules (
  rule_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  pattern TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tag_shortcuts (
  library_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  color TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (library_id, tag)
);

CREATE TABLE IF NOT EXISTS sync_journal (
  journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  object_kind TEXT NOT NULL,
  object_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_runs (
  run_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  query TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  source_stats_json TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT 'cjh',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_candidates (
  candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  library_id TEXT NOT NULL,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  identifiers_json TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_provenance (
  provenance_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  candidate_id TEXT NOT NULL DEFAULT '',
  item_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  identifiers_json TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT 'cjh',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_batch_jobs (
  job_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  status TEXT NOT NULL,
  queries_json TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  limit_per_query INTEGER NOT NULL,
  total_queries INTEGER NOT NULL,
  completed_queries INTEGER NOT NULL DEFAULT 0,
  failed_queries INTEGER NOT NULL DEFAULT 0,
  total_candidates INTEGER NOT NULL DEFAULT 0,
  run_ids_json TEXT NOT NULL DEFAULT '[]',
  error TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT 'cjh',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS retrieval_batch_context (
  job_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  context_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_batch_items (
  job_item_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  library_id TEXT NOT NULL,
  query_index INTEGER NOT NULL,
  query TEXT NOT NULL,
  status TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  candidate_count INTEGER NOT NULL DEFAULT 0,
  source_stats_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS retrieval_guided_jobs (
  job_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  status TEXT NOT NULL,
  topic TEXT NOT NULL,
  mode TEXT NOT NULL,
  time_range_json TEXT NOT NULL DEFAULT '{}',
  material_types_json TEXT NOT NULL DEFAULT '[]',
  sources_json TEXT NOT NULL DEFAULT '[]',
  options_json TEXT NOT NULL DEFAULT '{}',
  plan_json TEXT NOT NULL DEFAULT '{}',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  source_stats_json TEXT NOT NULL DEFAULT '{}',
  run_ids_json TEXT NOT NULL DEFAULT '[]',
  progress_json TEXT NOT NULL DEFAULT '{}',
  use_ai_planning INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT 'cjh',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS retrieval_custom_sources (
  source_id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  status_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_checked_at TEXT NOT NULL DEFAULT ''
);
"""


DEFAULT_COLUMNS = [
    "title",
    "creators",
    "year",
    "venue",
    "rating",
    "nested",
    "venue_rank",
    "reading_status",
    "collections",
]

SUPPORTED_COLUMNS = {
    "title",
    "remark",
    "title_zh",
    "abstract_zh",
    "creators",
    "year",
    "venue",
    "rating",
    "nested",
    "venue_rank",
    "reading_status",
    "plain",
    "collections",
}

TAG_SHORTCUTS_INITIALIZED_KEY = "tag_shortcuts_initialized"
LOCAL_COPY_MODE = "local_copy"
RETRIEVAL_LOCAL_PATHS_KEY = "retrieval_local_paths"
RETRIEVAL_HTTP_JSON_CONFIG_KEY = "retrieval_http_json_config"
RETRIEVAL_SQLITE_CONFIG_KEY = "retrieval_sqlite_config"
RETRIEVAL_MANIFEST_CONFIG_KEY = "retrieval_manifest_config"


def ensure_app_store() -> None:
    app_data_dir().mkdir(parents=True, exist_ok=True)
    libraries_dir().mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def connect() -> sqlite3.Connection:
    app_db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def list_libraries() -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = [_portable_library_row(dict(row)) for row in conn.execute("SELECT * FROM libraries ORDER BY updated_at DESC").fetchall()]
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("mode") == LOCAL_COPY_MODE:
            deduped.append(row)
            continue
        key = (row.get("mode", ""), _source_key(row.get("source_path", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def list_all_libraries() -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        return [_portable_library_row(dict(row)) for row in conn.execute("SELECT * FROM libraries ORDER BY updated_at DESC").fetchall()]


def _source_key(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve()).casefold()
    except (OSError, RuntimeError):
        return str(path).casefold()


def get_library(library_id: str) -> dict[str, Any] | None:
    ensure_app_store()
    with connect() as conn:
        row = conn.execute("SELECT * FROM libraries WHERE library_id = ?", (library_id,)).fetchone()
        return _portable_library_row(dict(row)) if row else None


def _portable_library_row(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("mode") != LOCAL_COPY_MODE:
        return row
    portable_path = libraries_dir() / str(row.get("library_id", ""))
    if (portable_path / "zotero.sqlite").exists():
        row["data_path"] = str(portable_path)
        return row
    data_path = Path(str(row.get("data_path", "")))
    if (data_path / "zotero.sqlite").exists():
        return row
    return row


def delete_library_record(library_id: str) -> None:
    ensure_app_store()
    with connect() as conn:
        conn.execute("DELETE FROM libraries WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM preferences WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM semantic_rules WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM tag_shortcuts WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_runs WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_candidates WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM import_provenance WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_batch_context WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_batch_jobs WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_batch_items WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_guided_jobs WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM retrieval_custom_sources WHERE library_id = ?", (library_id,))
        for table in (
            "retrieval_agent_sessions",
            "retrieval_agent_messages",
            "retrieval_agent_turns",
            "retrieval_agent_memory",
            "retrieval_agent_feedback",
        ):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if exists:
                conn.execute(f"DELETE FROM {table} WHERE library_id = ?", (library_id,))
        conn.commit()


def upsert_library(record: dict[str, Any]) -> dict[str, Any]:
    ensure_app_store()
    timestamp = now_iso()
    payload = {
        "library_id": record["library_id"],
        "name": record["name"],
        "mode": record["mode"],
        "source_path": str(record["source_path"]),
        "data_path": str(record["data_path"]),
        "source_fingerprint": record.get("source_fingerprint", ""),
        "created_at": record.get("created_at", timestamp),
        "updated_at": timestamp,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO libraries (library_id, name, mode, source_path, data_path, source_fingerprint, created_at, updated_at)
            VALUES (:library_id, :name, :mode, :source_path, :data_path, :source_fingerprint, :created_at, :updated_at)
            ON CONFLICT(library_id) DO UPDATE SET
              name = excluded.name,
              mode = excluded.mode,
              source_path = excluded.source_path,
              data_path = excluded.data_path,
              source_fingerprint = excluded.source_fingerprint,
              updated_at = excluded.updated_at
            """,
            payload,
        )
        conn.commit()
    return payload


def get_preference(library_id: str, key: str, default: Any = None) -> Any:
    ensure_app_store()
    with connect() as conn:
        row = conn.execute("SELECT value_json FROM preferences WHERE library_id = ? AND key = ?", (library_id, key)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except json.JSONDecodeError:
        return default


def set_preference(library_id: str, key: str, value: Any) -> None:
    ensure_app_store()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO preferences (library_id, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(library_id, key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (library_id, key, json.dumps(value, ensure_ascii=False), now_iso()),
        )
        conn.commit()


def retrieval_local_paths(library_id: str) -> list[str] | None:
    config = retrieval_local_config(library_id)
    if config is None:
        return None
    return [str(item).strip() for item in config.get("paths", []) if str(item).strip()]


def retrieval_local_config(library_id: str) -> dict[str, Any] | None:
    sentinel = {"__missing__": True}
    value = get_preference(library_id, RETRIEVAL_LOCAL_PATHS_KEY, sentinel)
    if value is sentinel:
        return None
    if isinstance(value, list):
        return {"paths": [str(item).strip() for item in value if str(item).strip()], "field_map": {}}
    if not isinstance(value, dict):
        return {"paths": [], "field_map": {}}
    paths = value.get("paths") if isinstance(value.get("paths"), list) else []
    field_map = value.get("field_map") if isinstance(value.get("field_map"), dict) else {}
    return {
        "paths": [str(item).strip() for item in paths if str(item).strip()],
        "field_map": {str(key): raw for key, raw in field_map.items() if str(key).strip() and raw},
    }


def set_retrieval_local_paths(library_id: str, paths: list[str]) -> list[str]:
    existing = retrieval_local_config(library_id) or {}
    config = set_retrieval_local_config(
        library_id,
        {"paths": paths, "field_map": existing.get("field_map") if isinstance(existing.get("field_map"), dict) else {}},
    )
    return [str(item).strip() for item in config.get("paths", []) if str(item).strip()]


def set_retrieval_local_config(library_id: str, config: dict[str, Any]) -> dict[str, Any]:
    raw_paths = config.get("paths") if isinstance(config.get("paths"), list) else []
    raw_field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
    normalized = {
        "paths": [str(path).strip() for path in raw_paths if str(path).strip()],
        "field_map": {str(key): value for key, value in raw_field_map.items() if str(key).strip() and value},
    }
    set_preference(library_id, RETRIEVAL_LOCAL_PATHS_KEY, normalized)
    return normalized


def retrieval_http_json_config(library_id: str) -> dict[str, Any] | None:
    sentinel = {"__missing__": True}
    value = get_preference(library_id, RETRIEVAL_HTTP_JSON_CONFIG_KEY, sentinel)
    if value is sentinel:
        return None
    if not isinstance(value, dict):
        return {}
    return value


def set_retrieval_http_json_config(library_id: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config) if isinstance(config, dict) else {}
    set_preference(library_id, RETRIEVAL_HTTP_JSON_CONFIG_KEY, normalized)
    return normalized


def retrieval_sqlite_config(library_id: str) -> dict[str, Any] | None:
    sentinel = {"__missing__": True}
    value = get_preference(library_id, RETRIEVAL_SQLITE_CONFIG_KEY, sentinel)
    if value is sentinel:
        return None
    if not isinstance(value, dict):
        return {}
    return value


def set_retrieval_sqlite_config(library_id: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config) if isinstance(config, dict) else {}
    set_preference(library_id, RETRIEVAL_SQLITE_CONFIG_KEY, normalized)
    return normalized


def retrieval_manifest_config(library_id: str) -> dict[str, Any] | None:
    sentinel = {"__missing__": True}
    value = get_preference(library_id, RETRIEVAL_MANIFEST_CONFIG_KEY, sentinel)
    if value is sentinel:
        return None
    if not isinstance(value, dict):
        return {}
    return value


def set_retrieval_manifest_config(library_id: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config) if isinstance(config, dict) else {}
    set_preference(library_id, RETRIEVAL_MANIFEST_CONFIG_KEY, normalized)
    return normalized


def list_retrieval_custom_sources(library_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    ensure_app_store()
    where = "WHERE library_id = ?"
    values: list[Any] = [library_id]
    if enabled_only:
        where += " AND enabled = 1"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM retrieval_custom_sources
            {where}
            ORDER BY updated_at DESC, created_at DESC
            """,
            values,
        ).fetchall()
    return [_custom_source_from_row(row) for row in rows]


def get_retrieval_custom_source(library_id: str, source_id: str) -> dict[str, Any] | None:
    ensure_app_store()
    clean_id = str(source_id or "").strip()
    if not clean_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_custom_sources WHERE library_id = ? AND source_id = ?",
            (library_id, clean_id),
        ).fetchone()
    return _custom_source_from_row(row) if row else None


def upsert_retrieval_custom_source(library_id: str, source: dict[str, Any]) -> dict[str, Any]:
    ensure_app_store()
    existing = get_retrieval_custom_source(library_id, str(source.get("source_id") or source.get("id") or ""))
    source_id = existing["source_id"] if existing else _custom_source_id(source.get("source_id") or source.get("id"))
    timestamp = now_iso()
    name = str(source.get("name") or (existing or {}).get("name") or "自定义源").strip()[:120] or "自定义源"
    kind = str(source.get("kind") or (existing or {}).get("kind") or "httpjson").strip().lower()[:40] or "httpjson"
    enabled = bool(source.get("enabled", (existing or {}).get("enabled", True)))
    config = source.get("config") if isinstance(source.get("config"), dict) else (existing or {}).get("config") or {}
    status = source.get("status") if isinstance(source.get("status"), dict) else (existing or {}).get("status") or {}
    created_at = (existing or {}).get("created_at") or timestamp
    last_checked_at = str(source.get("last_checked_at") or (existing or {}).get("last_checked_at") or "")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_custom_sources
              (source_id, library_id, name, kind, enabled, config_json, status_json, created_at, updated_at, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
              name = excluded.name,
              kind = excluded.kind,
              enabled = excluded.enabled,
              config_json = excluded.config_json,
              status_json = excluded.status_json,
              updated_at = excluded.updated_at,
              last_checked_at = excluded.last_checked_at
            """,
            (
                source_id,
                library_id,
                name,
                kind,
                1 if enabled else 0,
                json.dumps(config, ensure_ascii=False),
                json.dumps(status, ensure_ascii=False),
                created_at,
                timestamp,
                last_checked_at,
            ),
        )
        conn.commit()
    stored = get_retrieval_custom_source(library_id, source_id)
    if stored is None:
        raise ValueError("custom source was not saved")
    return stored


def update_retrieval_custom_source_status(
    library_id: str,
    source_id: str,
    status: dict[str, Any],
    *,
    checked: bool = False,
) -> dict[str, Any]:
    ensure_app_store()
    existing = get_retrieval_custom_source(library_id, source_id)
    if not existing:
        raise ValueError("custom source does not exist")
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_custom_sources
            SET status_json = ?, updated_at = ?, last_checked_at = CASE WHEN ? THEN ? ELSE last_checked_at END
            WHERE library_id = ? AND source_id = ?
            """,
            (
                json.dumps(status if isinstance(status, dict) else {}, ensure_ascii=False),
                timestamp,
                1 if checked else 0,
                timestamp,
                library_id,
                source_id,
            ),
        )
        conn.commit()
    stored = get_retrieval_custom_source(library_id, source_id)
    if stored is None:
        raise ValueError("custom source does not exist")
    return stored


def delete_retrieval_custom_source(library_id: str, source_id: str) -> bool:
    ensure_app_store()
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM retrieval_custom_sources WHERE library_id = ? AND source_id = ?",
            (library_id, str(source_id or "").strip()),
        )
        conn.commit()
    return cursor.rowcount > 0


def _custom_source_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item["config"] = _safe_json_dict(item.pop("config_json", "{}"))
    item["status"] = _safe_json_dict(item.pop("status_json", "{}"))
    item["id"] = item.get("source_id", "")
    return item


def _custom_source_id(value: Any = "") -> str:
    raw = str(value or "").strip().lower()
    cleaned = "".join(char if char.isalnum() or char == "-" else "-" for char in raw).strip("-")
    if not cleaned or not cleaned.startswith("custom-"):
        cleaned = f"custom-{new_key(10).lower()}"
    return cleaned[:80]


def _safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def column_preference(library_id: str) -> list[str]:
    value = get_preference(library_id, "columns", DEFAULT_COLUMNS)
    if not isinstance(value, list):
        return DEFAULT_COLUMNS
    columns = [str(item) for item in value if str(item) in SUPPORTED_COLUMNS]
    return columns or DEFAULT_COLUMNS


def column_width_preference(library_id: str) -> dict[str, int]:
    value = get_preference(library_id, "column_widths", {})
    if not isinstance(value, dict):
        return {}
    widths: dict[str, int] = {}
    for key, raw in value.items():
        if str(key) not in SUPPORTED_COLUMNS:
            continue
        try:
            width = int(raw)
        except (TypeError, ValueError):
            continue
        if 40 <= width <= 1200:
            widths[str(key)] = width
    return widths


def plain_tags_collapsed(library_id: str) -> bool:
    return bool(get_preference(library_id, "plain_tags_collapsed", True))


def append_journal(library_id: str, operation: str, object_kind: str, object_key: str, payload: dict[str, Any]) -> None:
    ensure_app_store()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sync_journal (library_id, operation, object_kind, object_key, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (library_id, operation, object_kind, object_key, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        conn.commit()


def list_semantic_rules(library_id: str) -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM semantic_rules WHERE library_id = ? AND enabled = 1 ORDER BY created_at",
            (library_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_semantic_rule(library_id: str, bucket: str, pattern: str, label: str = "") -> dict[str, Any]:
    ensure_app_store()
    rule_id = f"rule-{now_iso()}-{abs(hash((library_id, bucket, pattern))) % 100000}"
    record = {
        "rule_id": rule_id,
        "library_id": library_id,
        "bucket": bucket,
        "pattern": pattern,
        "label": label,
        "enabled": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO semantic_rules (rule_id, library_id, bucket, pattern, label, enabled, created_at, updated_at)
            VALUES (:rule_id, :library_id, :bucket, :pattern, :label, :enabled, :created_at, :updated_at)
            """,
            record,
        )
        conn.commit()
    return record


def list_tag_shortcuts(library_id: str) -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tag_shortcuts WHERE library_id = ? ORDER BY created_at, tag COLLATE NOCASE",
            (library_id,),
        ).fetchall()
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        normalized = normalize_hash_tag(item.get("tag", ""))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        item["tag"] = normalized
        values.append(item)
    return values


def ensure_tag_shortcuts(library_id: str, tags: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    for tag in tags:
        normalized = normalize_hash_tag(tag)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        upsert_tag_shortcut(library_id, normalized, stable_tag_color(normalized))
    return list_tag_shortcuts(library_id)


def tag_shortcuts_initialized(library_id: str) -> bool:
    return bool(get_preference(library_id, TAG_SHORTCUTS_INITIALIZED_KEY, False))


def mark_tag_shortcuts_initialized(library_id: str) -> None:
    set_preference(library_id, TAG_SHORTCUTS_INITIALIZED_KEY, True)


def upsert_tag_shortcut(library_id: str, tag: str, color: str) -> dict[str, Any]:
    ensure_app_store()
    clean_tag = normalize_hash_tag(tag)
    if not clean_tag:
        raise ValueError("标签不能为空。")
    timestamp = now_iso()
    record = {
        "library_id": library_id,
        "tag": clean_tag,
        "color": color,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM tag_shortcuts WHERE library_id = ? AND tag = ?",
            (library_id, clean_tag),
        ).fetchone()
        if existing:
            record["created_at"] = existing["created_at"]
        conn.execute(
            """
            INSERT INTO tag_shortcuts (library_id, tag, color, created_at, updated_at)
            VALUES (:library_id, :tag, :color, :created_at, :updated_at)
            ON CONFLICT(library_id, tag) DO UPDATE SET
              color = excluded.color,
              updated_at = excluded.updated_at
            """,
            record,
        )
        conn.commit()
    return record


def delete_tag_shortcut(library_id: str, tag: str) -> None:
    ensure_app_store()
    normalized = normalize_hash_tag(tag)
    raw_without_hash = normalized[1:] if normalized.startswith("#") else normalized
    with connect() as conn:
        conn.execute(
            "DELETE FROM tag_shortcuts WHERE library_id = ? AND tag IN (?, ?, ?)",
            (library_id, tag, normalized, raw_without_hash),
        )
        conn.commit()


def unsynced_count(library_id: str) -> int:
    ensure_app_store()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM sync_journal WHERE library_id = ? AND status IN ('pending', 'conflicted')",
            (library_id,),
        ).fetchone()
        return int(row["count"] if row else 0)


def pending_journal(library_id: str) -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_journal WHERE library_id = ? AND status IN ('pending', 'conflicted') ORDER BY journal_id",
            (library_id,),
        ).fetchall()
    values = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except json.JSONDecodeError:
            item["payload"] = {}
        values.append(item)
    return values


def mark_conflicted(library_id: str, journal_id: int) -> None:
    ensure_app_store()
    with connect() as conn:
        conn.execute("UPDATE sync_journal SET status = 'conflicted' WHERE library_id = ? AND journal_id = ?", (library_id, journal_id))
        conn.commit()


def create_retrieval_run(
    library_id: str,
    query: str,
    sources: list[str],
    source_stats: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    operator: str = "cjh",
) -> dict[str, Any]:
    ensure_app_store()
    timestamp = now_iso()
    run_id = f"run-{new_key(12).lower()}"
    stored_candidates: list[dict[str, Any]] = []
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_runs (run_id, library_id, query, sources_json, source_stats_json, operator, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                library_id,
                query,
                json.dumps(sources, ensure_ascii=False),
                json.dumps(source_stats, ensure_ascii=False),
                operator,
                timestamp,
            ),
        )
        for candidate in candidates:
            candidate_id = f"cand-{new_key(12).lower()}"
            payload = dict(candidate)
            payload["candidate_id"] = candidate_id
            stored_candidates.append(payload)
            identifiers = payload.get("identifiers") if isinstance(payload.get("identifiers"), dict) else {}
            conn.execute(
                """
                INSERT INTO retrieval_candidates
                  (candidate_id, run_id, library_id, source, external_id, title, identifiers_json, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    run_id,
                    library_id,
                    str(payload.get("source") or ""),
                    str(payload.get("external_id") or ""),
                    str(payload.get("title") or ""),
                    json.dumps(identifiers, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    timestamp,
                ),
            )
        conn.commit()
    return {"run_id": run_id, "candidates": stored_candidates}


def create_retrieval_batch_job(
    library_id: str,
    queries: list[str],
    sources: list[str],
    limit_per_query: int,
    *,
    operator: str = "cjh",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_app_store()
    clean_queries = [str(query or "").strip() for query in queries if str(query or "").strip()]
    if not clean_queries:
        raise ValueError("batch queries cannot be empty")
    clean_sources = [str(source or "").strip() for source in sources if str(source or "").strip()]
    timestamp = now_iso()
    job_id = f"batch-{new_key(12).lower()}"
    limit_value = max(1, min(int(limit_per_query or 10), 50))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_batch_jobs
              (job_id, library_id, status, queries_json, sources_json, limit_per_query, total_queries,
               completed_queries, failed_queries, total_candidates, run_ids_json, error, operator, created_at, updated_at)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, 0, 0, 0, '[]', '', ?, ?, ?)
            """,
            (
                job_id,
                library_id,
                json.dumps(clean_queries, ensure_ascii=False),
                json.dumps(clean_sources, ensure_ascii=False),
                limit_value,
                len(clean_queries),
                operator,
                timestamp,
                timestamp,
            ),
        )
        for index, query in enumerate(clean_queries):
            conn.execute(
                """
                INSERT INTO retrieval_batch_items
                  (job_item_id, job_id, library_id, query_index, query, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (f"batch-item-{new_key(12).lower()}", job_id, library_id, index, query, timestamp, timestamp),
            )
        if isinstance(context, dict) and context:
            conn.execute(
                """
                INSERT OR REPLACE INTO retrieval_batch_context
                  (job_id, library_id, context_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, library_id, json.dumps(context, ensure_ascii=False), timestamp),
            )
        conn.commit()
    return retrieval_batch_job(library_id, job_id)


def _decode_json_field(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except json.JSONDecodeError:
        return fallback


def _guided_job_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    json_defaults = {
        "time_range_json": {},
        "material_types_json": [],
        "sources_json": [],
        "options_json": {},
        "plan_json": {},
        "coverage_json": {},
        "source_stats_json": {},
        "run_ids_json": [],
        "progress_json": {},
    }
    for key, fallback in json_defaults.items():
        item[key.replace("_json", "")] = _decode_json_field(item.pop(key, ""), fallback)
    item["use_ai_planning"] = bool(item.get("use_ai_planning"))
    options = item.get("options") if isinstance(item.get("options"), dict) else {}
    item["search_route"] = str(options.get("search_route") or options.get("route") or "").strip()
    item["input_text"] = str(options.get("input_text") or item.get("topic") or "").strip()
    item["planner_version"] = str(options.get("planner_version") or "").strip()
    item["expansion_level"] = str(options.get("expansion_level") or "").strip()
    item["language_policy"] = str(options.get("language_policy") or "").strip()
    progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
    events = progress.get("events") if isinstance(progress.get("events"), list) else []
    item["events"] = [event for event in events if isinstance(event, dict)][-GUIDED_JOB_EVENT_LIMIT:]
    total = int(progress.get("total_queries") or 0)
    completed = int(progress.get("completed_queries") or 0)
    item["candidate_count"] = int(progress.get("candidate_count") or 0)
    item["progress_ratio"] = round(completed / total, 3) if total else 0
    return item


def _guided_event_payload(message: str, kind: str = "progress", data: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned = " ".join(str(message or "").split())[:GUIDED_JOB_EVENT_MESSAGE_LIMIT].strip()
    if not cleaned:
        return {}
    event = {
        "event_id": f"event-{new_key(10).lower()}",
        "kind": str(kind or "progress").strip().lower() or "progress",
        "message": cleaned,
        "created_at": now_iso(),
    }
    if isinstance(data, dict) and data:
        event["data"] = data
    return event


def _guided_progress_with_event(
    progress: dict[str, Any] | None,
    message: str,
    *,
    kind: str = "progress",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_progress = dict(progress or {}) if isinstance(progress, dict) else {}
    event = _guided_event_payload(message, kind=kind, data=data)
    if not event:
        return next_progress
    events = [item for item in next_progress.get("events") or [] if isinstance(item, dict)]
    latest = events[-1] if events else {}
    if str(latest.get("kind") or "") == event["kind"] and str(latest.get("message") or "") == event["message"]:
        return next_progress
    events.append(event)
    next_progress["events"] = events[-GUIDED_JOB_EVENT_LIMIT:]
    return next_progress


def create_retrieval_guided_job(
    library_id: str,
    *,
    topic: str,
    mode: str,
    time_range: dict[str, Any],
    material_types: list[str],
    sources: list[str],
    options: dict[str, Any],
    use_ai_planning: bool,
    operator: str = "cjh",
) -> dict[str, Any]:
    ensure_app_store()
    clean_topic = str(topic or "").strip()
    if not clean_topic:
        raise ValueError("guided search topic cannot be empty")
    clean_mode = str(mode or "fast").strip().lower()
    timestamp = now_iso()
    job_id = f"guided-{new_key(12).lower()}"
    progress = {"stage": "queued", "total_queries": 0, "completed_queries": 0, "failed_queries": 0, "candidate_count": 0}
    progress = _guided_progress_with_event(progress, "已创建引导式检索任务，等待后台执行。", kind="queued")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_guided_jobs
              (job_id, library_id, status, topic, mode, time_range_json, material_types_json, sources_json,
               options_json, plan_json, coverage_json, source_stats_json, run_ids_json, progress_json,
               use_ai_planning, error, operator, created_at, updated_at)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, '{}', '{}', '{}', '[]', ?, ?, '', ?, ?, ?)
            """,
            (
                job_id,
                library_id,
                clean_topic,
                clean_mode,
                json.dumps(time_range or {}, ensure_ascii=False),
                json.dumps(material_types or [], ensure_ascii=False),
                json.dumps(sources or [], ensure_ascii=False),
                json.dumps(options or {}, ensure_ascii=False),
                json.dumps(progress, ensure_ascii=False),
                1 if use_ai_planning else 0,
                operator,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return retrieval_guided_job(library_id, job_id)


def retrieval_guided_job(library_id: str, job_id: str) -> dict[str, Any]:
    ensure_app_store()
    clean_job_id = str(job_id or "").strip()
    if not clean_job_id:
        raise ValueError("guided search job does not exist")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_guided_jobs WHERE library_id = ? AND job_id = ?",
            (library_id, clean_job_id),
        ).fetchone()
    if not row:
        raise ValueError("guided search job does not exist")
    return _guided_job_from_row(row)


def recent_retrieval_guided_jobs(library_id: str, limit: int = 20) -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM retrieval_guided_jobs
            WHERE library_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (library_id, max(1, min(int(limit or 20), 100))),
        ).fetchall()
    return [_guided_job_from_row(row) for row in rows]


def latest_retrieval_guided_job(library_id: str) -> dict[str, Any] | None:
    jobs = recent_retrieval_guided_jobs(library_id, limit=1)
    return jobs[0] if jobs else None


def update_retrieval_guided_job(
    library_id: str,
    job_id: str,
    *,
    topic: str | None = None,
    status: str | None = None,
    options: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    source_stats: dict[str, Any] | None = None,
    run_ids: list[str] | None = None,
    progress: dict[str, Any] | None = None,
    error: str | None = None,
    started: bool = False,
    finished: bool = False,
) -> dict[str, Any]:
    ensure_app_store()
    retrieval_guided_job(library_id, job_id)
    timestamp = now_iso()
    assignments = ["updated_at = ?"]
    values: list[Any] = [timestamp]
    if topic is not None:
        clean_topic = str(topic or "").strip()
        if not clean_topic:
            raise ValueError("guided search topic cannot be empty")
        assignments.append("topic = ?")
        values.append(clean_topic)
    if status is not None:
        assignments.append("status = ?")
        values.append(str(status))
        if str(status) in {"queued", "running"}:
            assignments.append("finished_at = ''")
    if options is not None:
        assignments.append("options_json = ?")
        values.append(json.dumps(options, ensure_ascii=False))
    if plan is not None:
        assignments.append("plan_json = ?")
        values.append(json.dumps(plan, ensure_ascii=False))
    if coverage is not None:
        assignments.append("coverage_json = ?")
        values.append(json.dumps(coverage, ensure_ascii=False))
    if source_stats is not None:
        assignments.append("source_stats_json = ?")
        values.append(json.dumps(source_stats, ensure_ascii=False))
    if run_ids is not None:
        assignments.append("run_ids_json = ?")
        values.append(json.dumps(run_ids, ensure_ascii=False))
    if progress is not None:
        assignments.append("progress_json = ?")
        values.append(json.dumps(progress, ensure_ascii=False))
    if error is not None:
        assignments.append("error = ?")
        values.append(str(error or ""))
    if started:
        assignments.append("started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END")
        values.append(timestamp)
    if finished:
        assignments.append("finished_at = ?")
        values.append(timestamp)
    values.extend([library_id, job_id])
    with connect() as conn:
        conn.execute(
            f"""
            UPDATE retrieval_guided_jobs
            SET {", ".join(assignments)}
            WHERE library_id = ? AND job_id = ?
            """,
            values,
        )
        conn.commit()
    return retrieval_guided_job(library_id, job_id)


def append_retrieval_guided_event(
    library_id: str,
    job_id: str,
    message: str,
    *,
    kind: str = "progress",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job = retrieval_guided_job(library_id, job_id)
    progress = _guided_progress_with_event(
        job.get("progress") if isinstance(job.get("progress"), dict) else {},
        message,
        kind=kind,
        data=data,
    )
    return update_retrieval_guided_job(library_id, job_id, progress=progress)


def cancel_retrieval_guided_job(library_id: str, job_id: str, reason: str = "Guided search canceled.") -> dict[str, Any]:
    job = retrieval_guided_job(library_id, job_id)
    if job.get("status") not in {"queued", "running", "pausing"}:
        return job
    progress = _guided_progress_with_event(
        {**(job.get("progress") if isinstance(job.get("progress"), dict) else {}), "stage": "canceled"},
        reason,
        kind="warning",
    )
    return update_retrieval_guided_job(
        library_id,
        job_id,
        status="canceled",
        error=reason,
        progress=progress,
        finished=True,
    )


def pause_retrieval_guided_job(library_id: str, job_id: str, reason: str = "Guided search paused.") -> dict[str, Any]:
    job = retrieval_guided_job(library_id, job_id)
    if job.get("status") not in {"queued", "running"}:
        raise ValueError("guided search job cannot be paused")
    progress = _guided_progress_with_event(
        {**(job.get("progress") if isinstance(job.get("progress"), dict) else {}), "stage": "paused"},
        reason,
        kind="warning",
    )
    return update_retrieval_guided_job(
        library_id,
        job_id,
        status="paused",
        error=reason,
        progress=progress,
    )


def resume_retrieval_guided_job(library_id: str, job_id: str) -> dict[str, Any]:
    job = retrieval_guided_job(library_id, job_id)
    if job.get("status") != "paused":
        raise ValueError("guided search job is not paused")
    progress = _guided_progress_with_event(
        {**(job.get("progress") if isinstance(job.get("progress"), dict) else {}), "stage": "queued"},
        "已恢复引导式检索任务，等待继续执行。",
        kind="progress",
    )
    return update_retrieval_guided_job(
        library_id,
        job_id,
        status="queued",
        error="",
        progress=progress,
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds_between(start: Any, end: Any) -> float | None:
    start_dt = _parse_iso_datetime(start)
    end_dt = _parse_iso_datetime(end)
    if not start_dt or not end_dt:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _batch_job_timing(item: dict[str, Any]) -> dict[str, Any]:
    items = item.get("items") or []
    total = int(item.get("total_queries") or 0)
    completed = int(item.get("completed_queries") or 0)
    terminal_statuses = {"completed", "failed", "canceled"}
    remaining = max(0, total - completed)
    durations = [
        duration
        for duration in (
            _seconds_between(row.get("started_at"), row.get("finished_at"))
            for row in items
            if row.get("status") in {"completed", "failed"} and row.get("started_at") and row.get("finished_at")
        )
        if duration is not None
    ]
    average_seconds = round(sum(durations) / len(durations), 2) if durations else 0.0
    if not average_seconds and completed and item.get("started_at"):
        end_value = item.get("finished_at") if item.get("finished_at") else now_iso()
        elapsed = _seconds_between(item.get("started_at"), end_value)
        if elapsed is not None:
            average_seconds = round(elapsed / completed, 2)
    eta_seconds = int(round(average_seconds * remaining)) if average_seconds and remaining else 0
    active_count = sum(1 for row in items if row.get("status") not in terminal_statuses)
    return {
        "remaining_queries": remaining,
        "active_queries": active_count,
        "average_seconds_per_completed_query": average_seconds,
        "eta_seconds": eta_seconds,
    }


def _batch_job_from_row(row: sqlite3.Row | dict[str, Any], *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    item = dict(row)
    item["queries"] = _decode_json_field(item.pop("queries_json", "[]"), [])
    item["sources"] = _decode_json_field(item.pop("sources_json", "[]"), [])
    item["run_ids"] = _decode_json_field(item.pop("run_ids_json", "[]"), [])
    item["items"] = items or []
    total = int(item.get("total_queries") or 0)
    completed = int(item.get("completed_queries") or 0)
    item["progress"] = round(completed / total, 3) if total else 0
    item.update(_batch_job_timing(item))
    return item


def _batch_item_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["source_stats"] = _decode_json_field(item.pop("source_stats_json", "{}"), {})
    return item


def retrieval_batch_contexts_for_jobs(library_id: str, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    ensure_app_store()
    clean_job_ids = []
    seen: set[str] = set()
    for job_id in job_ids:
        clean_job_id = str(job_id or "").strip()
        if clean_job_id and clean_job_id not in seen:
            clean_job_ids.append(clean_job_id)
            seen.add(clean_job_id)
    if not clean_job_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_job_ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT job_id, context_json
            FROM retrieval_batch_context
            WHERE library_id = ? AND job_id IN ({placeholders})
            """,
            (library_id, *clean_job_ids),
        ).fetchall()
    contexts: dict[str, dict[str, Any]] = {}
    for row in rows:
        context = _decode_json_field(row["context_json"], {})
        if isinstance(context, dict):
            contexts[str(row["job_id"])] = context
    return contexts


def retrieval_batch_job(library_id: str, job_id: str) -> dict[str, Any]:
    ensure_app_store()
    clean_job_id = str(job_id or "").strip()
    if not clean_job_id:
        raise ValueError("batch job does not exist")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_batch_jobs WHERE library_id = ? AND job_id = ?",
            (library_id, clean_job_id),
        ).fetchone()
        if not row:
            raise ValueError("batch job does not exist")
        item_rows = conn.execute(
            """
            SELECT *
            FROM retrieval_batch_items
            WHERE library_id = ? AND job_id = ?
            ORDER BY query_index
            """,
            (library_id, clean_job_id),
        ).fetchall()
    job = _batch_job_from_row(row, items=[_batch_item_from_row(item) for item in item_rows])
    job["context"] = retrieval_batch_contexts_for_jobs(library_id, [clean_job_id]).get(clean_job_id, {})
    return job


def recent_retrieval_batch_jobs(library_id: str, limit: int = 20) -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM retrieval_batch_jobs
            WHERE library_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (library_id, max(1, min(int(limit or 20), 100))),
        ).fetchall()
    jobs = [_batch_job_from_row(row) for row in rows]
    contexts = retrieval_batch_contexts_for_jobs(library_id, [str(job.get("job_id") or "") for job in jobs])
    for job in jobs:
        job["context"] = contexts.get(str(job.get("job_id") or ""), {})
    return jobs


def retrieval_batch_items_for_job(library_id: str, job_id: str) -> list[dict[str, Any]]:
    return retrieval_batch_job(library_id, job_id)["items"]


def mark_retrieval_batch_job_running(library_id: str, job_id: str) -> None:
    ensure_app_store()
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET status = 'running', updated_at = ?, started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END
            WHERE library_id = ? AND job_id = ? AND status IN ('queued', 'running')
            """,
            (timestamp, timestamp, library_id, job_id),
        )
        conn.commit()


def mark_retrieval_batch_job_finished(library_id: str, job_id: str, status: str = "completed", error: str = "") -> None:
    ensure_app_store()
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET status = ?, error = ?, updated_at = ?, finished_at = ?
            WHERE library_id = ? AND job_id = ?
            """,
            (status, str(error or ""), timestamp, timestamp, library_id, job_id),
        )
        conn.commit()
    refresh_retrieval_batch_job_progress(library_id, job_id)


def cancel_retrieval_batch_job(library_id: str, job_id: str, reason: str = "Batch retrieval canceled.") -> dict[str, Any]:
    ensure_app_store()
    retrieval_batch_job(library_id, job_id)
    timestamp = now_iso()
    message = str(reason or "Batch retrieval canceled.")
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET status = 'canceled', error = ?, updated_at = ?, finished_at = ?
            WHERE library_id = ? AND job_id = ? AND status IN ('queued', 'running')
            """,
            (message, timestamp, timestamp, library_id, job_id),
        )
        conn.execute(
            """
            UPDATE retrieval_batch_items
            SET status = 'canceled', error = ?, updated_at = ?, finished_at = ?
            WHERE library_id = ? AND job_id = ? AND status = 'queued'
            """,
            (message, timestamp, timestamp, library_id, job_id),
        )
        conn.commit()
    refresh_retrieval_batch_job_progress(library_id, job_id)
    return retrieval_batch_job(library_id, job_id)


def pause_retrieval_batch_job(library_id: str, job_id: str, reason: str = "Batch retrieval paused.") -> dict[str, Any]:
    ensure_app_store()
    job = retrieval_batch_job(library_id, job_id)
    if job.get("status") not in {"queued", "running"}:
        raise ValueError("batch job cannot be paused")
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET status = 'paused', error = ?, updated_at = ?
            WHERE library_id = ? AND job_id = ? AND status IN ('queued', 'running')
            """,
            (str(reason or "Batch retrieval paused."), timestamp, library_id, job_id),
        )
        conn.commit()
    refresh_retrieval_batch_job_progress(library_id, job_id)
    return retrieval_batch_job(library_id, job_id)


def resume_retrieval_batch_job(library_id: str, job_id: str) -> dict[str, Any]:
    ensure_app_store()
    job = retrieval_batch_job(library_id, job_id)
    if job.get("status") != "paused":
        raise ValueError("batch job is not paused")
    if not [item for item in job.get("items", []) if item.get("status") in {"queued", "running"}]:
        raise ValueError("batch job has no remaining queries to resume")
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET status = 'queued', error = '', updated_at = ?, finished_at = ''
            WHERE library_id = ? AND job_id = ? AND status = 'paused'
            """,
            (timestamp, library_id, job_id),
        )
        conn.commit()
    refresh_retrieval_batch_job_progress(library_id, job_id)
    return retrieval_batch_job(library_id, job_id)


def retry_failed_retrieval_batch_job(library_id: str, job_id: str) -> dict[str, Any]:
    ensure_app_store()
    job = retrieval_batch_job(library_id, job_id)
    if job.get("status") in {"queued", "running"}:
        raise ValueError("batch job is still running")
    failed_items = [item for item in job.get("items", []) if item.get("status") == "failed"]
    if not failed_items:
        raise ValueError("batch job has no failed queries to retry")
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_items
            SET status = 'queued',
                run_id = '',
                candidate_count = 0,
                source_stats_json = '{}',
                error = '',
                updated_at = ?,
                started_at = '',
                finished_at = ''
            WHERE library_id = ? AND job_id = ? AND status = 'failed'
            """,
            (timestamp, library_id, job_id),
        )
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET status = 'queued', error = '', updated_at = ?, finished_at = ''
            WHERE library_id = ? AND job_id = ?
            """,
            (timestamp, library_id, job_id),
        )
        conn.commit()
    refresh_retrieval_batch_job_progress(library_id, job_id)
    return retrieval_batch_job(library_id, job_id)


def mark_retrieval_batch_item_running(library_id: str, job_item_id: str) -> bool:
    ensure_app_store()
    timestamp = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE retrieval_batch_items
            SET status = 'running', updated_at = ?, started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END
            WHERE library_id = ? AND job_item_id = ? AND status IN ('queued', 'failed')
            """,
            (timestamp, timestamp, library_id, job_item_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def complete_retrieval_batch_item(
    library_id: str,
    job_item_id: str,
    *,
    status: str,
    run_id: str = "",
    candidate_count: int = 0,
    source_stats: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    ensure_app_store()
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE retrieval_batch_items
            SET status = ?, run_id = ?, candidate_count = ?, source_stats_json = ?, error = ?, updated_at = ?, finished_at = ?
            WHERE library_id = ? AND job_item_id = ?
            """,
            (
                status,
                str(run_id or ""),
                max(0, int(candidate_count or 0)),
                json.dumps(source_stats or {}, ensure_ascii=False),
                str(error or ""),
                timestamp,
                timestamp,
                library_id,
                job_item_id,
            ),
        )
        conn.commit()


def refresh_retrieval_batch_job_progress(library_id: str, job_id: str) -> None:
    ensure_app_store()
    timestamp = now_iso()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, run_id, candidate_count
            FROM retrieval_batch_items
            WHERE library_id = ? AND job_id = ?
            """,
            (library_id, job_id),
        ).fetchall()
        completed = sum(1 for row in rows if row["status"] in {"completed", "failed", "canceled"})
        failed = sum(1 for row in rows if row["status"] == "failed")
        candidates = sum(int(row["candidate_count"] or 0) for row in rows)
        run_ids = [str(row["run_id"]) for row in rows if str(row["run_id"] or "")]
        conn.execute(
            """
            UPDATE retrieval_batch_jobs
            SET completed_queries = ?, failed_queries = ?, total_candidates = ?, run_ids_json = ?, updated_at = ?
            WHERE library_id = ? AND job_id = ?
            """,
            (completed, failed, candidates, json.dumps(run_ids, ensure_ascii=False), timestamp, library_id, job_id),
        )
        conn.commit()


def retrieval_candidates_for_import(library_id: str, run_id: str, candidate_ids: list[str]) -> list[dict[str, Any]]:
    ensure_app_store()
    clean_run_id = str(run_id or "").strip()
    ids = [str(candidate_id or "").strip() for candidate_id in candidate_ids if str(candidate_id or "").strip()]
    if not clean_run_id or not ids:
        raise ValueError("请选择要导入的检索候选。")
    placeholders = ", ".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT candidate_id, payload_json
            FROM retrieval_candidates
            WHERE library_id = ? AND run_id = ? AND candidate_id IN ({placeholders})
            """,
            [library_id, clean_run_id, *ids],
        ).fetchall()
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            by_id[str(row["candidate_id"])] = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            by_id[str(row["candidate_id"])] = {}
    missing = [candidate_id for candidate_id in ids if candidate_id not in by_id]
    if missing:
        raise ValueError("部分检索候选已失效，请重新检索。")
    return [by_id[candidate_id] for candidate_id in ids]


def record_import_provenance(
    library_id: str,
    run_id: str,
    candidates: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    operator: str = "cjh",
) -> None:
    ensure_app_store()
    timestamp = now_iso()
    with connect() as conn:
        for candidate, result in zip(candidates, results):
            identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
            payload = {
                "candidate": {
                    "candidate_id": candidate.get("candidate_id", ""),
                    "title": candidate.get("title", ""),
                    "source": candidate.get("source", ""),
                    "external_id": candidate.get("external_id", ""),
                    "identifiers": identifiers,
                },
                "result": result,
            }
            conn.execute(
                """
                INSERT INTO import_provenance
                  (provenance_id, library_id, run_id, candidate_id, item_key, status, source, identifiers_json, payload_json, operator, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"prov-{new_key(12).lower()}",
                    library_id,
                    str(run_id or ""),
                    str(candidate.get("candidate_id") or ""),
                    str(result.get("item_key") or ""),
                    str(result.get("status") or ""),
                    str(candidate.get("source") or result.get("source") or ""),
                    json.dumps(identifiers, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    operator,
                    timestamp,
                ),
            )
        conn.commit()


def recent_retrieval_runs(library_id: str, limit: int = 20) -> list[dict[str, Any]]:
    ensure_app_store()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*,
              (SELECT COUNT(*) FROM retrieval_candidates c WHERE c.run_id = r.run_id AND c.library_id = r.library_id) AS candidate_count,
              (SELECT COUNT(*) FROM import_provenance p WHERE p.run_id = r.run_id AND p.library_id = r.library_id) AS imported_count
            FROM retrieval_runs r
            WHERE r.library_id = ?
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (library_id, max(1, min(int(limit or 20), 100))),
        ).fetchall()
    values: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("sources_json", "source_stats_json"):
            try:
                item[key.replace("_json", "")] = json.loads(item.pop(key))
            except json.JSONDecodeError:
                item[key.replace("_json", "")] = [] if key == "sources_json" else {}
        values.append(item)
    return values


def retrieval_run_summary(library_id: str, limit: int = 100) -> dict[str, Any]:
    runs = recent_retrieval_runs(library_id, limit=limit)
    totals = {
        "run_count": len(runs),
        "candidate_count": 0,
        "imported_count": 0,
        "source_attempt_count": 0,
        "source_success_count": 0,
        "source_failure_count": 0,
    }
    source_totals: dict[str, dict[str, Any]] = {}
    error_kinds: dict[str, int] = {}
    query_counts: dict[str, int] = {}
    latest_run_at = ""
    earliest_run_at = ""
    for run in runs:
        totals["candidate_count"] += int(run.get("candidate_count") or 0)
        totals["imported_count"] += int(run.get("imported_count") or 0)
        created_at = str(run.get("created_at") or "")
        if created_at:
            latest_run_at = max(latest_run_at, created_at) if latest_run_at else created_at
            earliest_run_at = min(earliest_run_at, created_at) if earliest_run_at else created_at
        query = str(run.get("query") or "").strip()
        if query:
            query_counts[query] = query_counts.get(query, 0) + 1
        source_stats = run.get("source_stats") if isinstance(run.get("source_stats"), dict) else {}
        sources = run.get("sources") if isinstance(run.get("sources"), list) else list(source_stats)
        for source in [str(item or "").strip() for item in sources if str(item or "").strip()]:
            stats = source_stats.get(source) if isinstance(source_stats.get(source), dict) else {}
            item = source_totals.setdefault(
                source,
                {
                    "run_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "candidate_count": 0,
                    "elapsed_total_ms": 0,
                    "elapsed_avg_ms": 0,
                    "rate_limit_wait_total_ms": 0,
                    "rate_limit_wait_avg_ms": 0,
                    "observed_rate_limit_seconds": 0,
                    "error_kinds": {},
                    "last_error": "",
                    "last_action": "",
                },
            )
            item["run_count"] += 1
            totals["source_attempt_count"] += 1
            if stats.get("ok") is False:
                item["failure_count"] += 1
                totals["source_failure_count"] += 1
                kind = str(stats.get("error_kind") or "failed")
                item["error_kinds"][kind] = item["error_kinds"].get(kind, 0) + 1
                error_kinds[kind] = error_kinds.get(kind, 0) + 1
                item["last_error"] = str(stats.get("error") or "")
                item["last_action"] = str(stats.get("action") or "")
            else:
                item["success_count"] += 1
                totals["source_success_count"] += 1
            item["candidate_count"] += int(stats.get("count") or 0)
            item["elapsed_total_ms"] += int(stats.get("elapsed_ms") or 0)
            item["rate_limit_wait_total_ms"] += int(stats.get("rate_limit_wait_ms") or 0)
            try:
                observed_rate_limit = float(stats.get("rate_limit_seconds") or 0)
            except (TypeError, ValueError):
                observed_rate_limit = 0
            if observed_rate_limit:
                item["observed_rate_limit_seconds"] = observed_rate_limit
    for item in source_totals.values():
        item["elapsed_avg_ms"] = round(item["elapsed_total_ms"] / item["run_count"]) if item["run_count"] else 0
        item["rate_limit_wait_avg_ms"] = (
            round(item["rate_limit_wait_total_ms"] / item["run_count"]) if item["run_count"] else 0
        )
    top_queries = [
        {"query": query, "count": count}
        for query, count in sorted(query_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    ]
    totals["import_rate"] = round(totals["imported_count"] / totals["candidate_count"], 3) if totals["candidate_count"] else 0
    totals["source_success_rate"] = (
        round(totals["source_success_count"] / totals["source_attempt_count"], 3) if totals["source_attempt_count"] else 0
    )
    return {
        "limit": max(1, min(int(limit or 100), 500)),
        "generated_at": now_iso(),
        "earliest_run_at": earliest_run_at,
        "latest_run_at": latest_run_at,
        "totals": totals,
        "sources": source_totals,
        "error_kinds": error_kinds,
        "top_queries": top_queries,
    }


def retrieval_run_report(library_id: str, run_id: str) -> dict[str, Any]:
    ensure_app_store()
    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        raise ValueError("检索批次不存在。")
    with connect() as conn:
        run = conn.execute(
            """
            SELECT *
            FROM retrieval_runs
            WHERE library_id = ? AND run_id = ?
            """,
            (library_id, clean_run_id),
        ).fetchone()
        if not run:
            raise ValueError("检索批次不存在。")
        candidate_rows = conn.execute(
            """
            SELECT *
            FROM retrieval_candidates
            WHERE library_id = ? AND run_id = ?
            ORDER BY created_at, candidate_id
            """,
            (library_id, clean_run_id),
        ).fetchall()
        provenance_rows = conn.execute(
            """
            SELECT *
            FROM import_provenance
            WHERE library_id = ? AND run_id = ?
            ORDER BY created_at, provenance_id
            """,
            (library_id, clean_run_id),
        ).fetchall()
    run_payload = dict(run)
    for key in ("sources_json", "source_stats_json"):
        try:
            run_payload[key.replace("_json", "")] = json.loads(run_payload.pop(key))
        except json.JSONDecodeError:
            run_payload[key.replace("_json", "")] = [] if key == "sources_json" else {}
    candidates = []
    for row in candidate_rows:
        candidate = dict(row)
        for key in ("identifiers_json", "payload_json"):
            try:
                candidate[key.replace("_json", "")] = json.loads(candidate.pop(key))
            except json.JSONDecodeError:
                candidate[key.replace("_json", "")] = {} if key == "identifiers_json" else {}
        candidates.append(candidate)
    imports = []
    for row in provenance_rows:
        item = dict(row)
        for key in ("identifiers_json", "payload_json"):
            try:
                item[key.replace("_json", "")] = json.loads(item.pop(key))
            except json.JSONDecodeError:
                item[key.replace("_json", "")] = {}
        imports.append(item)
    return {"run": run_payload, "candidates": candidates, "imports": imports}


def app_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()
