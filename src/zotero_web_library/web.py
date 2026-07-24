from __future__ import annotations

import csv
import copy
import datetime
import hashlib
import http.client
import io
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib.parse import quote, urlencode, urlsplit
from urllib import request as urllib_request

from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

from . import app_store
from .citation_export import CitationExportError, export_citations, export_filename
from .codex_agent import (
    run_codex_connectivity_probe,
    run_codex_prompt,
    run_reading_chat_turn,
    recommend_matrix_fields,
    run_reading_matrix_for_item,
)
from .metadata_import import MetadataImportError, parse_import_text, resolve_identifier
from .paths import app_data_dir
from .rag import (
    add_knowledge_base_items as rag_add_knowledge_base_items,
    chunk_read as rag_chunk_read,
    create_knowledge_base as rag_create_knowledge_base,
    delete_knowledge_base as rag_delete_knowledge_base,
    embedding_config as rag_embedding_config,
    embed_missing_chunks as rag_embed_missing_chunks,
    embedding_status as rag_embedding_status,
    index_library as rag_index_library,
    index_mineru_results as rag_index_mineru_results,
    index_status as rag_index_status,
    keyword_search as rag_keyword_search,
    knowledge_base as rag_knowledge_base,
    list_knowledge_bases as rag_list_knowledge_bases,
    metadata_search as rag_metadata_search,
    remove_knowledge_base_items as rag_remove_knowledge_base_items,
    retrieve as rag_retrieve,
    run_agentic_chat as rag_run_agentic_chat,
    save_embedding_config as rag_save_embedding_config,
    semantic_search as rag_semantic_search,
)
from .rag.agent.client import missing_model_config_fields, normalize_openai_base_url
from .rag.agent.jobs import cancel_agent_chat_job, restart_agent_chat_job, start_agentic_chat_job
from .rag.agent.memory import load_conversation
from .rag.agent.runtime import load_agent_events, load_agent_run, reconcile_interrupted_runs
from .retrieval import CandidateImportError, RetrievalError, imported_items_from_candidates, retrieval_source_statuses, search_retrieval
from .retrieval.importing import imported_item_from_candidate
from .retrieval.models import SearchOptions, candidate_material_type, normalized_material_type
from .retrieval.providers import (
    AI_PIXEL_BASE_URL_ENV,
    AI_PIXEL_API_KEY_ENV,
    AI_PIXEL_DEFAULT_BASE_URL,
    AI_PIXEL_DEFAULT_MODEL,
    AI_PIXEL_MODEL_ENV,
    BRAVE_SEARCH_API_KEY_ENV,
    CustomSourceProvider,
    GITLAB_TOKEN_ENV,
    HTTP_JSON_CONFIG_ENV,
    MANIFEST_CONFIG_ENV,
    ai_pixel_chat_json,
    configured_local_file_paths,
    default_provider_registry,
    http_json_config,
    http_json_config_summary,
    http_json_config_templates,
    http_json_sample_items,
    iter_local_file_rows,
    local_value_to_text,
    manifest_config,
    manifest_config_summary,
    manifest_config_templates,
    manifest_sample_items,
    preview_http_json_mappings,
    preview_local_file_mappings,
    preview_manifest_mappings,
    preview_sqlite_mappings,
    retrieval_field_map_suggestion_from_payload,
    retrieval_field_map_targets,
    retrieval_error_details,
    retrieval_model_health_check,
    retrieval_model_status,
    normalized_ai_pixel_chat_url,
    split_local_path_config,
    SQLITE_CONFIG_ENV,
    sqlite_config,
    sqlite_config_summary,
    sqlite_config_templates,
    suggest_http_json_field_map,
    suggest_local_file_field_map,
    suggest_manifest_field_map,
    suggest_sqlite_field_map,
    use_ai_pixel_config,
)
from .retrieval.rehearsal import write_retrieval_rehearsal_kit
from .retrieval_agent import (
    apply_decision_to_state,
    build_agent_prompt,
    coverage_state_from_guided,
    default_agent_state,
    enforce_clarification_before_search,
    feedback_summary,
    mark_turn_failed,
    mark_turn_running,
    merge_intent_model,
    normalize_agent_decision,
    normalize_agent_state,
    update_pending_action,
)
from . import retrieval_agent_store
from .semantic_tags import normalize_hash_tag, stable_tag_color
from .sources import (
    SourceError,
    create_local_copy,
    create_local_copy_from_uploads,
    create_read_only_source,
    default_service_source_path,
    delete_source,
    list_server_directory,
    server_path_roots,
)
from .sync import mark_conflicts_for_changed_keys, prepare_sync_payloads
from .utils import now_iso
from .zotero_adapter import ZoteroRepository


RETRIEVAL_BATCH_LOCK = threading.Lock()
RUNNING_RETRIEVAL_BATCHES: set[str] = set()
RETRIEVAL_GUIDED_LOCK = threading.Lock()
RUNNING_RETRIEVAL_GUIDED_JOBS: set[str] = set()
RETRIEVAL_AGENT_LOCK = threading.Lock()
RUNNING_RETRIEVAL_AGENT_TURNS: set[str] = set()
RETRIEVAL_BACKGROUND_LOCK = threading.Lock()
RUNNING_RETRIEVAL_QUERY_PLAN_JOBS: set[str] = set()
RUNNING_RETRIEVAL_AI_SCORING_JOBS: set[str] = set()
RUNNING_RETRIEVAL_SEARCH_JOBS: set[str] = set()
RETRIEVAL_QUERY_PLAN_JOBS: dict[str, dict[str, Any]] = {}
RETRIEVAL_AI_SCORING_JOBS: dict[str, dict[str, Any]] = {}
RETRIEVAL_SEARCH_JOBS: dict[str, dict[str, Any]] = {}
RETRIEVAL_BACKGROUND_JOB_HISTORY_LIMIT = 30
RETRIEVAL_CONFIG_BUNDLE_SCHEMA = "web-library.retrieval-config-bundle/v1"
RETRIEVAL_BATCH_CONTEXT_SCHEMA = "web-library.retrieval-batch-context/v1"
RETRIEVAL_CONFIG_BUNDLE_REDACTED_VALUE = "__REDACTED__"

# 单篇文献研读对话：异步任务 + 轮询 + 停止 + 持久化。
READING_CHAT_LOCK = threading.Lock()
READING_CHAT_TASKS: dict[str, dict[str, Any]] = {}
READING_CHAT_HISTORY_LIMIT = 30
API_CONFIG_PREFERENCE_KEY = "api_config"
API_CONFIG_SECRET_KEEP_VALUE = "__KEEP_SECRET__"
MINERU_API_KEY_ENV = "MINERU_API_KEY"
MINERU_BASE_URL_ENV = "MINERU_BASE_URL"
MINERU_DEFAULT_BASE_URL = "https://mineru.net/api/v4/file-urls/batch"
MINERU_REQUEST_TIMEOUT_SECONDS = 180
MINERU_PARSE_POLL_INTERVAL_SECONDS = 3
CODEX_DEFAULT_BASE_URL = "https://api.openai.com/v1"
RETRIEVAL_BATCH_VALIDATION_MIN_COMPLETED_QUERIES = 3
AI_CANDIDATE_EVALUATION_BATCH_SIZE = 5
AI_CANDIDATE_EVALUATION_TIMEOUT_SECONDS = 90
AI_CANDIDATE_MANUAL_EVALUATION_LIMIT = 10
AI_CANDIDATE_METADATA_ABSTRACT_LIMIT = 600
AI_CANDIDATE_SCORE_FRAMEWORK = "ai_rubric_v1"
RETRIEVAL_PLANNER_TIMEOUT_SECONDS = 90
RETRIEVAL_PLANNER_RETRY_TIMEOUT_SECONDS = 45
RETRIEVAL_PLANNER_MATERIAL_TIMEOUT_SECONDS = 30
DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK = "metadata_rules_v1"
SENSITIVE_CONFIG_KEY_RE = re.compile(
    r"(authorization|api[-_ ]?key|token|secret|password|credential|bearer)",
    re.I,
)
ENV_REFERENCE_RE = re.compile(r"^\$\{ENV:[A-Za-z_][A-Za-z0-9_]*\}$")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def normalize_retrieval_report_format(value: str) -> str:
    normalized = str(value or "markdown").strip().lower()
    aliases = {"md": "markdown", "text": "markdown"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"markdown", "csv", "json"}:
        raise ValueError("不支持的报告格式。")
    return normalized


def normalize_retrieval_batch_report_scope(value: str) -> str:
    normalized = str(value or "queries").strip().lower()
    aliases = {"query": "queries", "items": "queries", "source": "sources", "source_evidence": "sources"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"queries", "sources"}:
        raise ValueError("不支持的批量报告范围。")
    return normalized


def clean_secret(value: Any) -> str:
    return str(value or "").strip()


def api_config_for_library(library_id: str) -> dict[str, Any]:
    value = app_store.get_preference(library_id, API_CONFIG_PREFERENCE_KEY, {})
    return value if isinstance(value, dict) else {}


def api_config_model_for_library(library_id: str) -> dict[str, str]:
    config = api_config_for_library(library_id)
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    return {
        "model": clean_secret(model.get("model") or model.get("model_name")),
        "base_url": clean_secret(model.get("base_url") or model.get("request_url") or model.get("url")),
        "api_key": clean_secret(model.get("api_key") or model.get("key")),
    }


def api_config_tokens_for_library(library_id: str) -> dict[str, str]:
    config = api_config_for_library(library_id)
    code_sources = config.get("code_sources") if isinstance(config.get("code_sources"), dict) else {}
    return {
        "github_token": clean_secret(code_sources.get("github_token") or code_sources.get("github")),
        "huggingface_token": clean_secret(code_sources.get("huggingface_token") or code_sources.get("huggingface")),
        "zenodo_token": clean_secret(code_sources.get("zenodo_token") or code_sources.get("zenodo")),
        "gitlab_token": clean_secret(code_sources.get("gitlab_token") or code_sources.get("gitlab")),
        "brave_search_token": clean_secret(
            code_sources.get("brave_search_token")
            or code_sources.get("brave_token")
            or code_sources.get("brave")
        ),
    }


def api_config_mineru_for_library(library_id: str) -> dict[str, str]:
    config = api_config_for_library(library_id)
    mineru = config.get("mineru") if isinstance(config.get("mineru"), dict) else {}
    return {
        "base_url": clean_secret(mineru.get("base_url") or mineru.get("url")),
        "api_key": clean_secret(mineru.get("api_key") or mineru.get("key")),
    }


def default_codex_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    return {
        "model": clean_secret(payload.get("model")),
        "base_url": clean_secret(payload.get("base_url")) or CODEX_DEFAULT_BASE_URL,
        "api_key": clean_secret(payload.get("api_key")),
        "reasoning_effort_default": clean_secret(payload.get("reasoning_effort_default")) or "medium",
    }


def api_config_codex_for_library(library_id: str) -> dict[str, Any]:
    config = api_config_for_library(library_id)
    codex = config.get("codex") if isinstance(config.get("codex"), dict) else {}
    return default_codex_config(codex)


def library_embedding_config_response(library: dict[str, Any], *, include_secrets: bool = False) -> dict[str, Any]:
    config = rag_embedding_config(library)
    api_key = clean_secret(config.get("api_key"))
    provider = clean_secret(config.get("provider")) or "openai"
    model = clean_secret(config.get("model")) or "text-embedding-3-small"
    base_url = clean_secret(config.get("base_url")) or CODEX_DEFAULT_BASE_URL
    return {
        "enabled": bool(config.get("enabled")),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key if include_secrets else "",
        "masked_api_key": masked_secret(api_key),
        "configured": bool(config.get("enabled") and provider and model and (provider != "openai" or api_key)),
        "source": "preference" if api_key else "none",
        "dim": config.get("dim"),
        "batch_size": int(config.get("batch_size") or 64),
        "vector_store_type": clean_secret(config.get("vector_store_type")) or "sqlite_blob",
    }


def effective_code_source_token(library_id: str, key: str, env_name: str) -> str:
    configured = api_config_tokens_for_library(library_id).get(key, "")
    return configured or clean_secret(os.environ.get(env_name))


def secret_config_source(saved: str, env_name: str) -> str:
    if saved:
        return "preference"
    if clean_secret(os.environ.get(env_name)):
        return "environment"
    return "none"


def masked_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "******"
    return f"{value[:3]}****{value[-4:]}"


def normalized_library_api_config(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing if isinstance(existing, dict) else {}
    existing_model = existing.get("model") if isinstance(existing.get("model"), dict) else {}
    existing_sources = existing.get("code_sources") if isinstance(existing.get("code_sources"), dict) else {}
    existing_mineru = existing.get("mineru") if isinstance(existing.get("mineru"), dict) else {}
    has_existing_codex = isinstance(existing.get("codex"), dict)
    existing_codex = default_codex_config(existing.get("codex"))
    has_model_payload = isinstance(payload.get("model"), dict) or any(
        key in payload for key in ("model", "model_name", "base_url", "request_url", "url", "api_key", "key")
    )
    has_source_payload = isinstance(payload.get("code_sources"), dict) or any(
        key in payload
        for key in (
            "github_token",
            "github",
            "huggingface_token",
            "huggingface",
            "zenodo_token",
            "zenodo",
            "gitlab_token",
            "gitlab",
            "brave_search_token",
            "brave_token",
            "brave",
        )
    )
    has_mineru_payload = isinstance(payload.get("mineru"), dict)
    has_codex_payload = isinstance(payload.get("codex"), dict)
    raw_model = payload.get("model") if isinstance(payload.get("model"), dict) else (payload if has_model_payload else existing_model)
    raw_sources = payload.get("code_sources") if isinstance(payload.get("code_sources"), dict) else (payload if has_source_payload else existing_sources)
    raw_mineru = payload.get("mineru") if isinstance(payload.get("mineru"), dict) else (payload if has_mineru_payload else existing_mineru)
    raw_codex = payload.get("codex") if has_codex_payload else existing_codex

    def next_secret(field: str, current: str = "", *aliases: str) -> str:
        value = ""
        for candidate_field in (field, *aliases):
            value = clean_secret(raw_model.get(candidate_field) if candidate_field in raw_model else raw_sources.get(candidate_field))
            if value:
                break
        if value == API_CONFIG_SECRET_KEEP_VALUE:
            return clean_secret(current)
        return value

    def next_mineru_secret(field: str, current: str = "") -> str:
        value = clean_secret(raw_mineru.get(field))
        if value == API_CONFIG_SECRET_KEEP_VALUE:
            return clean_secret(current)
        return value

    def next_codex_secret(field: str, current: str = "") -> str:
        value = clean_secret(raw_codex.get(field))
        if value == API_CONFIG_SECRET_KEEP_VALUE:
            return clean_secret(current)
        return value

    model_name = clean_secret(raw_model.get("model") or raw_model.get("model_name"))
    base_url = clean_secret(raw_model.get("base_url") or raw_model.get("request_url") or raw_model.get("url"))
    mineru_base_url = clean_secret(raw_mineru.get("base_url") or raw_mineru.get("url"))
    config = {
        "model": {
            "model": model_name,
            "base_url": base_url,
            "api_key": next_secret("api_key", existing_model.get("api_key")),
        },
        "code_sources": {
            "github_token": next_secret("github_token", existing_sources.get("github_token")),
            "huggingface_token": next_secret("huggingface_token", existing_sources.get("huggingface_token")),
            "zenodo_token": next_secret("zenodo_token", existing_sources.get("zenodo_token")),
            "gitlab_token": next_secret("gitlab_token", existing_sources.get("gitlab_token"), "gitlab"),
            "brave_search_token": next_secret(
                "brave_search_token",
                existing_sources.get("brave_search_token"),
                "brave_token",
                "brave",
            ),
        },
        "mineru": {
            "base_url": mineru_base_url,
            "api_key": next_mineru_secret("api_key", existing_mineru.get("api_key")),
        },
    }
    if has_codex_payload or has_existing_codex:
        config["codex"] = {
            "model": clean_secret(raw_codex.get("model")) if "model" in raw_codex else existing_codex.get("model", ""),
            "base_url": clean_secret(raw_codex.get("base_url")) if "base_url" in raw_codex else existing_codex.get("base_url", CODEX_DEFAULT_BASE_URL),
            "api_key": next_codex_secret("api_key", existing_codex.get("api_key")),
            "reasoning_effort_default": clean_secret(raw_codex.get("reasoning_effort_default"))
            if "reasoning_effort_default" in raw_codex
            else existing_codex.get("reasoning_effort_default", "medium"),
        }
    return config


def normalized_mineru_api_config(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing if isinstance(existing, dict) else {}
    existing_mineru = existing.get("mineru") if isinstance(existing.get("mineru"), dict) else {}
    raw_mineru = payload.get("mineru") if isinstance(payload.get("mineru"), dict) else payload
    api_key = clean_secret(raw_mineru.get("api_key") or raw_mineru.get("key") or raw_mineru.get("mineru_api_key"))
    if api_key == API_CONFIG_SECRET_KEEP_VALUE:
        api_key = clean_secret(existing_mineru.get("api_key"))
    mineru = {
        "base_url": clean_secret(raw_mineru.get("base_url") or raw_mineru.get("url") or raw_mineru.get("mineru_base_url")),
        "api_key": api_key,
    }
    config = dict(existing)
    config["mineru"] = mineru
    return config


def library_api_config_response(library_id: str, *, include_secrets: bool = False) -> dict[str, Any]:
    saved_model = api_config_model_for_library(library_id)
    saved_tokens = api_config_tokens_for_library(library_id)
    saved_mineru = api_config_mineru_for_library(library_id)
    saved_codex = api_config_codex_for_library(library_id)
    model_name = saved_model.get("model") or clean_secret(os.environ.get(AI_PIXEL_MODEL_ENV)) or AI_PIXEL_DEFAULT_MODEL
    base_url = saved_model.get("base_url") or clean_secret(os.environ.get(AI_PIXEL_BASE_URL_ENV)) or AI_PIXEL_DEFAULT_BASE_URL
    saved_api_key = saved_model.get("api_key", "")
    effective_api_key = saved_api_key or clean_secret(os.environ.get(AI_PIXEL_API_KEY_ENV))
    mineru_base_url = saved_mineru.get("base_url") or clean_secret(os.environ.get(MINERU_BASE_URL_ENV)) or MINERU_DEFAULT_BASE_URL
    saved_mineru_api_key = saved_mineru.get("api_key", "")
    effective_mineru_api_key = saved_mineru_api_key or clean_secret(os.environ.get(MINERU_API_KEY_ENV))
    code_sources = {
        "github": ("github_token", "GITHUB_TOKEN"),
        "huggingface": ("huggingface_token", "HUGGINGFACE_TOKEN"),
        "zenodo": ("zenodo_token", "ZENODO_ACCESS_TOKEN"),
        "gitlab": ("gitlab_token", GITLAB_TOKEN_ENV),
        "brave": ("brave_search_token", BRAVE_SEARCH_API_KEY_ENV),
    }
    code_payload: dict[str, dict[str, Any]] = {}
    for service, (field, env_name) in code_sources.items():
        saved_value = saved_tokens.get(field, "")
        effective_value = saved_value or clean_secret(os.environ.get(env_name))
        code_payload[service] = {
            "field": field,
            "env": env_name,
            "configured": bool(effective_value),
            "source": secret_config_source(saved_value, env_name),
            "token": saved_value if include_secrets else "",
            "masked": masked_secret(saved_value if saved_value else effective_value),
        }
    saved_codex_api_key = clean_secret(saved_codex.get("api_key"))
    return {
        "model": {
            "model": model_name,
            "base_url": base_url,
            "chat_url": normalized_ai_pixel_chat_url(base_url),
            "api_key": saved_api_key if include_secrets else "",
            "masked_api_key": masked_secret(saved_api_key if saved_api_key else effective_api_key),
            "configured": bool(effective_api_key),
            "source": secret_config_source(saved_api_key, AI_PIXEL_API_KEY_ENV),
            "model_source": "preference" if saved_model.get("model") else ("environment" if clean_secret(os.environ.get(AI_PIXEL_MODEL_ENV)) else "default"),
            "base_url_source": "preference" if saved_model.get("base_url") else ("environment" if clean_secret(os.environ.get(AI_PIXEL_BASE_URL_ENV)) else "default"),
        },
        "code_sources": code_payload,
        "codex": {
            "model": clean_secret(saved_codex.get("model")),
            "base_url": clean_secret(saved_codex.get("base_url")) or CODEX_DEFAULT_BASE_URL,
            "reasoning_effort_default": clean_secret(saved_codex.get("reasoning_effort_default")) or "medium",
            "api_key": saved_codex_api_key if include_secrets else "",
            "masked_api_key": masked_secret(saved_codex_api_key),
            "configured": bool(saved_codex_api_key),
            "source": "preference" if saved_codex_api_key else "none",
        },
        "mineru": {
            "base_url": mineru_base_url,
            "api_key": saved_mineru_api_key if include_secrets else "",
            "masked_api_key": masked_secret(saved_mineru_api_key if saved_mineru_api_key else effective_mineru_api_key),
            "configured": bool(effective_mineru_api_key),
            "source": secret_config_source(saved_mineru_api_key, MINERU_API_KEY_ENV),
            "base_url_source": "preference"
            if saved_mineru.get("base_url")
            else ("environment" if clean_secret(os.environ.get(MINERU_BASE_URL_ENV)) else "default"),
        },
    }


def effective_mineru_config_for_library(library_id: str) -> dict[str, str]:
    saved = api_config_mineru_for_library(library_id)
    return {
        "base_url": saved.get("base_url") or clean_secret(os.environ.get(MINERU_BASE_URL_ENV)) or MINERU_DEFAULT_BASE_URL,
        "api_key": saved.get("api_key") or clean_secret(os.environ.get(MINERU_API_KEY_ENV)),
    }


def mineru_results_dir(library: dict[str, Any]) -> Path:
    return Path(str(library["data_path"])) / "mineru-results"


def safe_result_filename(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return clean or "result"


def safe_extract_path(root: Path, relative_name: str) -> Path | None:
    parts = [part for part in Path(str(relative_name).replace("\\", "/")).parts if part not in {"", ".", ".."}]
    if not parts:
        return None
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def mineru_download_bytes(url: str) -> bytes:
    request_obj = urllib_request.Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urllib_request.urlopen(request_obj, timeout=MINERU_REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"MinerU 结果下载失败：HTTP {exc.code} {detail[:300]}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"MinerU 结果下载失败：{exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError("MinerU 结果下载失败：连接被中止，请检查网络、代理或防火墙。") from exc


def mineru_result_urls(payload: Any, target_keys: set[str]) -> list[str]:
    urls: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_name = str(key).lower()
            if key_name in target_keys and isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
            urls.extend(mineru_result_urls(value, target_keys))
    elif isinstance(payload, list):
        for item in payload:
            urls.extend(mineru_result_urls(item, target_keys))
    return list(dict.fromkeys(urls))


def write_mineru_downloaded_outputs(target_dir: Path, stem: str, result_payload: dict[str, Any]) -> dict[str, Any]:
    output_dir = target_dir / stem
    paths: dict[str, Any] = {}
    extracted_paths: list[str] = []
    zip_urls = mineru_result_urls(result_payload, {"full_zip_url", "zip_url", "result_zip_url"})
    for index, url in enumerate(zip_urls, start=1):
        archive_bytes = mineru_download_bytes(url)
        archive_path = target_dir / f"{stem}-mineru-{index}.zip"
        archive_path.write_bytes(archive_bytes)
        paths.setdefault("zip_paths", []).append(str(archive_path))
        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
        except zipfile.BadZipFile:
            continue
        with archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                suffix = Path(member.filename).suffix.lower()
                if suffix not in {".md", ".markdown", ".png", ".jpg", ".jpeg"}:
                    continue
                target = safe_extract_path(output_dir, member.filename)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
                extracted_paths.append(str(target))
    md_urls = mineru_result_urls(result_payload, {"md_url", "markdown_url"})
    for index, url in enumerate(md_urls, start=1):
        output_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = output_dir / f"downloaded-{index}.md"
        markdown_path.write_bytes(mineru_download_bytes(url))
        extracted_paths.append(str(markdown_path))
    image_urls = mineru_result_urls(result_payload, {"image_url", "img_url", "png_url"})
    for index, url in enumerate(image_urls, start=1):
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            suffix = ".png"
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"image-{index}{suffix}"
        image_path.write_bytes(mineru_download_bytes(url))
        extracted_paths.append(str(image_path))
    if extracted_paths:
        paths["output_dir"] = str(output_dir)
        paths["extracted_paths"] = extracted_paths
        markdown_paths = [path for path in extracted_paths if Path(path).suffix.lower() in {".md", ".markdown"}]
        image_paths = [path for path in extracted_paths if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}]
        if markdown_paths:
            paths["markdown_path"] = markdown_paths[0]
            paths["markdown_paths"] = markdown_paths
        if image_paths:
            paths["image_paths"] = image_paths
    return paths


def write_mineru_parse_result(library: dict[str, Any], attachment_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    target_dir = mineru_results_dir(library)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now_iso().replace(':', '').replace('-', '').replace('.', '')}-{safe_result_filename(attachment_key)}"
    json_path = target_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = ""
    result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    data = result_payload.get("data") if isinstance(result_payload.get("data"), dict) else {}
    for key in ("markdown", "md", "content"):
        if isinstance(data.get(key), str) and data.get(key):
            markdown = data[key]
            break
    if not markdown:
        for key in ("markdown", "md", "md_content", "content"):
            if isinstance(result_payload.get(key), str) and result_payload.get(key):
                markdown = result_payload[key]
                break
    if not markdown:
        nested_markdowns = mineru_nested_values(result_payload, {"markdown", "md", "md_content"})
        markdown = next((value for value in nested_markdowns if value), "")
    paths = {"json_path": str(json_path)}
    paths.update(write_mineru_downloaded_outputs(target_dir, stem, result_payload))
    if markdown:
        markdown_path = target_dir / f"{stem}.md"
        markdown_path.write_text(markdown, encoding="utf-8")
        paths["markdown_path"] = str(markdown_path)
    return paths


def mineru_request_url(configured_url: str) -> str:
    url = (clean_secret(configured_url) or MINERU_DEFAULT_BASE_URL).rstrip("/")
    if url.endswith("/api/v4/extract/task"):
        return url[: -len("/api/v4/extract/task")] + "/api/v4/file-urls/batch"
    if url.endswith("/api/v4"):
        return url + "/file-urls/batch"
    if url.endswith("/api/v4/file-urls/batch") or url.endswith("/file_parse"):
        return url
    if "/api/v4/" not in url:
        return url + "/file_parse"
    return url


def mineru_response_code_ok(payload: dict[str, Any]) -> bool:
    return payload.get("code") in (None, 0, "0", 200, "200")


def mineru_response_message(payload: dict[str, Any]) -> str:
    for key in ("err_msg", "error_msg", "msg", "message", "error"):
        value = payload.get(key)
        if value:
            return str(value)
    return "未知错误"


def mineru_nested_values(payload: Any, target_keys: set[str]) -> list[str]:
    values: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in target_keys and value is not None:
                values.append(str(value))
            values.extend(mineru_nested_values(value, target_keys))
    elif isinstance(payload, list):
        for item in payload:
            values.extend(mineru_nested_values(item, target_keys))
    return values


def mineru_parse_state(payload: dict[str, Any]) -> str:
    states = [state.strip().lower() for state in mineru_nested_values(payload, {"state", "status"})]
    if any(state in {"failed", "fail", "error"} for state in states):
        return "failed"
    if any(state in {"done", "success", "succeeded", "finished", "completed"} for state in states):
        return "done"
    if any(state in {"running", "pending", "processing", "queueing", "queued"} for state in states):
        return "running"
    return ""


def mineru_json_request(url: str, api_key: str, payload: dict[str, Any] | None = None, method: str = "POST") -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_obj = urllib_request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib_request.urlopen(request_obj, timeout=MINERU_REQUEST_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"MinerU API 请求失败：HTTP {exc.code} {detail[:300]}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"MinerU API 请求失败：{exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError("MinerU API 请求失败：连接被中止，请确认 MinerU 请求地址并检查代理/防火墙。") from exc
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MinerU API 返回非 JSON 内容：{response_text[:300]}") from exc
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def put_presigned_file(upload_url: str, file_bytes: bytes) -> None:
    parsed = urlsplit(upload_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("MinerU 文件上传失败：上传地址无效。")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(parsed.netloc, timeout=MINERU_REQUEST_TIMEOUT_SECONDS)
    try:
        connection.putrequest("PUT", path, skip_accept_encoding=True)
        connection.putheader("Host", parsed.netloc)
        connection.putheader("Content-Length", str(len(file_bytes)))
        connection.endheaders(file_bytes)
        response = connection.getresponse()
        detail = response.read().decode("utf-8", errors="replace").strip()
    except OSError as exc:
        raise RuntimeError("MinerU 文件上传失败：连接被中止，请检查网络、代理或防火墙。") from exc
    finally:
        connection.close()
    if response.status >= 400:
        raise RuntimeError(f"MinerU 文件上传失败：HTTP {response.status} {detail[:300]}")


def call_mineru_official_batch_parse(pdf_path: Path, api_key: str, url: str) -> dict[str, Any]:
    data_id = new_hash_for_payload(str(pdf_path), now_iso())[:16]
    created = mineru_json_request(
        url,
        api_key,
        {
            "enable_formula": True,
            "enable_table": True,
            "language": "ch",
            "files": [{"name": pdf_path.name, "is_ocr": True, "data_id": data_id}],
        },
    )
    if not mineru_response_code_ok(created):
        raise RuntimeError(f"MinerU API 请求失败：{mineru_response_message(created)}")
    created_data = created.get("data") if isinstance(created.get("data"), dict) else {}
    batch_id = str(created_data.get("batch_id") or created_data.get("batchId") or "")
    file_urls = created_data.get("file_urls") or created_data.get("fileUrls") or []
    upload_url = file_urls[0] if isinstance(file_urls, list) and file_urls else ""
    if not batch_id or not upload_url:
        raise RuntimeError(f"MinerU API 返回缺少 batch_id 或上传地址：{json.dumps(created, ensure_ascii=False)[:300]}")

    put_presigned_file(str(upload_url), pdf_path.read_bytes())

    results_url = url.replace("/api/v4/file-urls/batch", f"/api/v4/extract-results/batch/{quote(batch_id, safe='')}")
    last_result: dict[str, Any] = {}
    attempts = max(1, MINERU_REQUEST_TIMEOUT_SECONDS // MINERU_PARSE_POLL_INTERVAL_SECONDS)
    for _ in range(attempts):
        last_result = mineru_json_request(results_url, api_key, None, method="GET")
        if not mineru_response_code_ok(last_result):
            raise RuntimeError(f"MinerU API 请求失败：{mineru_response_message(last_result)}")
        state = mineru_parse_state(last_result)
        if state == "failed":
            raise RuntimeError(f"MinerU 解析失败：{mineru_response_message(last_result)}")
        if state == "done":
            break
        time.sleep(MINERU_PARSE_POLL_INTERVAL_SECONDS)
    else:
        raise RuntimeError("MinerU 解析超时：已上传 PDF，但等待解析结果超时。")

    return {
        "schema": "mineru.official-batch-result/v1",
        "batch_id": batch_id,
        "data_id": data_id,
        "create_response": created,
        "result_response": last_result,
    }


def call_mineru_file_parse(pdf_path: Path, api_key: str, url: str) -> dict[str, Any]:
    boundary = f"----WebLibraryMinerU{new_hash_for_payload(str(pdf_path), now_iso())[:16]}"
    parts: list[bytes] = []
    for name, value in {
        "parse_method": "auto",
        "return_md": "true",
        "return_content_list": "true",
        "return_images": "false",
        "return_middle_json": "true",
    }.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{pdf_path.name}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8")
        + pdf_path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    request_obj = urllib_request.Request(
        url,
        data=b"".join(parts),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(request_obj, timeout=MINERU_REQUEST_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"MinerU API 请求失败：HTTP {exc.code} {detail[:300]}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"MinerU API 请求失败：{exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError("MinerU API 请求失败：连接被中止，请确认自部署 MinerU 地址支持 /file_parse。") from exc
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MinerU API 返回非 JSON 内容：{response_text[:300]}") from exc
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def call_mineru_parse_pdf(pdf_path: Path, config: dict[str, str]) -> dict[str, Any]:
    api_key = clean_secret(config.get("api_key"))
    base_url = mineru_request_url(clean_secret(config.get("base_url")))
    if not api_key:
        raise ValueError("MinerU API Key 未配置，请先在 API 配置页填写。")
    if not pdf_path.exists():
        raise ValueError("PDF 文件不存在。")

    if base_url.endswith("/api/v4/file-urls/batch"):
        return call_mineru_official_batch_parse(pdf_path, api_key, base_url)
    return call_mineru_file_parse(pdf_path, api_key, base_url)


def new_hash_for_payload(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value or "").encode("utf-8"))
    return digest.hexdigest()


def mineru_parse_selected_pdfs(library: dict[str, Any], item_keys: list[str]) -> dict[str, Any]:
    if library.get("mode") != "local_copy":
        raise SourceError("只读源库不能执行 PDF 解析，请先创建可编辑本地副本。")
    keys = [str(key or "").strip() for key in item_keys if str(key or "").strip()]
    if not keys:
        raise ValueError("item_keys must contain at least one item")
    repo = ZoteroRepository(library)
    config = effective_mineru_config_for_library(str(library["library_id"]))
    results: list[dict[str, Any]] = []
    parsed_count = 0
    failed_count = 0
    pdf_count = 0
    for item_key in keys:
        try:
            attachments = repo.pdf_attachments_for_item(item_key)
        except (ValueError, OSError) as exc:
            results.append({"item_key": item_key, "status": "failed", "error": str(exc), "attachments": []})
            failed_count += 1
            continue
        item_results: list[dict[str, Any]] = []
        if not attachments:
            results.append({"item_key": item_key, "status": "skipped", "error": "没有可解析的 PDF 附件。", "attachments": []})
            continue
        for attachment in attachments:
            pdf_count += 1
            attachment_key = str(attachment.get("key") or "")
            try:
                parsed = call_mineru_parse_pdf(Path(str(attachment.get("resolved_path") or "")), config)
                paths = write_mineru_parse_result(
                    library,
                    attachment_key,
                    {
                        "schema": "web-library.mineru-parse-result/v1",
                        "library_id": library["library_id"],
                        "item_key": item_key,
                        "attachment": attachment,
                        "parsed_at": now_iso(),
                        "mineru": {"base_url": config.get("base_url", "")},
                        "result": parsed,
                    },
                )
                item_results.append({"attachment_key": attachment_key, "status": "parsed", **paths})
                parsed_count += 1
            except Exception as exc:  # noqa: BLE001 - external APIs can fail in many ways
                item_results.append({"attachment_key": attachment_key, "status": "failed", "error": str(exc)})
                failed_count += 1
        status = "parsed" if item_results and all(item.get("status") == "parsed" for item in item_results) else "partial"
        results.append({"item_key": item_key, "status": status, "attachments": item_results})
    return {
        "item_keys": keys,
        "pdf_count": pdf_count,
        "parsed_count": parsed_count,
        "failed_count": failed_count,
        "result_dir": str(mineru_results_dir(library)),
        "results": results,
    }


def retrieval_requested_source_names(value: Any) -> set[str]:
    if value is None or value == "":
        return set()
    raw_items = value if isinstance(value, list) else [value]
    names: set[str] = set()
    for item in raw_items:
        for part in str(item or "").split(","):
            clean = part.strip().lower()
            if clean:
                names.add(clean)
    return names


def retrieval_provider_registry_for_library(library_id: str) -> dict[str, Any]:
    local_config = app_store.retrieval_local_config(library_id) or {}
    return default_provider_registry(
        local_file_paths=local_config.get("paths") if isinstance(local_config.get("paths"), list) else None,
        local_file_field_map=local_config.get("field_map") if isinstance(local_config.get("field_map"), dict) else {},
        http_json_config_value=app_store.retrieval_http_json_config(library_id),
        sqlite_config_value=app_store.retrieval_sqlite_config(library_id),
        manifest_config_value=app_store.retrieval_manifest_config(library_id),
        github_token=effective_code_source_token(library_id, "github_token", "GITHUB_TOKEN"),
        huggingface_token=effective_code_source_token(library_id, "huggingface_token", "HUGGINGFACE_TOKEN"),
        zenodo_token=effective_code_source_token(library_id, "zenodo_token", "ZENODO_ACCESS_TOKEN"),
        gitlab_token=effective_code_source_token(library_id, "gitlab_token", GITLAB_TOKEN_ENV),
        brave_search_token=effective_code_source_token(library_id, "brave_search_token", BRAVE_SEARCH_API_KEY_ENV),
        custom_sources=app_store.list_retrieval_custom_sources(library_id, enabled_only=True),
    )


def local_retrieval_config_for_library(library_id: str) -> tuple[dict[str, Any], str]:
    stored_config = app_store.retrieval_local_config(library_id)
    if stored_config is not None:
        return stored_config, "preference"
    return {"paths": split_local_path_config(os.environ.get("WEB_LIBRARY_RETRIEVAL_LOCAL_PATHS", "")), "field_map": {}}, "environment"


def local_retrieval_paths_for_library(library_id: str) -> tuple[list[str], str]:
    config, source = local_retrieval_config_for_library(library_id)
    return [str(item).strip() for item in (config.get("paths") or []) if str(item).strip()], source


def local_retrieval_path_status(paths: list[str] | None, field_map: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        files = configured_local_file_paths(paths)
    except RetrievalError as exc:
        return {
            "configured": bool(paths),
            "available": False,
            "file_count": 0,
            "files": [],
            "field_map_count": len(field_map or {}),
            "message": str(exc),
        }
    return {
        "configured": True,
        "available": True,
        "file_count": len(files),
        "files": [str(path) for path in files[:20]],
        "field_map_count": len(field_map or {}),
        "message": f"已配置 {len(files)} 个本地检索文件。",
    }


def http_json_config_for_library(library_id: str) -> tuple[dict[str, Any] | str, str]:
    stored_config = app_store.retrieval_http_json_config(library_id)
    if stored_config is not None:
        return stored_config, "preference"
    return os.environ.get(HTTP_JSON_CONFIG_ENV, "").strip(), "environment"


def retrieval_field_map_allowed_targets() -> set[str]:
    return {str(item.get("target") or "") for item in retrieval_field_map_targets() if str(item.get("target") or "")}


def normalize_retrieval_field_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("field_map must be a JSON object")
    allowed = retrieval_field_map_allowed_targets()
    normalized: dict[str, Any] = {}
    unsupported: list[str] = []
    for raw_target, raw_path in value.items():
        target = str(raw_target or "").strip()
        if not target:
            continue
        if target not in allowed:
            unsupported.append(target)
            continue
        if isinstance(raw_path, list):
            paths = [str(item or "").strip() for item in raw_path if str(item or "").strip()]
            if paths:
                normalized[target] = paths
        else:
            path = str(raw_path or "").strip()
            if path:
                normalized[target] = path
    if unsupported:
        raise ValueError(
            "field_map contains unsupported target(s): "
            + ", ".join(sorted(unsupported))
            + ". Supported targets: "
            + ", ".join(sorted(allowed))
        )
    return normalized


def normalize_http_json_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "config" in payload:
        raw: Any = payload.get("config")
    elif "config_text" in payload:
        raw = payload.get("config_text")
    else:
        raw = payload
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str) and not raw.strip():
        return {}
    config = http_json_config(raw)
    normalized: dict[str, Any] = {
        "label": config.get("label") or "HTTP JSON",
        "url_template": config.get("url_template") or "",
        "items_path": config.get("items_path") or "",
        "field_map": normalize_retrieval_field_map(config.get("field_map") or {}),
    }
    if config.get("next_url_path"):
        normalized["next_url_path"] = config.get("next_url_path")
    if int(config.get("max_pages") or 1) > 1:
        normalized["max_pages"] = int(config.get("max_pages") or 1)
    if int(config.get("page_start") or 1) != 1:
        normalized["page_start"] = int(config.get("page_start") or 1)
    headers = config.get("headers") or {}
    if headers:
        normalized["headers"] = headers
    auth = config.get("auth") or {}
    if auth:
        normalized["auth"] = auth
    return normalized if normalized["url_template"] else {}


def sqlite_config_for_library(library_id: str) -> tuple[dict[str, Any] | str, str]:
    stored_config = app_store.retrieval_sqlite_config(library_id)
    if stored_config is not None:
        return stored_config, "preference"
    return os.environ.get(SQLITE_CONFIG_ENV, "").strip(), "environment"


def normalize_sqlite_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "config" in payload:
        raw: Any = payload.get("config")
    elif "config_text" in payload:
        raw = payload.get("config_text")
    else:
        raw = payload
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str) and not raw.strip():
        return {}
    config = sqlite_config(raw)
    normalized: dict[str, Any] = {
        "label": config.get("label") or "SQLite",
        "path": config.get("path") or "",
        "query": config.get("query") or "",
        "field_map": normalize_retrieval_field_map(config.get("field_map") or {}),
    }
    return normalized if normalized["path"] and normalized["query"] else {}


def manifest_config_for_library(library_id: str) -> tuple[dict[str, Any] | str, str]:
    stored_config = app_store.retrieval_manifest_config(library_id)
    if stored_config is not None:
        return stored_config, "preference"
    return os.environ.get(MANIFEST_CONFIG_ENV, "").strip(), "environment"


def normalize_manifest_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "config" in payload:
        raw: Any = payload.get("config")
    elif "config_text" in payload:
        raw = payload.get("config_text")
    else:
        raw = payload
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str) and not raw.strip():
        return {}
    config = manifest_config(raw)
    normalized: dict[str, Any] = {
        "label": config.get("label") or "Object Manifest",
        "manifest_path": config.get("manifest_path") or "",
        "manifest_url": config.get("manifest_url") or "",
        "items_path": config.get("items_path") or "",
        "field_map": normalize_retrieval_field_map(config.get("field_map") or {}),
    }
    headers = config.get("headers") or {}
    if headers:
        normalized["headers"] = headers
    auth = config.get("auth") or {}
    if auth:
        normalized["auth"] = auth
    return normalized if normalized["manifest_path"] or normalized["manifest_url"] else {}


def normalize_local_retrieval_paths_payload(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("paths")
    if raw is None:
        raw = payload.get("path_text") or payload.get("text") or ""
    if isinstance(raw, str):
        return split_local_path_config(raw)
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    raise ValueError("paths 必须是数组或文本。")


def normalize_local_retrieval_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    paths = normalize_local_retrieval_paths_payload(payload)
    raw_field_map = payload.get("field_map")
    if raw_field_map is None:
        raw_field_map = payload.get("field_map_text")
    if isinstance(raw_field_map, str):
        if not raw_field_map.strip():
            field_map: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(raw_field_map)
            except json.JSONDecodeError as exc:
                raise ValueError("field_map must be a JSON object") from exc
            if not isinstance(parsed, dict):
                raise ValueError("field_map must be a JSON object")
            field_map = parsed
    elif isinstance(raw_field_map, dict):
        field_map = raw_field_map
    elif raw_field_map is None:
        field_map = {}
    else:
        raise ValueError("field_map must be a JSON object")
    return {
        "paths": paths,
        "field_map": normalize_retrieval_field_map(field_map),
    }


def normalize_custom_source_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing if isinstance(existing, dict) else {}
    kind = normalize_custom_source_kind(payload.get("kind") or existing.get("kind") or "httpjson")
    name = str(payload.get("name") or existing.get("name") or custom_source_kind_label(kind)).strip()
    enabled = bool(payload.get("enabled", existing.get("enabled", True)))
    config = normalize_custom_source_config(kind, payload, existing.get("config") if isinstance(existing.get("config"), dict) else {})
    material_types = [
        normalized_material_type(item)
        for item in (payload.get("resource_types") or config.get("resource_types") or [])
        if normalized_material_type(item)
    ]
    if material_types:
        config["resource_types"] = list(dict.fromkeys(material_types))
    return {
        "source_id": str(payload.get("source_id") or payload.get("id") or existing.get("source_id") or "").strip(),
        "name": name[:120] or custom_source_kind_label(kind),
        "kind": kind,
        "enabled": enabled,
        "config": config,
    }


def normalize_custom_source_kind(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "").replace("-", "")
    aliases = {
        "httpjson": "httpjson",
        "http": "httpjson",
        "json": "httpjson",
        "localfile": "localfile",
        "file": "localfile",
        "csvjsonl": "localfile",
        "manifest": "manifest",
        "objectmanifest": "manifest",
        "sqlite": "sqlite",
    }
    return aliases.get(text, "httpjson")


def custom_source_kind_label(kind: str) -> str:
    return {
        "httpjson": "HTTP JSON",
        "localfile": "CSV/JSONL 文件",
        "manifest": "Object Manifest",
        "sqlite": "SQLite",
    }.get(kind, "自定义源")


def normalize_custom_source_config(kind: str, payload: dict[str, Any], existing_config: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("config") if "config" in payload else None
    if raw is None and "config_text" in payload:
        raw = payload.get("config_text")
    if isinstance(raw, str):
        if not raw.strip():
            raw = {}
        else:
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("config_text must be a JSON object") from exc
    if raw is None:
        raw = {key: value for key, value in payload.items() if key not in {"source_id", "id", "name", "kind", "enabled"}}
    if not isinstance(raw, dict):
        raise ValueError("custom source config must be a JSON object")
    merged = {**existing_config, **raw}
    if kind == "localfile":
        config = normalize_local_retrieval_config_payload(merged)
    elif kind == "httpjson":
        config = normalize_http_json_config_payload({"config": merged}) or dict(merged)
    elif kind == "manifest":
        config = normalize_manifest_config_payload({"config": merged}) or dict(merged)
    elif kind == "sqlite":
        config = normalize_sqlite_config_payload({"config": merged}) or dict(merged)
    else:
        raise ValueError(f"unsupported custom source kind: {kind}")
    resource_types = merged.get("resource_types")
    if isinstance(resource_types, str):
        resource_types = [item.strip() for item in resource_types.split(",") if item.strip()]
    if isinstance(resource_types, list):
        normalized_types = [normalized_material_type(item) for item in resource_types if normalized_material_type(item)]
        if normalized_types:
            config["resource_types"] = list(dict.fromkeys(normalized_types))
    return config


def custom_source_check_result(source: dict[str, Any], *, query: str = "robot", limit: int = 2) -> dict[str, Any]:
    provider = CustomSourceProvider(source)
    config_error = provider.configuration_error()
    if config_error:
        return {"ok": False, "available": False, "error": config_error, "candidate_count": 0, "candidates": []}
    candidates = provider.search(query, max(1, min(int(limit or 2), 10)))
    return {
        "ok": True,
        "available": True,
        "candidate_count": len(candidates),
        "candidates": [candidate.as_dict(include_raw=False) for candidate in candidates],
        "query": query,
    }


def config_value_looks_env_reference(value: str) -> bool:
    text = str(value or "").strip()
    return bool(ENV_REFERENCE_RE.match(text)) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", text) is not None


def redact_retrieval_config(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if (
                SENSITIVE_CONFIG_KEY_RE.search(key_text)
                and isinstance(item, str)
                and item
                and not config_value_looks_env_reference(item)
            ):
                redacted[key_text] = RETRIEVAL_CONFIG_BUNDLE_REDACTED_VALUE
                continue
            redacted[key_text] = redact_retrieval_config(item, parent_key=key_text)
        return redacted
    if isinstance(value, list):
        return [redact_retrieval_config(item, parent_key=parent_key) for item in value]
    if (
        isinstance(value, str)
        and SENSITIVE_CONFIG_KEY_RE.search(parent_key)
        and value
        and not config_value_looks_env_reference(value)
    ):
        return RETRIEVAL_CONFIG_BUNDLE_REDACTED_VALUE
    return value


def retrieval_config_contains_redactions(value: Any) -> bool:
    if value == RETRIEVAL_CONFIG_BUNDLE_REDACTED_VALUE:
        return True
    if isinstance(value, dict):
        return any(retrieval_config_contains_redactions(item) for item in value.values())
    if isinstance(value, list):
        return any(retrieval_config_contains_redactions(item) for item in value)
    return False


def safe_config_summary(summary_func, raw_config: Any) -> dict[str, Any]:
    try:
        return summary_func(raw_config)
    except Exception as exc:  # noqa: BLE001 - config bundles should report bad configs without failing the whole export
        return {"configured": False, "error": str(exc)}


def normalized_config_for_bundle(normalize_func, raw_config: Any) -> dict[str, Any]:
    try:
        return normalize_func({"config": raw_config})
    except Exception:
        return {}


def retrieval_config_bundle_for_library(library_id: str, *, redact: bool = True) -> dict[str, Any]:
    local_config, local_source = local_retrieval_config_for_library(library_id)
    local_paths = [str(path).strip() for path in local_config.get("paths") or [] if str(path).strip()]
    local_field_map = local_config.get("field_map") if isinstance(local_config.get("field_map"), dict) else {}
    http_raw_config, http_source = http_json_config_for_library(library_id)
    sqlite_raw_config, sqlite_source = sqlite_config_for_library(library_id)
    manifest_raw_config, manifest_source = manifest_config_for_library(library_id)
    configs = {
        "httpjson": (
            "HTTP JSON",
            http_source,
            http_raw_config,
            safe_config_summary(http_json_config_summary, http_raw_config),
            normalized_config_for_bundle(normalize_http_json_config_payload, http_raw_config),
        ),
        "sqlite": (
            "SQLite",
            sqlite_source,
            sqlite_raw_config,
            safe_config_summary(sqlite_config_summary, sqlite_raw_config),
            normalized_config_for_bundle(normalize_sqlite_config_payload, sqlite_raw_config),
        ),
        "manifest": (
            "Object Manifest",
            manifest_source,
            manifest_raw_config,
            safe_config_summary(manifest_config_summary, manifest_raw_config),
            normalized_config_for_bundle(normalize_manifest_config_payload, manifest_raw_config),
        ),
    }
    sources: dict[str, Any] = {
        "localfile": {
            "label": "Local CSV/JSONL",
            "source": local_source,
            "configured": bool(local_paths),
            "paths": list(local_paths),
            "field_map": local_field_map,
            "config": {"paths": list(local_paths), "field_map": local_field_map},
            "status": local_retrieval_path_status(local_paths, local_field_map),
        }
    }
    redacted_sources: list[str] = []
    for name, (label, source, _raw_config, summary, config) in configs.items():
        export_config = redact_retrieval_config(config) if redact else config
        if retrieval_config_contains_redactions(export_config):
            redacted_sources.append(name)
        sources[name] = {
            "label": label,
            "source": source,
            "configured": bool(summary.get("configured")),
            "summary": summary,
            "config": export_config,
        }
    return {
        "schema": RETRIEVAL_CONFIG_BUNDLE_SCHEMA,
        "generated_at": now_iso(),
        "library_id": library_id,
        "redacted": redact,
        "redacted_value": RETRIEVAL_CONFIG_BUNDLE_REDACTED_VALUE,
        "redacted_sources": redacted_sources,
        "sources": sources,
        "notes": [
            "API keys and direct secret values are redacted; keep real secrets in environment variables.",
            "Local paths are exported as-is and may need adjustment on another machine.",
        ],
    }


def stable_json_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def retrieval_config_fingerprint_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), dict) else {}
    stable_sources: dict[str, Any] = {}
    for raw_name, entry in sorted(sources.items(), key=lambda item: str(item[0])):
        name = str(raw_name)
        if not isinstance(entry, dict):
            continue
        stable_sources[name] = {
            "configured": bool(entry.get("configured")),
            "source": str(entry.get("source") or ""),
            "config": entry.get("config") if isinstance(entry.get("config"), dict) else {},
            "paths": [str(path) for path in entry.get("paths") or []],
            "field_map": entry.get("field_map") if isinstance(entry.get("field_map"), dict) else {},
            "summary": entry.get("summary") if isinstance(entry.get("summary"), dict) else {},
        }
    return {
        "schema": str(bundle.get("schema") or RETRIEVAL_CONFIG_BUNDLE_SCHEMA),
        "redacted": bool(bundle.get("redacted", True)),
        "redacted_sources": sorted(str(item) for item in bundle.get("redacted_sources") or []),
        "sources": stable_sources,
    }


def retrieval_batch_context_for_library(library_id: str) -> dict[str, Any]:
    bundle = retrieval_config_bundle_for_library(library_id, redact=True)
    fingerprint_payload = retrieval_config_fingerprint_payload(bundle)
    sources = fingerprint_payload.get("sources") if isinstance(fingerprint_payload.get("sources"), dict) else {}
    source_fingerprints = [
        {
            "source": name,
            "configured": bool(entry.get("configured")) if isinstance(entry, dict) else False,
            "fingerprint": stable_json_fingerprint(entry),
        }
        for name, entry in sorted(sources.items())
    ]
    return {
        "schema": RETRIEVAL_BATCH_CONTEXT_SCHEMA,
        "generated_at": now_iso(),
        "library_id": library_id,
        "redacted": True,
        "config_fingerprint": stable_json_fingerprint(fingerprint_payload),
        "configured_sources": [
            item["source"] for item in source_fingerprints if item.get("configured") and item.get("source")
        ],
        "source_fingerprints": source_fingerprints,
    }


def retrieval_config_bundle_filename() -> str:
    return "retrieval-config-bundle.json"


def clean_retrieval_config_bundle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = payload.get("bundle") if isinstance(payload.get("bundle"), dict) else payload
    if not isinstance(bundle, dict):
        raise ValueError("config bundle must be a JSON object")
    return bundle


def apply_retrieval_config_bundle(library_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    bundle = clean_retrieval_config_bundle_payload(payload)
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), dict) else {}
    requested_sources = payload.get("sources")
    if isinstance(requested_sources, list):
        apply_names = {str(name).strip() for name in requested_sources if str(name).strip()}
    else:
        apply_names = {"localfile", "httpjson", "sqlite", "manifest"}
    allow_redacted = truthy_query_flag(payload.get("allow_redacted"))
    dry_run = truthy_query_flag(payload.get("dry_run"))
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def skip(source: str, reason: str) -> None:
        skipped.append({"source": source, "reason": reason})

    local_entry = sources.get("localfile") if isinstance(sources.get("localfile"), dict) else {}
    if "localfile" in apply_names:
        config = local_entry.get("config") if isinstance(local_entry.get("config"), dict) else local_entry
        paths = config.get("paths") if isinstance(config.get("paths"), list) else []
        normalized_paths = [str(path).strip() for path in paths if str(path).strip()]
        try:
            field_map = normalize_retrieval_field_map(
                config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
            )
        except (RetrievalError, ValueError) as exc:
            skip("localfile", f"invalid config: {exc}")
        else:
            if normalized_paths:
                if dry_run:
                    applied.append(
                        {
                            "source": "localfile",
                            "configured": True,
                            "paths": normalized_paths,
                            "action": "would_apply",
                        }
                    )
                else:
                    stored_config = app_store.set_retrieval_local_config(
                        library_id,
                        {"paths": normalized_paths, "field_map": field_map},
                    )
                    applied.append(
                        {
                            "source": "localfile",
                            "configured": True,
                            "paths": stored_config["paths"],
                            "action": "applied",
                        }
                    )
            else:
                skip("localfile", "no paths in bundle")

    config_sources = {
        "httpjson": (normalize_http_json_config_payload, app_store.set_retrieval_http_json_config),
        "sqlite": (normalize_sqlite_config_payload, app_store.set_retrieval_sqlite_config),
        "manifest": (normalize_manifest_config_payload, app_store.set_retrieval_manifest_config),
    }
    for source_name, (normalize_func, setter) in config_sources.items():
        if source_name not in apply_names:
            continue
        entry = sources.get(source_name) if isinstance(sources.get(source_name), dict) else {}
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        if not config:
            skip(source_name, "no config in bundle")
            continue
        if retrieval_config_contains_redactions(config) and not allow_redacted:
            skip(source_name, "config contains redacted values")
            continue
        try:
            normalized = normalize_func({"config": config})
        except (RetrievalError, ValueError) as exc:
            skip(source_name, f"invalid config: {exc}")
            continue
        if not normalized:
            skip(source_name, "config is incomplete")
            continue
        if dry_run:
            applied.append({"source": source_name, "configured": True, "action": "would_apply"})
            continue
        stored_config = setter(library_id, normalized)
        applied.append({"source": source_name, "configured": bool(stored_config), "action": "applied"})

    return {
        "dry_run": dry_run,
        "applied": applied,
        "skipped": skipped,
        "bundle_schema": str(bundle.get("schema") or ""),
        "sources": retrieval_source_statuses(registry=retrieval_provider_registry_for_library(library_id)),
    }


RETRIEVAL_REHEARSAL_SOURCES = ("localfile", "sqlite", "manifest")


def configured_retrieval_rehearsal_sources(library_id: str) -> list[str]:
    bundle = retrieval_config_bundle_for_library(library_id, redact=False)
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), dict) else {}
    configured: list[str] = []
    for name in RETRIEVAL_REHEARSAL_SOURCES:
        entry = sources.get(name) if isinstance(sources.get(name), dict) else {}
        if entry.get("configured"):
            configured.append(name)
    return configured


def setup_retrieval_rehearsal_for_library(library_id: str, *, replace_existing: bool = False) -> dict[str, Any]:
    conflicts = configured_retrieval_rehearsal_sources(library_id)
    if conflicts and not replace_existing:
        return {
            "applied": False,
            "conflicts": conflicts,
            "kit": None,
            "message": (
                "Existing internal source config was not replaced. "
                "Pass replace_existing=1 to overwrite Local CSV/JSONL, SQLite and Object Manifest configs."
            ),
        }
    kit = write_retrieval_rehearsal_kit(app_data_dir(), library_id)
    applied = apply_retrieval_config_bundle(
        library_id,
        {
            "bundle": kit["config_bundle"],
            "sources": list(RETRIEVAL_REHEARSAL_SOURCES),
        },
    )
    return {
        "applied": True,
        "conflicts": conflicts,
        "kit": kit,
        "import_result": applied,
        "sources": applied.get("sources") or [],
        "message": "Rehearsal kit generated and internal retrieval sources were configured.",
        "next_steps": [
            "Run READY with query=robot catalyst and sample_size=2.",
            "Generate the query plan and run its 3-query batch before ONB.",
            "Download ONB report and ONB ZIP for handoff evidence.",
        ],
    }


def retrieval_rehearsal_validation_artifacts(job_id: str, *, query: str, sample_size: int, limit: int) -> dict[str, str]:
    query_plan_params = urlencode({"seed_query": query, "sample_size": sample_size, "limit": 5})
    query_plan_report_params = urlencode({"format": "markdown", "seed_query": query, "sample_size": sample_size, "limit": 5})
    readiness_params = urlencode({"query": query, "sample_size": sample_size})
    readiness_report_params = urlencode({"format": "markdown", "query": query, "sample_size": sample_size})
    onboarding_params = urlencode({"query": query, "sample_size": sample_size, "limit": limit})
    onboarding_report_params = urlencode({"format": "markdown", "query": query, "sample_size": sample_size, "limit": limit})
    return {
        "query_plan": f"/retrieval/query-plan?{query_plan_params}",
        "query_plan_report": f"/retrieval/query-plan/report?{query_plan_report_params}",
        "readiness": f"/retrieval/readiness?{readiness_params}",
        "readiness_report": f"/retrieval/readiness/report?{readiness_report_params}",
        "batch": f"/retrieval/batches/{job_id}",
        "batch_report": f"/retrieval/batches/{job_id}/report",
        "batch_source_csv": f"/retrieval/batches/{job_id}/report?format=csv&scope=sources",
        "onboarding": f"/retrieval/onboarding?{onboarding_params}",
        "onboarding_report": f"/retrieval/onboarding/report?{onboarding_report_params}",
        "onboarding_package": f"/retrieval/onboarding/package?{onboarding_params}",
    }


def retrieval_rehearsal_validation_evidence(
    setup: dict[str, Any],
    readiness: dict[str, Any],
    job: dict[str, Any],
    onboarding: dict[str, Any],
    artifacts: dict[str, str],
    *,
    queries: list[str],
    sources: list[str],
) -> dict[str, Any]:
    readiness_status = str(readiness.get("status") or "")
    onboarding_status = str(onboarding.get("status") or "")
    batch_validation = onboarding.get("batch_validation") if isinstance(onboarding.get("batch_validation"), dict) else {}
    batch_validation_status = str(batch_validation.get("status") or "")
    import_readiness = onboarding.get("import_readiness") if isinstance(onboarding.get("import_readiness"), dict) else {}
    import_readiness_status = str(import_readiness.get("status") or "")
    completed_queries = safe_int(job.get("completed_queries"))
    total_queries = safe_int(job.get("total_queries"))
    failed_queries = safe_int(job.get("failed_queries"))
    total_candidates = safe_int(job.get("total_candidates"))
    setup_sources = [
        str(item.get("source") or "")
        for item in (setup.get("import_result") or {}).get("applied", [])
        if isinstance(item, dict) and item.get("source")
    ]
    source_evidence = batch_validation.get("source_evidence") if isinstance(batch_validation.get("source_evidence"), list) else []
    artifact_count = sum(1 for endpoint in artifacts.values() if endpoint)
    if not setup.get("applied"):
        status = "blocked"
        message = "Rehearsal source setup did not apply."
    elif readiness_status == "blocked":
        status = "blocked"
        message = "Rehearsal sources are configured, but READY is blocked."
    elif import_readiness_status == "blocked":
        status = "blocked"
        message = "Rehearsal batch candidates cannot be converted into the import model."
    elif import_readiness_status == "warning":
        status = "needs_attention"
        message = "Rehearsal batch candidates need import-field review."
    elif batch_validation_status == "passed":
        status = "passed"
        message = "Rehearsal validation passed with READY, completed batch evidence and ONB artifacts."
    elif batch_validation_status == "active" or str(job.get("status") or "") in {"queued", "running", "paused"}:
        status = "active"
        message = "Rehearsal validation started; refresh batch and ONB after the batch finishes."
    else:
        status = "needs_attention"
        message = "Rehearsal validation produced evidence, but one or more gates still need attention."

    gates = [
        {
            "name": "setup_sources",
            "label": "Generate and save rehearsal sources",
            "status": "passed" if setup.get("applied") and len(set(setup_sources)) >= len(sources) else "blocked",
            "evidence": f"{len(set(setup_sources))}/{len(sources)} rehearsal sources configured.",
            "artifacts": [artifacts.get("query_plan", "")],
        },
        {
            "name": "readiness",
            "label": "READY preview over rehearsal sources",
            "status": "passed" if readiness_status == "ready" else readiness_status or "missing",
            "evidence": str(readiness.get("message") or ""),
            "artifacts": [artifacts.get("readiness_report", "")],
        },
        {
            "name": "batch_validation",
            "label": "Three-query validation batch",
            "status": batch_validation_status or str(job.get("status") or "missing"),
            "evidence": (
                f"{completed_queries}/{total_queries} completed; "
                f"{failed_queries} failed; {total_candidates} candidates."
            ),
            "artifacts": [artifacts.get("batch_report", ""), artifacts.get("batch_source_csv", "")],
        },
        {
            "name": "import_readiness",
            "label": "Candidate import model readiness",
            "status": "passed" if import_readiness_status == "passed" else import_readiness_status or "missing",
            "evidence": (
                f"{safe_int(import_readiness.get('ready_candidate_count'))}/"
                f"{safe_int(import_readiness.get('checked_candidate_count'))} sampled candidates importable; "
                f"{safe_int(import_readiness.get('error_candidate_count'))} conversion errors."
            ),
            "artifacts": [artifacts.get("batch_report", "")],
        },
        {
            "name": "onboarding",
            "label": "ONB handoff evidence",
            "status": "passed" if onboarding_status == "ready" else onboarding_status or "missing",
            "evidence": str(onboarding.get("message") or ""),
            "artifacts": [artifacts.get("onboarding_report", ""), artifacts.get("onboarding_package", "")],
        },
    ]
    return {
        "status": status,
        "message": message,
        "query_count": len(queries),
        "source_count": len(sources),
        "artifact_count": artifact_count,
        "readiness_status": readiness_status,
        "batch_status": str(job.get("status") or ""),
        "batch_validation_status": batch_validation_status,
        "import_readiness_status": import_readiness_status,
        "import_readiness_ready_candidate_count": safe_int(import_readiness.get("ready_candidate_count")),
        "import_readiness_checked_candidate_count": safe_int(import_readiness.get("checked_candidate_count")),
        "onboarding_status": onboarding_status,
        "completed_queries": completed_queries,
        "total_queries": total_queries,
        "failed_queries": failed_queries,
        "total_candidates": total_candidates,
        "validated_sources": [str(item) for item in batch_validation.get("validated_sources") or []],
        "missing_sources": [str(item) for item in batch_validation.get("missing_sources") or []],
        "source_evidence": source_evidence,
        "handoff_artifacts": artifacts,
        "gates": gates,
    }


RETRIEVAL_SOURCE_INTAKE_SCHEMA = "web-library.retrieval-source-intake/v1"
SOURCE_INTAKE_DESCRIPTORS = {
    "localfile": {
        "label": "Local CSV/JSONL",
        "endpoint": "/retrieval/local-files",
        "required": ["paths", "field_map"],
        "next_action": "Save Local CSV/JSONL paths, preview rows, then run READY.",
    },
    "httpjson": {
        "label": "HTTP JSON",
        "endpoint": "/retrieval/http-json",
        "required": ["url_template", "items_path", "field_map"],
        "next_action": "Save the HTTP JSON template, sample the response, then run READY.",
    },
    "sqlite": {
        "label": "SQLite",
        "endpoint": "/retrieval/sqlite",
        "required": ["path", "query", "field_map"],
        "next_action": "Save a read-only SELECT query, preview rows, then run READY.",
    },
    "manifest": {
        "label": "Object Manifest",
        "endpoint": "/retrieval/manifest",
        "required": ["manifest_path or manifest_url", "items_path", "field_map"],
        "next_action": "Save the manifest location and items_path, preview objects, then run READY.",
    },
}


def source_intake_target_source(source_type: str) -> dict[str, Any]:
    canonical = source_intake_clean_text(source_type)
    descriptor = SOURCE_INTAKE_DESCRIPTORS.get(canonical, {})
    return {
        "name": canonical,
        "label": descriptor.get("label", canonical),
        "endpoint": descriptor.get("endpoint", ""),
        "required": descriptor.get("required", []),
    }


def source_intake_clean_text(value: Any) -> str:
    return str(value or "").strip()


def source_intake_input_text(payload: dict[str, Any]) -> str:
    for key in ("input", "text", "sample_text", "raw", "path", "url", "sql"):
        text = source_intake_clean_text(payload.get(key))
        if text:
            return text
    return ""


def source_intake_parse_json(text: str) -> Any:
    clean = source_intake_clean_text(text)
    if not clean or clean[0] not in "[{":
        return None
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return None


def source_intake_value_at_path(value: Any, path: str) -> Any:
    current = value
    for part in [item for item in source_intake_clean_text(path).split(".") if item]:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def source_intake_samples_from_json(value: Any, configured_path: str = "") -> list[dict[str, Any]]:
    configured = source_intake_value_at_path(value, configured_path) if configured_path else None
    candidates = [
        configured,
        value,
        value.get("samples") if isinstance(value, dict) else None,
        value.get("items") if isinstance(value, dict) else None,
        value.get("objects") if isinstance(value, dict) else None,
        value.get("results") if isinstance(value, dict) else None,
        value.get("records") if isinstance(value, dict) else None,
        source_intake_value_at_path(value, "data.items"),
        source_intake_value_at_path(value, "data.records"),
        source_intake_value_at_path(value, "response.docs"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)][:10]
        if isinstance(candidate, dict) and any(not isinstance(item, (dict, list)) for item in candidate.values()):
            return [candidate]
    return []


def source_intake_columns_from_text(text: str) -> list[str]:
    clean = source_intake_clean_text(text)
    if not clean:
        return []
    if clean[0] in "[{":
        return []
    first_line = clean.splitlines()[0]
    if not any(separator in first_line for separator in [",", "\t", ";"]):
        return []
    try:
        row = next(csv.reader([first_line]))
    except csv.Error:
        row = re.split(r"[\t,;]+", first_line)
    columns = [source_intake_clean_text(item) for item in row if source_intake_clean_text(item)]
    return columns if len(columns) >= 2 else []


def source_intake_sql_columns(text: str) -> list[str]:
    match = re.search(r"\bselect\b(?P<columns>.+?)\bfrom\b", text, re.I | re.S)
    if not match:
        return []
    columns: list[str] = []
    for raw in match.group("columns").split(","):
        column = re.sub(r"\bas\b\s+\w+$", "", raw.strip(), flags=re.I)
        column = column.split(".")[-1].strip(" []`\"")
        if column and column != "*" and column not in columns:
            columns.append(column)
    return columns[:20]


def source_intake_extension(text: str) -> str:
    path = source_intake_clean_text(text).split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
    suffix = Path(path).suffix.lower()
    return suffix.lstrip(".")


def source_intake_has_local_path(value: str, *, has_json_sample: bool, has_sql: bool) -> bool:
    text = source_intake_clean_text(value)
    if not text or has_json_sample or has_sql or "://" in text:
        return False
    return bool(re.search(r"^[A-Za-z]:[\\/]", text) or text.startswith("/") or "\\" in text)


def source_intake_items_path(json_value: Any) -> str:
    if isinstance(json_value, dict):
        for path in ("objects", "items", "results", "records", "data.items", "data.records", "response.docs"):
            if isinstance(source_intake_value_at_path(json_value, path), list):
                return path
    return ""


def source_intake_columns_from_samples(samples: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        for key in sample:
            clean_key = source_intake_clean_text(key)
            if clean_key and clean_key not in columns:
                columns.append(clean_key)
            if len(columns) >= 40:
                return columns
    return columns


def source_intake_existing_path(payload: dict[str, Any], text: str, *, has_json_sample: bool, has_sql: bool) -> Path | None:
    raw = source_intake_clean_text(payload.get("path")) or source_intake_clean_text(text)
    raw = raw.strip("\"'")
    if not raw or "\n" in raw or "\r" in raw:
        return None
    explicit_path = bool(source_intake_clean_text(payload.get("path")))
    if not explicit_path and not source_intake_has_local_path(raw, has_json_sample=has_json_sample, has_sql=has_sql):
        return None
    if raw.lower().startswith(("http://", "https://")) or "://" in raw:
        return None
    try:
        path = Path(raw).expanduser()
        return path if path.exists() else None
    except (OSError, RuntimeError, ValueError):
        return None


def source_intake_csv_header_columns(path: Path) -> list[str]:
    if path.suffix.lower() != ".csv":
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            first_line = handle.readline()
    except OSError:
        return []
    return source_intake_columns_from_text(first_line)


def source_intake_sample_local_path(path: Path, *, sample_size: int = 5) -> dict[str, Any]:
    try:
        paths = configured_local_file_paths([path])
        samples: list[dict[str, Any]] = []
        sampled_path = paths[0] if paths else path
        columns: list[str] = []
        for file_path in paths:
            previous_sample_count = len(samples)
            if not columns:
                columns = source_intake_csv_header_columns(file_path)
            for _row_number, row in iter_local_file_rows(file_path):
                if isinstance(row, dict):
                    samples.append(row)
                if len(samples) >= sample_size:
                    break
            if len(samples) > previous_sample_count:
                sampled_path = file_path
            if len(samples) >= sample_size:
                break
        sampled_columns = source_intake_columns_from_samples(samples)
        return {
            "sampled_path": str(sampled_path),
            "sampled_file_count": len(paths),
            "samples": samples,
            "columns": sampled_columns or columns,
        }
    except (RetrievalError, OSError, UnicodeError, csv.Error) as exc:
        return {"sampled_path": str(path), "sampling_error": str(exc)}


def source_intake_sample_manifest_path(path: Path, *, sample_size: int = 5) -> dict[str, Any]:
    config = {
        "label": path.stem or "Object Manifest",
        "manifest_path": str(path),
    }
    try:
        sampled_config, samples = manifest_sample_items(config, sample_size=sample_size)
        items_path = source_intake_clean_text(sampled_config.get("items_path"))
        return {
            "sampled_path": str(path),
            "items_path": items_path,
            "samples": samples,
            "columns": source_intake_columns_from_samples(samples),
            "config": sampled_config,
        }
    except (RetrievalError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"sampled_path": str(path), "sampling_error": str(exc)}


def source_intake_sample_url_requested(payload: dict[str, Any]) -> bool:
    return any(
        truthy_query_flag(payload.get(name))
        for name in ("sample_url", "sample_http", "fetch_url", "fetch_http")
    )


def source_intake_http_sample_size(payload: dict[str, Any]) -> int:
    try:
        parsed = int(payload.get("sample_size") or 5)
    except (TypeError, ValueError):
        parsed = 5
    return max(1, min(parsed, 10))


def source_intake_sample_http_json_url(
    payload: dict[str, Any],
    url: str,
    *,
    sample_size: int = 5,
) -> dict[str, Any]:
    clean_url = source_intake_clean_text(url).strip("\"'")
    if not clean_url.lower().startswith(("http://", "https://")) or "\n" in clean_url or "\r" in clean_url:
        return {}
    config = dict(payload.get("config") if isinstance(payload.get("config"), dict) else {})
    if not source_intake_clean_text(config.get("url_template")):
        config["url_template"] = clean_url
    config.setdefault("label", "HTTP JSON")
    items_path = source_intake_clean_text(payload.get("items_path"))
    if items_path:
        config["items_path"] = items_path
    query = source_intake_clean_text(payload.get("query") or payload.get("seed_query")) or "robot"
    try:
        sampled_config, samples = http_json_sample_items(config, query=query, sample_size=sample_size)
        sampled_items_path = source_intake_clean_text(sampled_config.get("items_path"))
        return {
            "sample_url_requested": True,
            "sampled_url": clean_url,
            "sample_query": query,
            "items_path": sampled_items_path,
            "samples": samples,
            "columns": source_intake_columns_from_samples(samples),
            "config": sampled_config,
        }
    except (RetrievalError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "sample_url_requested": True,
            "sampled_url": clean_url,
            "sample_query": query,
            "sampling_error": str(exc),
        }


def source_intake_sqlite_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def source_intake_sample_sqlite_path(path: Path, *, sample_size: int = 5) -> dict[str, Any]:
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            table_row = connection.execute(
                """
                SELECT name, type
                FROM sqlite_master
                WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
                ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name
                LIMIT 1
                """
            ).fetchone()
            if table_row is None:
                return {"sampled_path": str(path), "sampling_error": "SQLite database has no user tables or views."}
            table = str(table_row["name"])
            table_sql = source_intake_sqlite_identifier(table)
            columns = [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table_sql})")
                if source_intake_clean_text(row["name"])
            ]
            select_columns = columns[:12]
            select_sql = ", ".join(source_intake_sqlite_identifier(column) for column in select_columns) or "*"
            sample_rows = connection.execute(f"SELECT {select_sql} FROM {table_sql} LIMIT ?", (max(1, sample_size),)).fetchall()
            samples = [{key: row[key] for key in row.keys()} for row in sample_rows]
            return {
                "sampled_path": str(path),
                "sampled_table": table,
                "samples": samples,
                "columns": columns,
                "config": {
                    "label": path.stem or "SQLite",
                    "path": str(path),
                    "query": f"SELECT {select_sql} FROM {table_sql} LIMIT :limit",
                },
            }
    except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
        return {"sampled_path": str(path), "sampling_error": str(exc)}


def source_intake_sample_path(path: Path, extension: str) -> dict[str, Any]:
    if extension in {"sqlite", "sqlite3", "db"} and path.is_file():
        return source_intake_sample_sqlite_path(path)
    if extension == "json" and path.is_file():
        return source_intake_sample_manifest_path(path)
    return source_intake_sample_local_path(path)


def source_intake_signals(payload: dict[str, Any]) -> dict[str, Any]:
    text = source_intake_input_text(payload)
    path_text = source_intake_clean_text(payload.get("path")) or text
    url_text = source_intake_clean_text(payload.get("url")) or text
    sql_text = source_intake_clean_text(payload.get("sql")) or text
    has_sql = bool(re.search(r"\bselect\b.+\bfrom\b", sql_text, re.I | re.S))
    json_value = payload.get("sample") if isinstance(payload.get("sample"), (dict, list)) else source_intake_parse_json(text)
    items_path = source_intake_clean_text(payload.get("items_path")) or source_intake_items_path(json_value)
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else source_intake_samples_from_json(json_value, items_path)
    columns = [source_intake_clean_text(item) for item in payload.get("columns") or [] if source_intake_clean_text(item)]
    if not columns:
        columns = source_intake_columns_from_text(text) or source_intake_sql_columns(sql_text)
    extension = source_intake_extension(source_intake_clean_text(payload.get("path")) or source_intake_clean_text(payload.get("url")) or text)
    sample_path = source_intake_existing_path(payload, text, has_json_sample=isinstance(json_value, (dict, list)), has_sql=has_sql)
    path_sample = source_intake_sample_path(sample_path, extension) if sample_path is not None else {}
    url_sample = (
        source_intake_sample_http_json_url(
            payload,
            url_text,
            sample_size=source_intake_http_sample_size(payload),
        )
        if source_intake_sample_url_requested(payload)
        else {}
    )
    if not items_path and path_sample.get("items_path"):
        items_path = source_intake_clean_text(path_sample.get("items_path"))
    if not items_path and url_sample.get("items_path"):
        items_path = source_intake_clean_text(url_sample.get("items_path"))
    if not samples and isinstance(path_sample.get("samples"), list):
        samples = path_sample["samples"]
    if not samples and isinstance(url_sample.get("samples"), list):
        samples = url_sample["samples"]
    if not columns:
        sampled_columns = path_sample.get("columns") if isinstance(path_sample.get("columns"), list) else []
        columns = [source_intake_clean_text(item) for item in sampled_columns if source_intake_clean_text(item)]
    if not columns:
        sampled_columns = url_sample.get("columns") if isinstance(url_sample.get("columns"), list) else []
        columns = [source_intake_clean_text(item) for item in sampled_columns if source_intake_clean_text(item)]
    config = path_sample.get("config") if isinstance(path_sample.get("config"), dict) else {}
    if not config:
        config = url_sample.get("config") if isinstance(url_sample.get("config"), dict) else {}
    if sample_path is not None and extension not in {"json", "sqlite", "sqlite3", "db"} and not config:
        config = {"paths": [str(sample_path)]}
    has_json_sample = (
        isinstance(json_value, (dict, list))
        or (extension == "json" and bool(samples))
        or bool(url_sample.get("samples"))
    )
    return {
        "text": text,
        "extension": extension,
        "has_url": url_text.lower().startswith(("http://", "https://")),
        "has_local_path": source_intake_has_local_path(path_text, has_json_sample=isinstance(json_value, (dict, list)), has_sql=has_sql),
        "has_sql": has_sql,
        "has_json_sample": has_json_sample,
        "items_path": items_path,
        "sample_count": len(samples) if isinstance(samples, list) else 0,
        "columns": columns,
        "column_count": len(columns),
        "samples": samples if isinstance(samples, list) else [],
        "sampled_path": path_sample.get("sampled_path") or "",
        "sample_url_requested": bool(url_sample.get("sample_url_requested")),
        "sampled_url": url_sample.get("sampled_url") or "",
        "sample_query": url_sample.get("sample_query") or "",
        "sampled_file_count": path_sample.get("sampled_file_count") or 0,
        "sampled_table": path_sample.get("sampled_table") or "",
        "sampling_error": path_sample.get("sampling_error") or url_sample.get("sampling_error") or "",
        "config": config,
    }


def source_intake_add_score(scores: dict[str, dict[str, Any]], source_type: str, amount: float, reason: str) -> None:
    entry = scores.setdefault(source_type, {"score": 0.0, "reasons": []})
    entry["score"] += amount
    if reason not in entry["reasons"]:
        entry["reasons"].append(reason)


def source_intake_candidates(payload: dict[str, Any], signals: dict[str, Any]) -> list[dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    explicit = source_intake_clean_text(payload.get("source_type") or payload.get("source"))
    if explicit:
        explicit_normalized = re.sub(r"[\s-]+", "_", explicit.casefold())
        explicit_source = {
            "local": "localfile",
            "local_file": "localfile",
            "local_csv": "localfile",
            "csv": "localfile",
            "jsonl": "localfile",
            "http": "httpjson",
            "http_json": "httpjson",
            "rest": "httpjson",
            "sqlite3": "sqlite",
            "object_manifest": "manifest",
            "objectmanifest": "manifest",
        }.get(explicit_normalized, explicit_normalized)
        if explicit_source in SOURCE_INTAKE_DESCRIPTORS:
            source_intake_add_score(scores, explicit_source, 1.0, "explicit source_type was provided")
    extension = str(signals.get("extension") or "")
    if extension in {"csv", "tsv", "jsonl", "ndjson"}:
        source_intake_add_score(scores, "localfile", 0.9, f".{extension} file extension")
    if extension in {"sqlite", "sqlite3", "db"}:
        source_intake_add_score(scores, "sqlite", 0.95, f".{extension} database extension")
    if extension == "json":
        source_intake_add_score(scores, "manifest", 0.65, ".json file can be an Object Manifest")
        if signals.get("sampled_path") and signals.get("items_path"):
            source_intake_add_score(scores, "manifest", 0.35, "local JSON manifest was sampled")
    if signals.get("has_url"):
        source_intake_add_score(scores, "httpjson", 0.8, "HTTP URL detected")
        if extension == "json" or signals.get("items_path") == "objects":
            source_intake_add_score(scores, "manifest", 0.45, "remote JSON manifest-like payload detected")
    if signals.get("has_sql"):
        source_intake_add_score(scores, "sqlite", 0.8, "SELECT query detected")
    if signals.get("column_count"):
        if extension != "json":
            source_intake_add_score(scores, "localfile", 0.55, "tabular columns detected")
        if signals.get("has_sql"):
            source_intake_add_score(scores, "sqlite", 0.35, "SQL columns detected")
    if signals.get("has_json_sample"):
        source_intake_add_score(scores, "httpjson", 0.55, "JSON sample detected")
        if signals.get("items_path") == "objects":
            source_intake_add_score(scores, "manifest", 0.7, "objects array detected")
        elif signals.get("items_path"):
            source_intake_add_score(scores, "httpjson", 0.25, f"{signals.get('items_path')} array detected")
    if signals.get("has_local_path") and extension not in {"sqlite", "sqlite3", "db"}:
        source_intake_add_score(scores, "localfile", 0.35, "local path detected")
    if not scores:
        source_intake_add_score(scores, "localfile", 0.2, "no strong signal; start with Local CSV/JSONL if it is a file")
        source_intake_add_score(scores, "httpjson", 0.2, "no strong signal; use HTTP JSON if it is an API response")
    candidates: list[dict[str, Any]] = []
    for source_type, score_entry in scores.items():
        descriptor = SOURCE_INTAKE_DESCRIPTORS[source_type]
        raw_score = float(score_entry.get("score") or 0)
        candidates.append(
            {
                "source_type": source_type,
                "label": descriptor["label"],
                "score": round(min(raw_score, 1.0), 2),
                "reasons": score_entry.get("reasons") or [],
                "endpoint": descriptor["endpoint"],
                "required": descriptor["required"],
                "next_action": descriptor["next_action"],
            }
        )
    return sorted(candidates, key=lambda item: item["score"], reverse=True)


def source_intake_field_map_payload(
    payload: dict[str, Any],
    signals: dict[str, Any],
    source_type: str,
) -> tuple[dict[str, Any], str, str]:
    suggestion_payload: dict[str, Any] = {"source_type": source_type}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    if not config:
        config = signals.get("config") if isinstance(signals.get("config"), dict) else {}
    if config:
        suggestion_payload["config"] = config
    if signals.get("items_path"):
        suggestion_payload["items_path"] = signals["items_path"]
    if signals.get("samples"):
        suggestion_payload["samples"] = signals["samples"]
        return suggestion_payload, "samples", json.dumps(signals["samples"], ensure_ascii=False, indent=2)
    if signals.get("columns"):
        suggestion_payload["columns"] = signals["columns"]
        return suggestion_payload, "columns", ", ".join(signals["columns"])
    return {}, "columns", ""


def source_intake_field_map_paths(field_map: dict[str, Any], target: str) -> list[str]:
    value = field_map.get(target)
    if isinstance(value, list):
        raw_paths = value
    else:
        raw_paths = [value]
    return [source_intake_clean_text(path) for path in raw_paths if source_intake_clean_text(path)]


def source_intake_sample_text_for_target(
    sample: dict[str, Any],
    field_map: dict[str, Any],
    target: str,
    fallback_paths: list[str],
) -> str:
    for path in [*source_intake_field_map_paths(field_map, target), *fallback_paths]:
        raw = sample.get(path) if path in sample else source_intake_value_at_path(sample, path)
        text = source_intake_clean_text(local_value_to_text(raw))
        if text:
            return text
    return ""


def source_intake_validation_queries(
    signals: dict[str, Any],
    field_map_suggestion: dict[str, Any],
    *,
    source_type: str,
    seed_query: str = "robot",
    limit: int = 5,
) -> dict[str, Any]:
    samples = signals.get("samples") if isinstance(signals.get("samples"), list) else []
    query_limit = max(1, min(int(limit or 5), 5))
    field_map = field_map_suggestion.get("field_map") if isinstance(field_map_suggestion.get("field_map"), dict) else {}
    by_query: dict[str, dict[str, Any]] = {}
    for index, sample in enumerate(samples[:10], start=1):
        if not isinstance(sample, dict):
            continue
        title = source_intake_sample_text_for_target(
            sample,
            field_map,
            "title",
            ["title", "paper_title", "name", "headline"],
        )
        abstract = source_intake_sample_text_for_target(
            sample,
            field_map,
            "abstract",
            ["abstract", "abstractNote", "description", "summary"],
        )
        tags = source_intake_sample_text_for_target(
            sample,
            field_map,
            "tags",
            ["tags", "keywords", "subjects", "topics"],
        )
        item = {
            "fields": {"title": title, "abstractNote": abstract},
            "tags": [{"tag": tag} for tag in re.split(r"[;,|]", tags) if tag.strip()],
        }
        query, reason = retrieval_query_plan_query_from_item(item, seed_query)
        if not query:
            continue
        by_query.setdefault(
            query,
            {
                "query": query,
                "reason": reason,
                "source": source_type,
                "sample_count": 0,
                "evidence": [],
            },
        )
        record = by_query[query]
        record["sample_count"] = safe_int(record.get("sample_count")) + 1
        record["evidence"].append(
            {
                "sample_index": index,
                "title": title[:180],
                "source": source_type,
            }
        )
    queries = sorted(
        by_query.values(),
        key=lambda item: (-safe_int(item.get("sample_count")), str(item.get("query") or "")),
    )[:query_limit]
    fallback = False
    if not queries and samples and source_intake_clean_text(seed_query):
        fallback = True
        queries = [
            {
                "query": source_intake_clean_text(seed_query),
                "reason": "seed_query",
                "source": source_type,
                "sample_count": 0,
                "evidence": [],
            }
        ]
    if not samples:
        status = "empty"
        message = "No intake samples are available for validation query drafting."
    elif fallback:
        status = "empty"
        message = "Intake samples did not produce query terms; using the seed query as a fallback."
    elif len(queries) >= min(3, query_limit):
        status = "ready"
        message = "Validation query drafts were generated from intake samples."
    else:
        status = "low_sample"
        message = "Validation query drafts were generated, but fewer than 3 distinct queries are available."
    return {
        "status": status,
        "message": message,
        "seed_query": source_intake_clean_text(seed_query),
        "query_count": len(queries),
        "query_text": "\n".join(str(item.get("query") or "") for item in queries if item.get("query")),
        "queries": queries,
    }


def source_intake_validation_plan(
    target_source: dict[str, Any],
    validation_queries: dict[str, Any],
    source_statuses: list[dict[str, Any]] | None = None,
    batch_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_name = source_intake_clean_text(target_source.get("name"))
    query_count = safe_int(validation_queries.get("query_count"))
    minimum_queries = 3
    batch_summary = source_intake_batch_validation_summary(batch_validation)
    batch_validation_status = str(batch_summary.get("status") or "")
    has_batch_evidence = safe_int(batch_summary.get("job_count")) > 0
    status_by_name = {
        str(status.get("name") or ""): status
        for status in source_statuses or []
        if isinstance(status, dict) and status.get("name")
    }
    target_status = status_by_name.get(source_name, {})
    source_status_known = bool(target_status)
    source_available = bool(target_status.get("available")) if source_status_known else None
    source_configured = bool(target_status.get("configured")) if source_status_known else None
    source_message = source_intake_clean_text(target_status.get("message")) if source_status_known else ""
    if not source_name:
        status = "missing_target"
        message = "No target source was identified for validation."
    elif source_status_known and not source_available:
        status = "needs_config"
        message = "Save or fix the target source config before validation."
    elif has_batch_evidence and batch_validation_status:
        status = batch_validation_status
        message = str(batch_summary.get("message") or "Recent batch validation evidence is available.")
    elif query_count >= minimum_queries:
        status = "ready"
        message = "Enough intake validation queries are available for a small batch."
    elif query_count:
        status = "needs_queries"
        message = "Add more validation queries or run PLAN after saving the source config."
    else:
        status = "needs_sample"
        message = "Collect a representative sample before starting validation."
    if source_status_known and source_available:
        save_config_status = "passed"
        save_config_evidence = f"Target source is available in /retrieval/sources. {source_message}".strip()
    elif source_status_known and source_configured:
        save_config_status = "blocked"
        save_config_evidence = f"Target source config is saved but unavailable: {source_message}".strip()
    elif source_status_known:
        save_config_status = "pending"
        save_config_evidence = f"Target source is not configured yet in /retrieval/sources. {source_message}".strip()
    else:
        save_config_status = "pending"
        save_config_evidence = "Target source appears available in /retrieval/sources."
    readiness_status = "pending"
    readiness_evidence = "READY report shows the target source can sample records and map fields."
    if source_status_known:
        readiness_status = "ready" if source_available else "blocked"
        readiness_evidence = (
            "Target source is available; run READY to verify sample and field mapping quality."
            if source_available
            else "Save or fix the target source config before running READY."
        )
    batch_endpoint = "/retrieval/batches"
    if source_status_known and not source_available:
        batch_status = "blocked"
        batch_evidence = f"Target source {source_name} must be available before batch validation."
    elif has_batch_evidence and batch_validation_status:
        batch_status = batch_validation_status
        batch_endpoint = str(batch_summary.get("latest_report_endpoint") or batch_endpoint)
        batch_evidence_parts = [
            str(batch_summary.get("message") or "Recent batch validation evidence is available."),
        ]
        required_queries = safe_int(batch_summary.get("required_completed_queries"))
        if required_queries:
            batch_evidence_parts.append(
                f"Completed {safe_int(batch_summary.get('completed_queries'))}/{required_queries} required queries."
            )
        required_draft_queries = safe_int(batch_summary.get("required_query_count"))
        if required_draft_queries:
            batch_evidence_parts.append(
                f"Draft coverage {safe_int(batch_summary.get('covered_query_count'))}/{required_draft_queries}."
            )
        validated_sources = batch_summary.get("validated_sources") if isinstance(batch_summary.get("validated_sources"), list) else []
        missing_sources = batch_summary.get("missing_sources") if isinstance(batch_summary.get("missing_sources"), list) else []
        if validated_sources:
            batch_evidence_parts.append("Validated source(s): " + ", ".join(str(item) for item in validated_sources) + ".")
        if missing_sources:
            batch_evidence_parts.append("Missing source(s): " + ", ".join(str(item) for item in missing_sources) + ".")
        config_context_status = str(batch_summary.get("config_context_status") or "")
        if config_context_status:
            batch_evidence_parts.append(f"Config context {config_context_status}.")
        remediation = batch_summary.get("remediation") if isinstance(batch_summary.get("remediation"), dict) else {}
        if remediation.get("label"):
            batch_evidence_parts.append(f"Next: {remediation.get('label')}.")
        batch_evidence = " ".join(part for part in batch_evidence_parts if part)
    elif query_count >= minimum_queries:
        batch_status = "ready"
        batch_evidence = f"At least {minimum_queries} drafted queries are ready for {source_name or 'the target source'}."
    elif query_count:
        batch_status = "needs_queries"
        batch_evidence = f"Add queries until at least {minimum_queries} completed queries can validate {source_name or 'the target source'}."
    else:
        batch_status = "needs_queries"
        batch_evidence = f"At least {minimum_queries} completed queries for {source_name or 'the target source'}."
    gates = [
        {
            "name": "save_config",
            "label": "Save target source config",
            "status": save_config_status,
            "endpoint": target_source.get("endpoint", ""),
            "evidence": save_config_evidence,
        },
        {
            "name": "readiness",
            "label": "Run READY preflight",
            "status": readiness_status,
            "endpoint": "/retrieval/readiness/report?format=markdown",
            "evidence": readiness_evidence,
        },
        {
            "name": "batch_validation",
            "label": "Run target-source validation batch",
            "status": batch_status,
            "endpoint": batch_endpoint,
            "evidence": batch_evidence,
        },
        {
            "name": "onboarding",
            "label": "Download ONB evidence",
            "status": "pending",
            "endpoint": "/retrieval/onboarding/report?format=markdown",
            "evidence": "ONB report and ONB ZIP include readiness, batch evidence and handoff artifacts.",
        },
    ]
    return {
        "status": status,
        "message": message,
        "target_source": source_name,
        "minimum_queries": minimum_queries,
        "query_count": query_count,
        "source_status": {
            "known": source_status_known,
            "configured": source_configured,
            "available": source_available,
            "message": source_message,
        },
        "batch_validation": batch_summary,
        "gates": gates,
        "artifacts": source_intake_validation_artifacts(batch_summary),
    }


def source_intake_batch_validation_summary(batch_validation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(batch_validation, dict) or not batch_validation:
        return {}
    summary: dict[str, Any] = {
        "status": str(batch_validation.get("status") or ""),
        "message": str(batch_validation.get("message") or ""),
        "job_count": safe_int(batch_validation.get("job_count")),
        "completed_job_count": safe_int(batch_validation.get("completed_job_count")),
        "active_job_count": safe_int(batch_validation.get("active_job_count")),
        "failed_job_count": safe_int(batch_validation.get("failed_job_count")),
        "canceled_job_count": safe_int(batch_validation.get("canceled_job_count")),
        "completed_queries": safe_int(batch_validation.get("completed_queries")),
        "required_completed_queries": safe_int(batch_validation.get("required_completed_queries")),
        "completed_query_gap": safe_int(batch_validation.get("completed_query_gap")),
        "failed_queries": safe_int(batch_validation.get("failed_queries")),
        "total_candidates": safe_int(batch_validation.get("total_candidates")),
        "latest_job_id": str(batch_validation.get("latest_job_id") or ""),
        "latest_status": str(batch_validation.get("latest_status") or ""),
        "latest_report_endpoint": str(batch_validation.get("latest_report_endpoint") or ""),
        "latest_source_report_endpoint": str(batch_validation.get("latest_source_report_endpoint") or ""),
        "required_query_count": safe_int(batch_validation.get("required_query_count")),
        "covered_query_count": safe_int(batch_validation.get("covered_query_count")),
        "missing_query_count": safe_int(batch_validation.get("missing_query_count")),
        "config_context_status": str(batch_validation.get("config_context_status") or ""),
        "config_matched_job_count": safe_int(batch_validation.get("config_matched_job_count")),
        "config_mismatch_job_count": safe_int(batch_validation.get("config_mismatch_job_count")),
        "config_unknown_job_count": safe_int(batch_validation.get("config_unknown_job_count")),
        "latest_config_fingerprint": str(batch_validation.get("latest_config_fingerprint") or ""),
    }
    remediation = batch_validation.get("remediation") if isinstance(batch_validation.get("remediation"), dict) else {}
    if remediation:
        summary["remediation"] = {
            "action": str(remediation.get("action") or ""),
            "label": str(remediation.get("label") or ""),
            "endpoint": str(remediation.get("endpoint") or ""),
            "method": str(remediation.get("method") or "GET"),
            "queries": [str(item) for item in remediation.get("queries") or [] if str(item or "").strip()],
            "sources": [str(item) for item in remediation.get("sources") or [] if str(item or "").strip()],
            "message": str(remediation.get("message") or ""),
        }
    for key in (
        "required_sources",
        "validated_sources",
        "missing_sources",
        "source_errors",
        "required_queries",
        "covered_queries",
        "missing_queries",
        "completed_query_texts",
    ):
        summary[key] = [str(item) for item in batch_validation.get(key) or [] if str(item or "").strip()]
    source_evidence = batch_validation.get("source_evidence") if isinstance(batch_validation.get("source_evidence"), list) else []
    summary["source_evidence"] = [
        {
            "source": str(item.get("source") or ""),
            "requested": bool(item.get("requested")),
            "query_count": safe_int(item.get("query_count")),
            "success_count": safe_int(item.get("success_count")),
            "failure_count": safe_int(item.get("failure_count")),
            "candidate_count": safe_int(item.get("candidate_count")),
            "latest_error_kind": str(item.get("latest_error_kind") or ""),
            "latest_diagnostic": str(item.get("latest_diagnostic") or ""),
        }
        for item in source_evidence
        if isinstance(item, dict) and str(item.get("source") or "")
    ]
    return summary


def source_intake_validation_artifacts(batch_summary: dict[str, Any] | None = None) -> list[dict[str, str]]:
    artifacts = [
            {"label": "Source intake report", "endpoint": "/retrieval/source-intake/report?format=markdown"},
            {"label": "READY report", "endpoint": "/retrieval/readiness/report?format=markdown"},
            {"label": "Query plan report", "endpoint": "/retrieval/query-plan/report?format=markdown"},
            {"label": "ONB report", "endpoint": "/retrieval/onboarding/report?format=markdown"},
            {"label": "ONB ZIP", "endpoint": "/retrieval/onboarding/package"},
        ]
    if isinstance(batch_summary, dict) and batch_summary.get("latest_report_endpoint"):
        artifacts.append({"label": "Latest batch report", "endpoint": str(batch_summary["latest_report_endpoint"])})
    if isinstance(batch_summary, dict) and batch_summary.get("latest_source_report_endpoint"):
        artifacts.append(
            {"label": "Latest batch source CSV", "endpoint": str(batch_summary["latest_source_report_endpoint"])}
        )
    return artifacts


def retrieval_source_intake_from_payload(
    payload: dict[str, Any],
    *,
    source_statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("source intake payload must be a JSON object")
    signals = source_intake_signals(payload)
    candidates = source_intake_candidates(payload, signals)
    top = candidates[0] if candidates else {}
    source_type = str(top.get("source_type") or "localfile")
    target_source = source_intake_target_source(source_type)
    suggestion_payload, input_mode, field_map_input = source_intake_field_map_payload(payload, signals, source_type)
    field_map_suggestion: dict[str, Any] = {}
    field_map_config = ""
    if suggestion_payload:
        field_map_suggestion = retrieval_field_map_suggestion_from_payload(suggestion_payload)
        if field_map_suggestion.get("config_draft"):
            field_map_config = json.dumps(field_map_suggestion["config_draft"], ensure_ascii=False, indent=2)
    validation_queries = source_intake_validation_queries(
        signals,
        field_map_suggestion,
        source_type=source_type,
        seed_query=source_intake_clean_text(payload.get("query") or payload.get("seed_query")) or "robot",
    )
    validation_plan = source_intake_validation_plan(
        target_source,
        validation_queries,
        source_statuses=source_statuses,
    )
    quality = field_map_suggestion.get("quality") if isinstance(field_map_suggestion.get("quality"), dict) else {}
    status = "ready" if field_map_suggestion and quality.get("status") in {"good", "warning"} else "needs_sample"
    if not field_map_suggestion:
        status = "needs_sample"
    message = (
        f"Likely {top.get('label', source_type)} source. {top.get('next_action', '')}"
        if top
        else "Paste a path, URL, SQL query, columns or JSON sample to classify the source."
    )
    next_steps = [
        f"Open {top.get('endpoint')} and fill required fields: {', '.join(top.get('required') or [])}."
        if top
        else "Collect a path, URL, SQL query, columns or JSON sample.",
        "Use Field map lab or source-specific Suggest to confirm field_map.",
        "Save config, run READY, then run a 3-query batch and ONB.",
    ]
    return {
        "schema": RETRIEVAL_SOURCE_INTAKE_SCHEMA,
        "generated_at": now_iso(),
        "status": status,
        "source_type": source_type,
        "target_source": target_source,
        "confidence": top.get("score", 0),
        "message": message,
        "signals": {
            "extension": signals.get("extension"),
            "has_url": signals.get("has_url"),
            "has_local_path": signals.get("has_local_path"),
            "has_sql": signals.get("has_sql"),
            "has_json_sample": signals.get("has_json_sample"),
            "items_path": signals.get("items_path"),
            "sample_count": signals.get("sample_count"),
            "column_count": signals.get("column_count"),
            "columns": signals.get("columns"),
            "sampled_path": signals.get("sampled_path"),
            "sample_url_requested": signals.get("sample_url_requested"),
            "sampled_url": signals.get("sampled_url"),
            "sample_query": signals.get("sample_query"),
            "sampled_file_count": signals.get("sampled_file_count"),
            "sampled_table": signals.get("sampled_table"),
            "sampling_error": signals.get("sampling_error"),
        },
        "candidates": candidates,
        "field_map_suggestion": field_map_suggestion,
        "field_map_lab": {
            "source_type": source_type,
            "input_mode": input_mode,
            "input": field_map_input,
            "config": field_map_config,
        },
        "validation_queries": validation_queries,
        "validation_plan": validation_plan,
        "next_steps": next_steps,
    }


def retrieval_source_intake_for_library(library_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    source_statuses = retrieval_source_statuses(
        registry=retrieval_provider_registry_for_library(library_id),
    )
    intake = retrieval_source_intake_from_payload(payload, source_statuses=source_statuses)
    target_source = intake.get("target_source") if isinstance(intake.get("target_source"), dict) else {}
    target_source_name = source_intake_clean_text(target_source.get("name"))
    validation_queries = intake.get("validation_queries") if isinstance(intake.get("validation_queries"), dict) else {}
    batch_validation = retrieval_batch_validation_summary(
        library_id,
        required_sources=[target_source_name] if target_source_name else [],
        required_queries=[
            str(item.get("query") or "")
            for item in validation_queries.get("queries") or []
            if isinstance(item, dict) and str(item.get("query") or "").strip()
        ],
    )
    intake["validation_plan"] = source_intake_validation_plan(
        target_source,
        validation_queries,
        source_statuses=source_statuses,
        batch_validation=batch_validation,
    )
    return intake


def source_intake_report_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value if value is not None else "")


def retrieval_source_intake_report_rows(intake: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = [
        {
            "section": "overview",
            "name": "status",
            "value": str(intake.get("status") or ""),
            "details": str(intake.get("message") or ""),
        },
        {
            "section": "overview",
            "name": "source_type",
            "value": str(intake.get("source_type") or ""),
            "details": f"confidence={intake.get('confidence', 0)}",
        },
    ]
    target_source = intake.get("target_source") if isinstance(intake.get("target_source"), dict) else {}
    if target_source:
        rows.append(
            {
                "section": "overview",
                "name": "target_source",
                "value": str(target_source.get("name") or ""),
                "details": "; ".join(
                    str(part)
                    for part in [
                        f"label={target_source.get('label')}" if target_source.get("label") else "",
                        f"endpoint={target_source.get('endpoint')}" if target_source.get("endpoint") else "",
                        "required=" + ", ".join(str(item) for item in target_source.get("required") or [])
                        if target_source.get("required")
                        else "",
                    ]
                    if part
                ),
            }
        )
    signals = intake.get("signals") if isinstance(intake.get("signals"), dict) else {}
    for name, value in signals.items():
        if value in (None, "", [], {}):
            continue
        rows.append(
            {
                "section": "signal",
                "name": str(name),
                "value": source_intake_report_value(value),
                "details": "",
            }
        )
    for candidate in intake.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        rows.append(
            {
                "section": "candidate",
                "name": str(candidate.get("source_type") or ""),
                "value": str(candidate.get("score") or 0),
                "details": "; ".join(
                    [
                        str(candidate.get("label") or ""),
                        "reasons=" + ", ".join(str(item) for item in candidate.get("reasons") or []),
                        "required=" + ", ".join(str(item) for item in candidate.get("required") or []),
                        str(candidate.get("next_action") or ""),
                    ]
                ).strip("; "),
            }
        )
    suggestion = intake.get("field_map_suggestion") if isinstance(intake.get("field_map_suggestion"), dict) else {}
    quality = suggestion.get("quality") if isinstance(suggestion.get("quality"), dict) else {}
    if quality:
        rows.append(
            {
                "section": "field_map_quality",
                "name": str(quality.get("status") or ""),
                "value": str(quality.get("score") if quality.get("score") is not None else ""),
                "details": "; ".join(str(item) for item in quality.get("recommendations") or []),
            }
        )
    for item in suggestion.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or item.get("field") or "")
        source_path = str(item.get("source_path") or item.get("path") or item.get("source") or "")
        rows.append(
            {
                "section": "field_map",
                "name": target,
                "value": source_path,
                "details": "; ".join(
                    str(part)
                    for part in [
                        f"confidence={item.get('confidence')}" if item.get("confidence") is not None else "",
                        item.get("reason") or "",
                    ]
                    if part
                ),
            }
        )
    config_draft = suggestion.get("config_draft") if isinstance(suggestion.get("config_draft"), dict) else {}
    if config_draft:
        rows.append(
            {
                "section": "config_draft",
                "name": str(intake.get("source_type") or ""),
                "value": source_intake_report_value(config_draft),
                "details": "Review before saving to the target source config.",
            }
        )
    validation = intake.get("validation_queries") if isinstance(intake.get("validation_queries"), dict) else {}
    if validation:
        rows.append(
            {
                "section": "validation_query_status",
                "name": str(validation.get("status") or ""),
                "value": str(validation.get("query_count") or 0),
                "details": str(validation.get("message") or ""),
            }
        )
    for item in validation.get("queries") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "section": "validation_query",
                "name": str(item.get("reason") or ""),
                "value": str(item.get("query") or ""),
                "details": "; ".join(
                    str(part)
                    for part in [
                        f"source={item.get('source')}" if item.get("source") else "",
                        f"samples={item.get('sample_count')}" if item.get("sample_count") is not None else "",
                    ]
                    if part
                ),
            }
        )
    validation_plan = intake.get("validation_plan") if isinstance(intake.get("validation_plan"), dict) else {}
    if validation_plan:
        rows.append(
            {
                "section": "validation_plan",
                "name": "status",
                "value": str(validation_plan.get("status") or ""),
                "details": str(validation_plan.get("message") or ""),
            }
        )
        rows.append(
            {
                "section": "validation_plan",
                "name": "minimum_queries",
                "value": str(validation_plan.get("minimum_queries") or 0),
                "details": f"target_source={validation_plan.get('target_source', '')}; query_count={validation_plan.get('query_count', 0)}",
            }
        )
    batch_validation = (
        validation_plan.get("batch_validation") if isinstance(validation_plan.get("batch_validation"), dict) else {}
    )
    if batch_validation:
        rows.append(
            {
                "section": "validation_batch",
                "name": "status",
                "value": str(batch_validation.get("status") or ""),
                "details": str(batch_validation.get("message") or ""),
            }
        )
        rows.append(
            {
                "section": "validation_batch",
                "name": "completed_queries",
                "value": str(batch_validation.get("completed_queries") or 0),
                "details": (
                    f"required={batch_validation.get('required_completed_queries', 0)}; "
                    f"total_candidates={batch_validation.get('total_candidates', 0)}; "
                    f"latest_job_id={batch_validation.get('latest_job_id', '')}; "
                    f"draft_coverage={batch_validation.get('covered_query_count', 0)}/"
                    f"{batch_validation.get('required_query_count', 0)}"
                ),
            }
        )
        if batch_validation.get("config_context_status"):
            rows.append(
                {
                    "section": "validation_batch",
                    "name": "config_context",
                    "value": str(batch_validation.get("config_context_status") or ""),
                    "details": (
                        f"matched={batch_validation.get('config_matched_job_count', 0)}; "
                        f"mismatch={batch_validation.get('config_mismatch_job_count', 0)}; "
                        f"unknown={batch_validation.get('config_unknown_job_count', 0)}; "
                        f"latest_config_fingerprint={batch_validation.get('latest_config_fingerprint', '')}"
                    ),
                }
            )
        remediation = batch_validation.get("remediation") if isinstance(batch_validation.get("remediation"), dict) else {}
        if remediation:
            rows.append(
                {
                    "section": "validation_batch",
                    "name": "remediation",
                    "value": str(remediation.get("action") or ""),
                    "details": (
                        f"label={remediation.get('label', '')}; "
                        f"method={remediation.get('method', 'GET')}; "
                        f"endpoint={remediation.get('endpoint', '')}; "
                        f"queries={len(remediation.get('queries') or [])}; "
                        f"sources={len(remediation.get('sources') or [])}"
                    ),
                }
            )
        if batch_validation.get("required_query_count"):
            rows.append(
                {
                    "section": "validation_batch",
                    "name": "draft_query_coverage",
                    "value": (
                        f"{batch_validation.get('covered_query_count', 0)}/"
                        f"{batch_validation.get('required_query_count', 0)}"
                    ),
                    "details": "missing=" + ", ".join(str(item) for item in batch_validation.get("missing_queries") or []),
                }
            )
        for source_evidence in batch_validation.get("source_evidence") or []:
            if not isinstance(source_evidence, dict):
                continue
            rows.append(
                {
                    "section": "validation_batch_source",
                    "name": str(source_evidence.get("source") or ""),
                    "value": str(source_evidence.get("query_count") or 0),
                    "details": (
                        f"success={source_evidence.get('success_count', 0)}; "
                        f"failure={source_evidence.get('failure_count', 0)}; "
                        f"candidates={source_evidence.get('candidate_count', 0)}; "
                        f"latest_error={source_evidence.get('latest_error_kind', '')}"
                    ),
                }
            )
    for gate in validation_plan.get("gates") or []:
        if not isinstance(gate, dict):
            continue
        rows.append(
            {
                "section": "validation_gate",
                "name": str(gate.get("name") or ""),
                "value": str(gate.get("status") or ""),
                "details": "; ".join(
                    str(part)
                    for part in [
                        gate.get("label") or "",
                        f"endpoint={gate.get('endpoint')}" if gate.get("endpoint") else "",
                        gate.get("evidence") or "",
                    ]
                    if part
                ),
            }
        )
    for artifact in validation_plan.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        rows.append(
            {
                "section": "validation_artifact",
                "name": str(artifact.get("label") or ""),
                "value": str(artifact.get("endpoint") or ""),
                "details": "",
            }
        )
    for index, step in enumerate(intake.get("next_steps") or [], start=1):
        text = str(step or "").strip()
        if not text:
            continue
        rows.append({"section": "next_step", "name": str(index), "value": text, "details": ""})
    return rows


def retrieval_source_intake_markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("\n", " ").replace("|", "\\|")


def render_retrieval_source_intake_report_markdown(intake: dict[str, Any]) -> str:
    signals = intake.get("signals") if isinstance(intake.get("signals"), dict) else {}
    suggestion = intake.get("field_map_suggestion") if isinstance(intake.get("field_map_suggestion"), dict) else {}
    config_draft = suggestion.get("config_draft") if isinstance(suggestion.get("config_draft"), dict) else {}
    validation = intake.get("validation_queries") if isinstance(intake.get("validation_queries"), dict) else {}
    target_source = intake.get("target_source") if isinstance(intake.get("target_source"), dict) else {}
    validation_plan = intake.get("validation_plan") if isinstance(intake.get("validation_plan"), dict) else {}
    lines = [
        "# Retrieval source intake",
        "",
        f"- Generated at: {intake.get('generated_at', '')}",
        f"- Status: {intake.get('status', '')}",
        f"- Likely source: {intake.get('source_type', '')}",
        f"- Target batch source: {target_source.get('name', '') or intake.get('source_type', '')}",
        f"- Target config endpoint: {target_source.get('endpoint', '')}",
        f"- Confidence: {intake.get('confidence', 0)}",
        f"- Message: {intake.get('message', '')}",
        "",
        "## Signals",
        "",
        "| Signal | Value |",
        "| --- | --- |",
    ]
    for name, value in signals.items():
        if value in (None, "", [], {}):
            continue
        lines.append(
            "| "
            + " | ".join(
                retrieval_source_intake_markdown_cell(item)
                for item in [name, source_intake_report_value(value)]
            )
            + " |"
        )
    lines.extend(["", "## Candidate Source Types", "", "| Source | Score | Reasons | Next action |", "| --- | ---: | --- | --- |"])
    for candidate in intake.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                retrieval_source_intake_markdown_cell(item)
                for item in [
                    candidate.get("label") or candidate.get("source_type"),
                    candidate.get("score") or 0,
                    ", ".join(str(reason) for reason in candidate.get("reasons") or []),
                    candidate.get("next_action") or "",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Field Map Draft", "", "| Target | Source path | Confidence | Reason |", "| --- | --- | ---: | --- |"])
    field_map_rows = 0
    for item in suggestion.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        field_map_rows += 1
        lines.append(
            "| "
            + " | ".join(
                retrieval_source_intake_markdown_cell(value)
                for value in [
                    item.get("target") or item.get("field") or "",
                    item.get("source_path") or item.get("path") or item.get("source") or "",
                    item.get("confidence") if item.get("confidence") is not None else "",
                    item.get("reason") or "",
                ]
            )
            + " |"
        )
    if not field_map_rows:
        lines.append("| - | - | 0 | No field map draft available yet. |")
    if config_draft:
        lines.extend(["", "### Config Draft", "", "```json", json.dumps(config_draft, ensure_ascii=False, indent=2), "```"])
    lines.extend(
        [
            "",
            "## Validation Queries",
            "",
            f"- Status: {validation.get('status', '')}",
            f"- Query count: {validation.get('query_count', 0)}",
            f"- Conclusion: {validation.get('message', '')}",
            "",
            "| Query | Reason | Samples |",
            "| --- | --- | ---: |",
        ]
    )
    validation_rows = 0
    for item in validation.get("queries") or []:
        if not isinstance(item, dict):
            continue
        validation_rows += 1
        lines.append(
            "| "
            + " | ".join(
                retrieval_source_intake_markdown_cell(value)
                for value in [item.get("query") or "", item.get("reason") or "", item.get("sample_count") or 0]
            )
            + " |"
        )
    if not validation_rows:
        lines.append("| - | - | 0 | No intake validation query draft available yet. |")
    batch_validation = (
        validation_plan.get("batch_validation") if isinstance(validation_plan.get("batch_validation"), dict) else {}
    )
    lines.extend(
        [
            "",
            "## Validation Plan",
            "",
            f"- Status: {validation_plan.get('status', '')}",
            f"- Minimum completed queries: {validation_plan.get('minimum_queries', 0)}",
            f"- Target source: {validation_plan.get('target_source', '') or target_source.get('name', '')}",
            f"- Conclusion: {validation_plan.get('message', '')}",
            "",
            "| Gate | Status | Endpoint | Evidence |",
            "| --- | --- | --- | --- |",
        ]
    )
    if batch_validation:
        lines.extend(
            [
                f"- Batch evidence: {batch_validation.get('status', '')}",
                f"- Completed batch queries: {batch_validation.get('completed_queries', 0)}/"
                f"{batch_validation.get('required_completed_queries', 0)}",
                f"- Draft query coverage: {batch_validation.get('covered_query_count', 0)}/"
                f"{batch_validation.get('required_query_count', 0)}",
                f"- Validated sources: {', '.join(str(item) for item in batch_validation.get('validated_sources') or [])}",
                f"- Missing sources: {', '.join(str(item) for item in batch_validation.get('missing_sources') or [])}",
                f"- Missing draft queries: {', '.join(str(item) for item in batch_validation.get('missing_queries') or [])}",
                f"- Config context: {batch_validation.get('config_context_status', '')}",
                f"- Remediation: {(batch_validation.get('remediation') or {}).get('label', '')}",
                "",
            ]
        )
    validation_gate_rows = 0
    for gate in validation_plan.get("gates") or []:
        if not isinstance(gate, dict):
            continue
        validation_gate_rows += 1
        lines.append(
            "| "
            + " | ".join(
                retrieval_source_intake_markdown_cell(value)
                for value in [
                    gate.get("label") or gate.get("name") or "",
                    gate.get("status") or "",
                    gate.get("endpoint") or "",
                    gate.get("evidence") or "",
                ]
            )
            + " |"
        )
    if not validation_gate_rows:
        lines.append("| - | - | - | No validation plan available yet. |")
    lines.extend(["", "| Artifact | Endpoint |", "| --- | --- |"])
    validation_artifact_rows = 0
    for artifact in validation_plan.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        validation_artifact_rows += 1
        lines.append(
            "| "
            + " | ".join(
                retrieval_source_intake_markdown_cell(value)
                for value in [artifact.get("label") or "", artifact.get("endpoint") or ""]
            )
            + " |"
        )
    if not validation_artifact_rows:
        lines.append("| - | - |")
    lines.extend(["", "## Next Steps", ""])
    next_steps = [str(step) for step in intake.get("next_steps") or [] if str(step or "").strip()]
    lines.extend(f"- {step}" for step in next_steps) if next_steps else lines.append("- Collect a representative source sample.")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_source_intake_report_csv(intake: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = ["section", "name", "value", "details"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_source_intake_report_rows(intake))
    return output.getvalue()


def render_retrieval_source_intake_report_json(intake: dict[str, Any]) -> str:
    return json.dumps(intake, ensure_ascii=False, indent=2)


def render_retrieval_source_intake_report(intake: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_source_intake_report_csv(intake)
    if normalized == "json":
        return render_retrieval_source_intake_report_json(intake)
    return render_retrieval_source_intake_report_markdown(intake)


def retrieval_field_map_report_rows(suggestion: dict[str, Any]) -> list[dict[str, str]]:
    quality = suggestion.get("quality") if isinstance(suggestion.get("quality"), dict) else {}
    field_map = suggestion.get("field_map") if isinstance(suggestion.get("field_map"), dict) else {}
    ai = suggestion.get("ai_enhancement") if isinstance(suggestion.get("ai_enhancement"), dict) else {}
    rows: list[dict[str, str]] = [
        {
            "section": "overview",
            "name": "source_type",
            "value": str(suggestion.get("source_type") or ""),
            "details": f"generated_at={suggestion.get('generated_at', '')}",
        },
        {
            "section": "overview",
            "name": "quality",
            "value": str(quality.get("status") or ""),
            "details": f"score={quality.get('score') if quality.get('score') is not None else ''}",
        },
        {
            "section": "overview",
            "name": "suggested_field_count",
            "value": str(len(field_map)),
            "details": "",
        },
    ]
    mapped_targets: set[str] = set()
    for item in suggestion.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or item.get("field") or "")
        if target:
            mapped_targets.add(target)
        rows.append(
            {
                "section": "mapping",
                "name": target,
                "value": source_intake_report_value(item.get("source_path") or item.get("path") or item.get("source") or ""),
                "details": "; ".join(
                    str(part)
                    for part in [
                        str(item.get("label") or ""),
                        f"confidence={item.get('confidence')}" if item.get("confidence") is not None else "",
                        "existing" if item.get("existing") is True else "",
                        str(item.get("reason") or ""),
                    ]
                    if part
                ),
            }
        )
    for target, source_path in field_map.items():
        if str(target) in mapped_targets:
            continue
        rows.append(
            {
                "section": "mapping",
                "name": str(target),
                "value": source_intake_report_value(source_path),
                "details": "field_map entry",
            }
        )
    coverage = quality.get("coverage") if isinstance(quality.get("coverage"), dict) else {}
    for name, value in coverage.items():
        rows.append(
            {
                "section": "coverage",
                "name": str(name),
                "value": source_intake_report_value(value),
                "details": "",
            }
        )
    for index, recommendation in enumerate(quality.get("recommendations") or [], start=1):
        text = str(recommendation or "").strip()
        if text:
            rows.append({"section": "recommendation", "name": str(index), "value": text, "details": ""})
    for path in suggestion.get("unmapped_source_paths") or []:
        rows.append({"section": "unmapped_source_path", "name": str(path), "value": "", "details": ""})
    if ai:
        rows.append(
            {
                "section": "ai",
                "name": str(ai.get("status") or "skipped"),
                "value": str(ai.get("applied_field_count") if ai.get("applied_field_count") is not None else ""),
                "details": str(ai.get("message") or ""),
            }
        )
    config_draft = suggestion.get("config_draft") if isinstance(suggestion.get("config_draft"), dict) else {}
    if config_draft:
        rows.append(
            {
                "section": "config_draft",
                "name": str(suggestion.get("source_type") or ""),
                "value": source_intake_report_value(config_draft),
                "details": "Review before saving to the target source config.",
            }
        )
    return rows


def render_retrieval_field_map_report_markdown(suggestion: dict[str, Any]) -> str:
    quality = suggestion.get("quality") if isinstance(suggestion.get("quality"), dict) else {}
    field_map = suggestion.get("field_map") if isinstance(suggestion.get("field_map"), dict) else {}
    config_draft = suggestion.get("config_draft") if isinstance(suggestion.get("config_draft"), dict) else {}
    ai = suggestion.get("ai_enhancement") if isinstance(suggestion.get("ai_enhancement"), dict) else {}
    suggestions_by_target = {
        str(item.get("target") or item.get("field") or ""): item
        for item in suggestion.get("suggestions") or []
        if isinstance(item, dict)
    }
    score = quality.get("score")
    lines = [
        "# Retrieval field map report",
        "",
        f"- Generated at: {suggestion.get('generated_at', '')}",
        f"- Source type: {suggestion.get('source_type', '')}",
        f"- Quality: {quality.get('status', '')}",
        f"- Score: {score if score is not None else ''}",
        f"- Suggested fields: {len(field_map)}",
        "",
        "## Field Map Draft",
        "",
        "| Target | Source path | Confidence | Reason |",
        "| --- | --- | ---: | --- |",
    ]
    if field_map:
        for target, source_path in field_map.items():
            item = suggestions_by_target.get(str(target), {})
            lines.append(
                "| "
                + " | ".join(
                    retrieval_source_intake_markdown_cell(value)
                    for value in [
                        target,
                        source_intake_report_value(source_path),
                        item.get("confidence") if item.get("confidence") is not None else "",
                        item.get("reason") or "field_map entry",
                    ]
                )
                + " |"
            )
    else:
        lines.append("| - | - | 0 | No field_map suggestions. |")
    coverage = quality.get("coverage") if isinstance(quality.get("coverage"), dict) else {}
    if coverage:
        lines.extend(["", "## Quality Coverage", "", "| Area | Covered |", "| --- | --- |"])
        for name, value in coverage.items():
            lines.append(
                "| "
                + " | ".join(retrieval_source_intake_markdown_cell(item) for item in [name, source_intake_report_value(value)])
                + " |"
            )
    recommendations = [str(item) for item in quality.get("recommendations") or [] if str(item or "").strip()]
    if recommendations:
        lines.extend(["", "## Recommendations", ""])
        lines.extend(f"- {item}" for item in recommendations)
    unmapped = [str(item) for item in suggestion.get("unmapped_source_paths") or [] if str(item or "").strip()]
    if unmapped:
        lines.extend(["", "## Unmapped Source Paths", ""])
        lines.extend(f"- `{item}`" for item in unmapped)
    if ai:
        lines.extend(
            [
                "",
                "## AI Enhancement",
                "",
                f"- Requested: {source_intake_report_value(ai.get('requested'))}",
                f"- Status: {ai.get('status', '')}",
                f"- Applied fields: {ai.get('applied_field_count', 0)}",
                f"- Message: {ai.get('message', '')}",
            ]
        )
    if config_draft:
        lines.extend(["", "## Config Draft", "", "```json", json.dumps(config_draft, ensure_ascii=False, indent=2), "```"])
    lines.append("")
    return "\n".join(lines)


def render_retrieval_field_map_report_csv(suggestion: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = ["section", "name", "value", "details"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_field_map_report_rows(suggestion))
    return output.getvalue()


def render_retrieval_field_map_report_json(suggestion: dict[str, Any]) -> str:
    return json.dumps(suggestion, ensure_ascii=False, indent=2)


def render_retrieval_field_map_report(suggestion: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_field_map_report_csv(suggestion)
    if normalized == "json":
        return render_retrieval_field_map_report_json(suggestion)
    return render_retrieval_field_map_report_markdown(suggestion)


def field_map_suggestion_response_for_source(source: str, suggestion: dict[str, Any]) -> dict[str, Any]:
    payload = dict(suggestion)
    if source != "preference":
        payload["config_draft"] = {}
        payload["draft_available"] = False
        payload["message"] = "Suggestion was generated from environment config; config_draft is not returned."
    else:
        payload["draft_available"] = bool(payload.get("config_draft"))
    return payload


def field_map_suggestion_summary_for_readiness(source: str, suggestion: dict[str, Any]) -> dict[str, Any]:
    payload = field_map_suggestion_response_for_source(source, suggestion)
    field_map = payload.get("field_map") if isinstance(payload.get("field_map"), dict) else {}
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    unmapped_paths = payload.get("unmapped_source_paths") if isinstance(payload.get("unmapped_source_paths"), list) else []
    columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []
    draft_available = bool(payload.get("draft_available"))
    config_draft = payload.get("config_draft") if draft_available and isinstance(payload.get("config_draft"), dict) else {}
    recommendations = quality.get("recommendations") if isinstance(quality.get("recommendations"), list) else []
    return {
        "source_type": str(payload.get("source_type") or ""),
        "status": str(quality.get("status") or "unknown"),
        "score": quality.get("score"),
        "field_map": field_map,
        "suggested_field_count": len(field_map),
        "suggestions": suggestions[:12],
        "unmapped_source_path_count": len(unmapped_paths),
        "sample_count": safe_int(payload.get("sample_count")),
        "columns": [str(column) for column in columns[:30]],
        "quality": quality,
        "recommendations": [str(item) for item in recommendations[:6] if str(item or "").strip()],
        "draft_available": draft_available,
        "config_draft": config_draft,
        "message": str(payload.get("message") or ""),
    }


def field_map_suggestion_error_for_readiness(exc: BaseException) -> dict[str, Any]:
    details = retrieval_error_details(exc)
    message = details.get("action") or details.get("error") or str(exc)
    return {
        "source_type": "",
        "status": "error",
        "error_kind": details.get("error_kind") or "field_map_suggestion_error",
        "error": details.get("error") or str(exc),
        "field_map": {},
        "suggested_field_count": 0,
        "suggestions": [],
        "unmapped_source_path_count": 0,
        "sample_count": 0,
        "columns": [],
        "quality": {"status": "error", "recommendations": [message] if message else []},
        "recommendations": [message] if message else [],
        "draft_available": False,
        "config_draft": {},
        "message": message,
    }


def retrieval_readiness_field_map_suggestion(
    name: str,
    raw_config: Any,
    source: str,
    *,
    query: str,
    sample_size: int,
) -> dict[str, Any]:
    try:
        if name == "httpjson":
            suggestion = suggest_http_json_field_map(
                raw_config,
                query=query,
                sample_size=sample_size,
                replace_existing=True,
            )
        elif name == "sqlite":
            suggestion = suggest_sqlite_field_map(
                raw_config,
                query=query,
                sample_size=sample_size,
                replace_existing=True,
            )
        elif name == "manifest":
            suggestion = suggest_manifest_field_map(
                raw_config,
                sample_size=sample_size,
                replace_existing=True,
            )
        else:
            return {}
    except Exception as exc:  # noqa: BLE001 - suggestion must not change readiness status
        return field_map_suggestion_error_for_readiness(exc)
    return field_map_suggestion_summary_for_readiness(source, suggestion)


def normalize_retrieval_batch_queries(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[\r\n]+", value)
    elif isinstance(value, list):
        parts = [str(item or "") for item in value]
    else:
        raise ValueError("queries must be a list or newline-separated text")
    queries: list[str] = []
    seen: set[str] = set()
    for part in parts:
        query = re.sub(r"\s+", " ", str(part or "")).strip()
        key = query.casefold()
        if not query or key in seen:
            continue
        queries.append(query)
        seen.add(key)
    if not queries:
        raise ValueError("batch queries cannot be empty")
    if len(queries) > 50:
        raise ValueError("batch query count cannot exceed 50")
    return queries


def normalize_retrieval_source_limits(value: Any, available_sources: list[str], fallback: int = 10) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    available = {str(source or "").strip().lower() for source in available_sources}
    limits: dict[str, int] = {}
    for key, raw_limit in value.items():
        source = str(key or "").strip().lower()
        if not source or source not in available:
            continue
        try:
            limits[source] = max(1, min(int(raw_limit or fallback), 50))
        except (TypeError, ValueError):
            limits[source] = max(1, min(int(fallback or 10), 50))
    return limits


def normalize_optional_retrieval_queries(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)) and not value:
        return []
    return normalize_retrieval_batch_queries(list(value) if isinstance(value, tuple) else value)


def retrieval_report_filename(run_id: str, fmt: str = "markdown") -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in str(run_id or "retrieval-run")).strip("-")
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"{safe or 'retrieval-run'}-report.{suffix}"


def retrieval_report_mimetype(fmt: str) -> str:
    return {"markdown": "text/markdown", "csv": "text/csv", "json": "application/json"}[normalize_retrieval_report_format(fmt)]


def retrieval_summary_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-summary-report.{suffix}"


def retrieval_source_setup_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-source-setup-report.{suffix}"


def retrieval_source_intake_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-source-intake-report.{suffix}"


def retrieval_field_map_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-field-map-report.{suffix}"


def retrieval_source_field_map_report_filename(source: str, fmt: str = "markdown") -> str:
    safe_source = "".join(char if char.isalnum() or char in "-_" else "-" for char in str(source or "source")).strip("-")
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-{safe_source or 'source'}-field-map-report.{suffix}"


def retrieval_readiness_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-readiness-report.{suffix}"


def retrieval_tuning_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-tuning-report.{suffix}"


def retrieval_onboarding_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-onboarding-report.{suffix}"


def retrieval_query_plan_report_filename(fmt: str = "markdown") -> str:
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalize_retrieval_report_format(fmt)]
    return f"retrieval-query-plan.{suffix}"


def retrieval_onboarding_package_filename() -> str:
    return "retrieval-onboarding-package.zip"


def retrieval_batch_report_filename(job_id: str, fmt: str = "markdown", scope: str = "queries") -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in str(job_id or "retrieval-batch")).strip("-")
    normalized_format = normalize_retrieval_report_format(fmt)
    suffix = {"markdown": "md", "csv": "csv", "json": "json"}[normalized_format]
    scope_suffix = "-sources" if normalized_format == "csv" and normalize_retrieval_batch_report_scope(scope) == "sources" else ""
    return f"{safe or 'retrieval-batch'}-report{scope_suffix}.{suffix}"


def retrieval_import_evidence(
    library_id: str,
    run_id: str,
    candidates: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    clean_run_id = str(run_id or "").strip()
    candidate_values = [candidate for candidate in candidates if isinstance(candidate, dict)]
    result_values = [result for result in results if isinstance(result, dict)]
    statuses = Counter(str(result.get("status") or "unknown") for result in result_values)
    provenance_recorded_count = min(len(candidate_values), len(result_values))
    item_key_count = sum(1 for result in result_values if str(result.get("item_key") or "").strip())
    source_values: set[str] = set()
    for index, candidate in enumerate(candidate_values):
        source = str(candidate.get("source") or "").strip()
        if not source and index < len(result_values):
            source = str(result_values[index].get("source") or "").strip()
        if source:
            source_values.add(source)
    status = "recorded" if provenance_recorded_count else "empty"
    if statuses.get("conflict") or statuses.get("failed"):
        status = "needs_review"
    elif provenance_recorded_count and not clean_run_id:
        status = "recorded_without_run"
    items: list[dict[str, Any]] = []
    for candidate, result in zip(candidate_values, result_values):
        identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
        result_identifiers = result.get("identifiers") if isinstance(result.get("identifiers"), dict) else {}
        items.append(
            {
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "source": str(candidate.get("source") or result.get("source") or ""),
                "title": str(candidate.get("title") or result.get("title") or ""),
                "status": str(result.get("status") or ""),
                "item_key": str(result.get("item_key") or ""),
                "identifiers": identifiers or result_identifiers,
            }
        )
        if len(items) >= 20:
            break
    run_report_endpoint = f"/api/library/{library_id}/retrieval/runs/{clean_run_id}/report" if clean_run_id else ""
    return {
        "status": status,
        "run_id": clean_run_id,
        "run_linked": bool(clean_run_id),
        "candidate_count": len(candidate_values),
        "result_count": len(result_values),
        "provenance_recorded_count": provenance_recorded_count,
        "item_key_count": item_key_count,
        "created_count": int(statuses.get("created", 0)),
        "existing_count": int(statuses.get("existing", 0)),
        "conflict_count": int(statuses.get("conflict", 0)),
        "failed_count": int(statuses.get("failed", 0)),
        "statuses": dict(sorted(statuses.items())),
        "sources": sorted(source_values),
        "run_report_endpoint": run_report_endpoint,
        "run_report_markdown_endpoint": f"{run_report_endpoint}?format=markdown" if run_report_endpoint else "",
        "summary_report_endpoint": f"/api/library/{library_id}/retrieval/summary/report?format=markdown",
        "items": items,
        "item_sample_count": len(items),
        "message": (
            "Import provenance recorded for this retrieval run."
            if clean_run_id
            else "Import provenance recorded without a retrieval run link."
        ),
    }


def retrieval_report_candidate_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    imports_by_candidate = {str(item.get("candidate_id") or ""): item for item in report["imports"]}
    rows: list[dict[str, str]] = []
    for index, candidate in enumerate(report["candidates"], start=1):
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
        import_item = imports_by_candidate.get(str(candidate.get("candidate_id") or ""), {})
        result = import_item.get("payload", {}).get("result", {}) if isinstance(import_item.get("payload"), dict) else {}
        rows.append(
            {
                "rank": str(payload.get("rank") or index),
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "source": str(candidate.get("source") or payload.get("source") or ""),
                "title": str(payload.get("title") or candidate.get("title") or "未命名候选"),
                "identifiers": ", ".join(f"{key}:{value}" for key, value in identifiers.items()),
                "confidence": str(payload.get("confidence_label") or payload.get("confidence") or ""),
                "import_status": str(result.get("status") or import_item.get("status") or "未导入"),
                "item_key": str(result.get("item_key") or import_item.get("item_key") or ""),
            }
        )
    return rows


def render_retrieval_report_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    rows = retrieval_report_candidate_rows(report)
    source_stats = run.get("source_stats") or {}
    lines = [
        "# 多源检索报告",
        "",
        f"- 检索批次：`{run.get('run_id', '')}`",
        f"- 查询词：{run.get('query', '')}",
        f"- 操作者：{run.get('operator', '')}",
        f"- 检索时间：{run.get('created_at', '')}",
        f"- 数据源：{', '.join(run.get('sources') or [])}",
        f"- 候选数：{len(report['candidates'])}",
        f"- 导入记录数：{len(report['imports'])}",
        "",
        "## 数据源统计",
        "",
        "| 数据源 | 状态 | 数量 | 耗时 | 诊断 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for source, stats in source_stats.items():
        status = "成功" if stats.get("ok") else "失败"
        diagnostic = stats.get("action") or stats.get("error") or ""
        if stats.get("error_kind"):
            diagnostic = f"{stats.get('error_kind')}：{diagnostic}"
        safe_diagnostic = str(diagnostic).replace("|", "\\|")
        lines.append(
            f"| {source} | {status} | {stats.get('count', 0)} | {stats.get('elapsed_ms', 0)}ms | {safe_diagnostic} |"
        )
    lines.extend(["", "## 候选与导入结果", "", "| 排名 | 来源 | 标题 | 标识符 | 可信度 | 导入状态 | Zotero Key |", "| ---: | --- | --- | --- | --- | --- | --- |"])
    for row in rows:
        title = row["title"].replace("|", "\\|")
        lines.append(
            "| "
            + " | ".join(
                [
                    row["rank"],
                    row["source"],
                    title,
                    (row["identifiers"] or "-").replace("|", "\\|"),
                    row["confidence"],
                    row["import_status"],
                    row["item_key"],
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def render_retrieval_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = ["rank", "candidate_id", "source", "title", "identifiers", "confidence", "import_status", "item_key"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_report_candidate_rows(report))
    return output.getvalue()


def render_retrieval_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_retrieval_report(report: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_report_csv(report)
    if normalized == "json":
        return render_retrieval_report_json(report)
    return render_retrieval_report_markdown(report)


def retrieval_batch_source_stat_summary(source_stats: dict[str, Any]) -> dict[str, Any]:
    source_counts: list[str] = []
    diagnostics: list[str] = []
    success_count = 0
    failure_count = 0
    elapsed_ms = 0
    for source, stats in sorted(source_stats.items()):
        if not isinstance(stats, dict):
            continue
        count = safe_int(stats.get("count"))
        source_counts.append(f"{source}:{count}")
        elapsed_ms += safe_int(stats.get("elapsed_ms"))
        if stats.get("ok"):
            success_count += 1
        else:
            failure_count += 1
        error_kind = str(stats.get("error_kind") or "").strip()
        diagnostic = str(stats.get("action") or stats.get("error") or "").strip()
        if error_kind or diagnostic:
            diagnostics.append(f"{source}:{error_kind or diagnostic}")
    return {
        "source_counts": ", ".join(source_counts),
        "diagnostics": "; ".join(diagnostics),
        "success_count": success_count,
        "failure_count": failure_count,
        "elapsed_ms": elapsed_ms,
    }


def retrieval_batch_report_rows(job: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in job.get("items") or []:
        if not isinstance(item, dict):
            continue
        source_stats = item.get("source_stats") if isinstance(item.get("source_stats"), dict) else {}
        source_summary = retrieval_batch_source_stat_summary(source_stats)
        query_number = safe_int(item.get("query_index")) + 1
        rows.append(
            {
                "query_number": str(query_number),
                "query": str(item.get("query") or ""),
                "status": str(item.get("status") or ""),
                "run_id": str(item.get("run_id") or ""),
                "candidate_count": str(item.get("candidate_count") or 0),
                "source_success_count": str(source_summary["success_count"]),
                "source_failure_count": str(source_summary["failure_count"]),
                "source_counts": str(source_summary["source_counts"]),
                "elapsed_ms": str(source_summary["elapsed_ms"]),
                "diagnostics": str(item.get("error") or source_summary["diagnostics"] or ""),
                "started_at": str(item.get("started_at") or ""),
                "finished_at": str(item.get("finished_at") or ""),
            }
        )
    return rows


def retrieval_batch_report(job: dict[str, Any]) -> dict[str, Any]:
    rows = retrieval_batch_report_rows(job)
    source_evidence = retrieval_batch_source_evidence(job.get("items") or [], job.get("sources") or [])
    source_errors = [
        str(item.get("source") or "")
        for item in source_evidence
        if str(item.get("source") or "") and safe_int(item.get("failure_count"))
    ]
    completed = safe_int(job.get("completed_queries"))
    failed = safe_int(job.get("failed_queries"))
    total = safe_int(job.get("total_queries"))
    return {
        "generated_at": now_iso(),
        "job": job,
        "summary": {
            "job_id": str(job.get("job_id") or ""),
            "status": str(job.get("status") or ""),
            "total_queries": total,
            "completed_queries": completed,
            "failed_queries": failed,
            "remaining_queries": safe_int(job.get("remaining_queries")),
            "total_candidates": safe_int(job.get("total_candidates")),
            "progress": job.get("progress") or 0,
            "source_count": len(job.get("sources") or []),
            "source_error_count": len(source_errors),
            "source_errors": source_errors,
            "run_count": len(job.get("run_ids") or []),
            "eta_seconds": safe_int(job.get("eta_seconds")),
        },
        "source_evidence": source_evidence,
        "rows": rows,
    }


def retrieval_source_evidence_diagnostic(item: dict[str, Any]) -> str:
    error_kind = str(item.get("latest_error_kind") or "").strip()
    diagnostic = str(item.get("latest_diagnostic") or "").strip()
    if error_kind and diagnostic and error_kind != diagnostic:
        return f"{error_kind}: {diagnostic}"
    return error_kind or diagnostic or "-"


def retrieval_source_evidence_status(item: dict[str, Any]) -> str:
    if safe_int(item.get("failure_count")):
        return "source_errors"
    if safe_int(item.get("query_count")):
        return "passed"
    if item.get("requested"):
        return "missing"
    return "skipped"


def render_retrieval_batch_report_markdown(report: dict[str, Any]) -> str:
    job = report.get("job") if isinstance(report.get("job"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# Retrieval batch report",
        "",
        f"- Generated at: {report.get('generated_at', '')}",
        f"- Batch job: `{summary.get('job_id', '')}`",
        f"- Status: {summary.get('status', '')}",
        f"- Operator: {job.get('operator', '')}",
        f"- Sources: {', '.join(job.get('sources') or [])}",
        f"- Limit per query: {job.get('limit_per_query', '')}",
        f"- Progress: {summary.get('completed_queries', 0)}/{summary.get('total_queries', 0)} queries",
        f"- Failed queries: {summary.get('failed_queries', 0)}",
        f"- Total candidates: {summary.get('total_candidates', 0)}",
        "",
        "## Source summary",
        "",
        "| Source | Requested | Queries | Success | Failures | Candidates | Elapsed | Diagnostic |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("source_evidence") or []:
        if not isinstance(item, dict):
            continue
        cells = [
            item.get("source", ""),
            "yes" if item.get("requested") else "no",
            safe_int(item.get("query_count")),
            safe_int(item.get("success_count")),
            safe_int(item.get("failure_count")),
            safe_int(item.get("candidate_count")),
            f"{safe_int(item.get('elapsed_ms'))}ms",
            retrieval_source_evidence_diagnostic(item),
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines.extend(
        [
            "",
            "## Query results",
            "",
            "| # | Query | Status | Run | Candidates | Sources | Elapsed | Diagnostics |",
            "| ---: | --- | --- | --- | ---: | --- | ---: | --- |",
        ]
    )
    for row in report.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cells = [
            row.get("query_number", ""),
            row.get("query", ""),
            row.get("status", ""),
            row.get("run_id", "") or "-",
            row.get("candidate_count", "0"),
            row.get("source_counts", "") or "-",
            f"{row.get('elapsed_ms', '0')}ms",
            row.get("diagnostics", "") or "-",
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_batch_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "query_number",
        "query",
        "status",
        "run_id",
        "candidate_count",
        "source_success_count",
        "source_failure_count",
        "source_counts",
        "elapsed_ms",
        "diagnostics",
        "started_at",
        "finished_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(report.get("rows") or [])
    return output.getvalue()


def render_retrieval_batch_source_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "source",
        "status",
        "requested",
        "query_count",
        "success_count",
        "failure_count",
        "candidate_count",
        "elapsed_ms",
        "latest_error_kind",
        "latest_diagnostic",
    ]
    rows = []
    for item in report.get("source_evidence") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "source": str(item.get("source") or ""),
                "status": retrieval_source_evidence_status(item),
                "requested": "true" if item.get("requested") else "false",
                "query_count": str(safe_int(item.get("query_count"))),
                "success_count": str(safe_int(item.get("success_count"))),
                "failure_count": str(safe_int(item.get("failure_count"))),
                "candidate_count": str(safe_int(item.get("candidate_count"))),
                "elapsed_ms": str(safe_int(item.get("elapsed_ms"))),
                "latest_error_kind": str(item.get("latest_error_kind") or ""),
                "latest_diagnostic": str(item.get("latest_diagnostic") or ""),
            }
        )
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def render_retrieval_batch_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_retrieval_batch_report(report: dict[str, Any], fmt: str, scope: str = "queries") -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        if normalize_retrieval_batch_report_scope(scope) == "sources":
            return render_retrieval_batch_source_report_csv(report)
        return render_retrieval_batch_report_csv(report)
    if normalized == "json":
        return render_retrieval_batch_report_json(report)
    return render_retrieval_batch_report_markdown(report)


def retrieval_summary_report_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    rows = [
        {
            "section": "totals",
            "name": "阶段合计",
            "run_count": str(totals.get("run_count", 0)),
            "candidate_count": str(totals.get("candidate_count", 0)),
            "imported_count": str(totals.get("imported_count", 0)),
            "success_count": str(totals.get("source_success_count", 0)),
            "failure_count": str(totals.get("source_failure_count", 0)),
            "success_rate": str(totals.get("source_success_rate", 0)),
            "import_rate": str(totals.get("import_rate", 0)),
            "elapsed_avg_ms": "",
            "details": f"统计最近 {summary.get('limit', '')} 个检索批次",
        }
    ]
    sources = summary.get("sources") if isinstance(summary.get("sources"), dict) else {}
    for source, item in sorted(sources.items(), key=lambda pair: (-int(pair[1].get("run_count") or 0), pair[0])):
        run_count = int(item.get("run_count") or 0)
        success_count = int(item.get("success_count") or 0)
        success_rate = round(success_count / run_count, 3) if run_count else 0
        error_kinds = item.get("error_kinds") if isinstance(item.get("error_kinds"), dict) else {}
        detail_parts = [
            ", ".join(f"{key}:{value}" for key, value in error_kinds.items()),
            str(item.get("last_action") or item.get("last_error") or ""),
        ]
        rows.append(
            {
                "section": "source",
                "name": str(source),
                "run_count": str(run_count),
                "candidate_count": str(item.get("candidate_count", 0)),
                "imported_count": "",
                "success_count": str(success_count),
                "failure_count": str(item.get("failure_count", 0)),
                "success_rate": str(success_rate),
                "import_rate": "",
                "elapsed_avg_ms": str(item.get("elapsed_avg_ms", 0)),
                "details": "；".join(part for part in detail_parts if part),
            }
        )
    for query in summary.get("top_queries") or []:
        if not isinstance(query, dict):
            continue
        rows.append(
            {
                "section": "top_query",
                "name": str(query.get("query") or ""),
                "run_count": str(query.get("count", 0)),
                "candidate_count": "",
                "imported_count": "",
                "success_count": "",
                "failure_count": "",
                "success_rate": "",
                "import_rate": "",
                "elapsed_avg_ms": "",
                "details": "高频检索词",
            }
        )
    return rows


def render_retrieval_summary_report_markdown(summary: dict[str, Any]) -> str:
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    sources = summary.get("sources") if isinstance(summary.get("sources"), dict) else {}
    error_kinds = summary.get("error_kinds") if isinstance(summary.get("error_kinds"), dict) else {}
    lines = [
        "# 多源检索阶段统计报告",
        "",
        f"- 生成时间：{summary.get('generated_at', '')}",
        f"- 统计窗口：{summary.get('earliest_run_at', '') or '-'} 至 {summary.get('latest_run_at', '') or '-'}",
        f"- 统计批次数限制：{summary.get('limit', '')}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 检索批次 | {totals.get('run_count', 0)} |",
        f"| 候选条目 | {totals.get('candidate_count', 0)} |",
        f"| 导入记录 | {totals.get('imported_count', 0)} |",
        f"| 导入率 | {totals.get('import_rate', 0)} |",
        f"| 数据源调用 | {totals.get('source_attempt_count', 0)} |",
        f"| 源成功率 | {totals.get('source_success_rate', 0)} |",
        "",
        "## 数据源稳定性",
        "",
        "| 数据源 | 调用 | 成功 | 失败 | 候选 | 平均耗时 | 最近诊断 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for source, item in sorted(sources.items(), key=lambda pair: (-int(pair[1].get("run_count") or 0), pair[0])):
        diagnostic = str(item.get("last_action") or item.get("last_error") or "").replace("|", "\\|") or "-"
        lines.append(
            f"| {source} | {item.get('run_count', 0)} | {item.get('success_count', 0)} | "
            f"{item.get('failure_count', 0)} | {item.get('candidate_count', 0)} | "
            f"{item.get('elapsed_avg_ms', 0)}ms | {diagnostic} |"
        )
    lines.extend(["", "## 错误类型", "", "| 类型 | 次数 |", "| --- | ---: |"])
    if error_kinds:
        for kind, count in sorted(error_kinds.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"| {kind} | {count} |")
    else:
        lines.append("| 无 | 0 |")
    lines.extend(["", "## 高频查询", "", "| 查询词 | 次数 |", "| --- | ---: |"])
    top_queries = [query for query in summary.get("top_queries") or [] if isinstance(query, dict)]
    if top_queries:
        for query in top_queries:
            safe_query = str(query.get("query") or "").replace("|", "\\|")
            lines.append(f"| {safe_query} | {query.get('count', 0)} |")
    else:
        lines.append("| 无 | 0 |")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_summary_report_csv(summary: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "section",
        "name",
        "run_count",
        "candidate_count",
        "imported_count",
        "success_count",
        "failure_count",
        "success_rate",
        "import_rate",
        "elapsed_avg_ms",
        "details",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_summary_report_rows(summary))
    return output.getvalue()


def render_retrieval_summary_report_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2)


def render_retrieval_summary_report(summary: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_summary_report_csv(summary)
    if normalized == "json":
        return render_retrieval_summary_report_json(summary)
    return render_retrieval_summary_report_markdown(summary)


RETRIEVAL_TUNING_MAX_RATE_LIMIT_SECONDS = 10.0
RETRIEVAL_TUNING_LEVEL_ORDER = {
    "fix_config": 0,
    "slow_down": 1,
    "investigate": 2,
    "review_query": 3,
    "collect_data": 4,
    "keep": 5,
}


def retrieval_tuning_level_label(level: str) -> str:
    return {
        "fix_config": "先修配置",
        "slow_down": "调慢限流",
        "investigate": "排查失败",
        "review_query": "检查检索覆盖",
        "collect_data": "继续采样",
        "keep": "保持现状",
    }.get(str(level or ""), "复核")


def increased_rate_limit_seconds(
    current: float,
    *,
    multiplier: float,
    minimum_delta: float,
    minimum: float,
) -> float:
    base = max(0.0, current)
    value = max(base * multiplier, base + minimum_delta, minimum)
    return round(min(value, RETRIEVAL_TUNING_MAX_RATE_LIMIT_SECONDS), 2)


def retrieval_tuning_error_summary(error_kinds: dict[str, int]) -> str:
    if not error_kinds:
        return ""
    return ", ".join(f"{kind}:{count}" for kind, count in sorted(error_kinds.items(), key=lambda pair: (-pair[1], pair[0])))


def retrieval_tuning_recommendation(
    *,
    run_count: int,
    success_count: int,
    failure_count: int,
    candidate_count: int,
    failure_rate: float,
    error_kinds: dict[str, int],
    current_rate_limit_seconds: float,
    rate_limit_env: str,
    global_rate_limit_env: str,
    last_diagnostic: str,
) -> tuple[str, float, str]:
    target_env = rate_limit_env or global_rate_limit_env or "WEB_LIBRARY_RETRIEVAL_RATE_LIMIT_SECONDS"
    recommended = round(max(0.0, current_rate_limit_seconds), 2)
    if run_count <= 0:
        return (
            "collect_data",
            recommended,
            "先用 5-10 条真实查询跑小批量，积累成功率、耗时、候选量和错误类型后再调参。",
        )
    if any(error_kinds.get(kind, 0) for kind in ("configuration", "auth")):
        return (
            "fix_config",
            recommended,
            "先修配置或鉴权，再用小批量复测；当前不建议只靠调慢限流解决。",
        )
    if error_kinds.get("rate_limited", 0):
        recommended = increased_rate_limit_seconds(
            current_rate_limit_seconds,
            multiplier=2.0,
            minimum_delta=1.0,
            minimum=1.0,
        )
        if recommended <= current_rate_limit_seconds:
            action = f"{target_env} 已接近 {RETRIEVAL_TUNING_MAX_RATE_LIMIT_SECONDS:g}s 上限，先降低批量规模或分时段重跑。"
        else:
            action = f"将 {target_env} 调到约 {recommended:g}s 后重跑；若仍出现 429，再按 1.5-2x 放慢。"
        return "slow_down", recommended, action
    transient_failures = sum(error_kinds.get(kind, 0) for kind in ("timeout", "network", "upstream", "http"))
    if transient_failures and failure_rate >= 0.25:
        recommended = increased_rate_limit_seconds(
            current_rate_limit_seconds,
            multiplier=1.5,
            minimum_delta=0.5,
            minimum=0.5,
        )
        if recommended <= current_rate_limit_seconds:
            action = f"保留 {target_env}={current_rate_limit_seconds:g}s，降低批量规模并复测网络/上游稳定性。"
        else:
            action = f"将 {target_env} 调到约 {recommended:g}s，并降低单批查询量后复测。"
        return "slow_down", recommended, action
    if failure_rate >= 0.5:
        diagnostic = last_diagnostic or retrieval_tuning_error_summary(error_kinds) or "最近失败率偏高"
        return "investigate", recommended, f"失败率 {failure_rate:.0%} 偏高；先查看最近诊断：{diagnostic}"
    if success_count and candidate_count == 0:
        return "review_query", recommended, "源调用成功但没有候选；检查检索词、字段映射或该源数据覆盖范围。"
    if run_count < 3:
        return "collect_data", recommended, "样本还少，先继续用真实查询采样，再决定是否调整源级限流。"
    return "keep", recommended, "当前窗口未发现需要调整限流的信号，保持现有设置并继续观察。"


def retrieval_tuning_report(summary: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    summary_sources = summary.get("sources") if isinstance(summary.get("sources"), dict) else {}
    source_statuses = {str(source.get("name") or ""): source for source in sources if isinstance(source, dict)}
    source_names = {str(name) for name in summary_sources if str(name)}
    source_names.update(
        str(source.get("name") or "")
        for source in sources
        if isinstance(source, dict) and source.get("configured") and str(source.get("name") or "")
    )
    rows: list[dict[str, Any]] = []
    for source_name in sorted(source_names):
        stats = summary_sources.get(source_name) if isinstance(summary_sources.get(source_name), dict) else {}
        status = source_statuses.get(source_name, {})
        setup = status.get("setup") if isinstance(status.get("setup"), dict) else {}
        raw_error_kinds = stats.get("error_kinds") if isinstance(stats.get("error_kinds"), dict) else {}
        error_kinds = {str(kind): safe_int(count) for kind, count in raw_error_kinds.items() if str(kind)}
        run_count = safe_int(stats.get("run_count"))
        success_count = safe_int(stats.get("success_count"))
        failure_count = safe_int(stats.get("failure_count"))
        candidate_count = safe_int(stats.get("candidate_count"))
        success_rate = round(success_count / run_count, 3) if run_count else 0
        failure_rate = round(failure_count / run_count, 3) if run_count else 0
        observed_rate_limit = safe_float(stats.get("observed_rate_limit_seconds"))
        current_rate_limit = safe_float(status.get("rate_limit_seconds")) if status else observed_rate_limit
        rate_limit_env = str(setup.get("rate_limit_env") or "")
        global_rate_limit_env = str(setup.get("global_rate_limit_env") or "")
        last_diagnostic = str(stats.get("last_action") or stats.get("last_error") or "")
        level, recommended_rate_limit, action = retrieval_tuning_recommendation(
            run_count=run_count,
            success_count=success_count,
            failure_count=failure_count,
            candidate_count=candidate_count,
            failure_rate=failure_rate,
            error_kinds=error_kinds,
            current_rate_limit_seconds=current_rate_limit,
            rate_limit_env=rate_limit_env,
            global_rate_limit_env=global_rate_limit_env,
            last_diagnostic=last_diagnostic,
        )
        rows.append(
            {
                "source": source_name,
                "label": str(status.get("label") or source_name),
                "available": bool(status.get("available")) if status else True,
                "configured": bool(status.get("configured")) if status else False,
                "run_count": run_count,
                "success_count": success_count,
                "failure_count": failure_count,
                "success_rate": success_rate,
                "failure_rate": failure_rate,
                "candidate_count": candidate_count,
                "elapsed_avg_ms": safe_int(stats.get("elapsed_avg_ms")),
                "rate_limit_wait_avg_ms": safe_int(stats.get("rate_limit_wait_avg_ms")),
                "rate_limit_wait_total_ms": safe_int(stats.get("rate_limit_wait_total_ms")),
                "current_rate_limit_seconds": round(current_rate_limit, 2),
                "observed_rate_limit_seconds": round(observed_rate_limit, 2),
                "recommended_rate_limit_seconds": recommended_rate_limit,
                "rate_limit_env": rate_limit_env,
                "global_rate_limit_env": global_rate_limit_env,
                "error_kinds": error_kinds,
                "error_summary": retrieval_tuning_error_summary(error_kinds),
                "last_diagnostic": last_diagnostic,
                "level": level,
                "level_label": retrieval_tuning_level_label(level),
                "action": action,
            }
        )
    rows.sort(key=lambda row: (RETRIEVAL_TUNING_LEVEL_ORDER.get(str(row.get("level") or ""), 9), -safe_int(row.get("run_count")), str(row.get("source") or "")))
    action_levels = {"fix_config", "slow_down", "investigate", "review_query"}
    summary_payload = {
        "source_count": len(rows),
        "configured_source_count": sum(1 for row in rows if row.get("configured")),
        "run_count": safe_int((summary.get("totals") or {}).get("run_count") if isinstance(summary.get("totals"), dict) else 0),
        "source_attempt_count": safe_int((summary.get("totals") or {}).get("source_attempt_count") if isinstance(summary.get("totals"), dict) else 0),
        "needs_action_count": sum(1 for row in rows if row.get("level") in action_levels),
        "slow_down_count": sum(1 for row in rows if row.get("level") == "slow_down"),
        "fix_config_count": sum(1 for row in rows if row.get("level") == "fix_config"),
        "collect_data_count": sum(1 for row in rows if row.get("level") == "collect_data"),
    }
    if not rows or not summary_payload["source_attempt_count"]:
        status = "no_data"
        message = "还没有可用于调优的检索运行记录。"
    elif summary_payload["fix_config_count"]:
        status = "blocked"
        message = "存在配置或鉴权类问题，先修源配置再调限流。"
    elif summary_payload["slow_down_count"] or any(row.get("level") in {"investigate", "review_query"} for row in rows):
        status = "warning"
        message = "部分源需要调慢、排查失败或复核检索覆盖。"
    elif summary_payload["collect_data_count"]:
        status = "observing"
        message = "当前样本偏少，先继续采样。"
    else:
        status = "healthy"
        message = "当前窗口未发现需要调整限流的信号。"
    if status == "no_data":
        recommendations = ["先用 5-10 条真实查询跑小批量，积累成功率、耗时、候选量和错误类型后再调参。"]
    else:
        recommendations = [f"{row['label']}: {row['action']}" for row in rows if row.get("level") != "keep"][:8]
    if not recommendations:
        recommendations = ["当前窗口没有发现需要调整限流的源，保持现有设置并继续采样。"]
    return {
        "generated_at": now_iso(),
        "status": status,
        "message": message,
        "limit": summary.get("limit", 100),
        "earliest_run_at": summary.get("earliest_run_at", ""),
        "latest_run_at": summary.get("latest_run_at", ""),
        "summary": summary_payload,
        "sources": rows,
        "recommendations": recommendations,
    }


def retrieval_tuning_report_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in report.get("sources") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "source": str(item.get("source") or ""),
                "label": str(item.get("label") or ""),
                "available": str(bool(item.get("available"))).lower(),
                "configured": str(bool(item.get("configured"))).lower(),
                "run_count": str(item.get("run_count") or 0),
                "success_count": str(item.get("success_count") or 0),
                "failure_count": str(item.get("failure_count") or 0),
                "success_rate": str(item.get("success_rate") or 0),
                "failure_rate": str(item.get("failure_rate") or 0),
                "candidate_count": str(item.get("candidate_count") or 0),
                "elapsed_avg_ms": str(item.get("elapsed_avg_ms") or 0),
                "rate_limit_wait_avg_ms": str(item.get("rate_limit_wait_avg_ms") or 0),
                "current_rate_limit_seconds": str(item.get("current_rate_limit_seconds") or 0),
                "recommended_rate_limit_seconds": str(item.get("recommended_rate_limit_seconds") or 0),
                "rate_limit_env": str(item.get("rate_limit_env") or ""),
                "global_rate_limit_env": str(item.get("global_rate_limit_env") or ""),
                "level": str(item.get("level") or ""),
                "action": str(item.get("action") or ""),
                "error_kinds": str(item.get("error_summary") or ""),
                "last_diagnostic": str(item.get("last_diagnostic") or ""),
            }
        )
    return rows


def render_retrieval_tuning_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# 多源检索限流调优报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 调优状态：{report.get('status', '')}",
        f"- 统计窗口：{report.get('earliest_run_at', '') or '-'} 至 {report.get('latest_run_at', '') or '-'}",
        f"- 统计批次数限制：{report.get('limit', '')}",
        f"- 结论：{report.get('message', '')}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 已纳入源 | {summary.get('source_count', 0)} |",
        f"| 检索批次 | {summary.get('run_count', 0)} |",
        f"| 源调用 | {summary.get('source_attempt_count', 0)} |",
        f"| 需要处理 | {summary.get('needs_action_count', 0)} |",
        f"| 建议调慢 | {summary.get('slow_down_count', 0)} |",
        f"| 先修配置 | {summary.get('fix_config_count', 0)} |",
        "",
        "## 源级建议",
        "",
        "| 源 | 调用 | 成功率 | 失败类型 | 当前限流 | 建议限流 | 建议 |",
        "| --- | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    rows = report.get("sources") or []
    if rows:
        for row in rows:
            safe_errors = str(row.get("error_summary") or "-").replace("|", "\\|")
            safe_action = str(row.get("action") or "").replace("|", "\\|")
            lines.append(
                f"| {row.get('label') or row.get('source')} | {row.get('run_count', 0)} | "
                f"{float(row.get('success_rate') or 0):.0%} | {safe_errors} | "
                f"{row.get('current_rate_limit_seconds', 0)}s | {row.get('recommended_rate_limit_seconds', 0)}s | "
                f"{row.get('level_label', '')}：{safe_action} |"
            )
    else:
        lines.append("| - | 0 | 0% | - | 0s | 0s | 先跑一次小批量检索再生成调优建议 |")
    lines.extend(["", "## 操作建议", ""])
    for index, item in enumerate(report.get("recommendations") or [], start=1):
        lines.append(f"{index}. {item}")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_tuning_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "source",
        "label",
        "available",
        "configured",
        "run_count",
        "success_count",
        "failure_count",
        "success_rate",
        "failure_rate",
        "candidate_count",
        "elapsed_avg_ms",
        "rate_limit_wait_avg_ms",
        "current_rate_limit_seconds",
        "recommended_rate_limit_seconds",
        "rate_limit_env",
        "global_rate_limit_env",
        "level",
        "action",
        "error_kinds",
        "last_diagnostic",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_tuning_report_rows(report))
    return output.getvalue()


def render_retrieval_tuning_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_retrieval_tuning_report(report: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_tuning_report_csv(report)
    if normalized == "json":
        return render_retrieval_tuning_report_json(report)
    return render_retrieval_tuning_report_markdown(report)


def retrieval_source_setup_report(sources: list[dict[str, Any]], *, include_health: bool = False) -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "include_health": include_health,
        "source_count": len(sources),
        "available_count": sum(1 for source in sources if source.get("available")),
        "configured_count": sum(1 for source in sources if source.get("configured")),
        "sources": sources,
    }


def retrieval_source_setup_report_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source in report.get("sources") or []:
        if not isinstance(source, dict):
            continue
        setup = source.get("setup") if isinstance(source.get("setup"), dict) else {}
        health = source.get("health") if isinstance(source.get("health"), dict) else {}
        rows.append(
            {
                "name": str(source.get("name") or ""),
                "label": str(source.get("label") or ""),
                "available": str(bool(source.get("available"))).lower(),
                "configured": str(bool(source.get("configured"))).lower(),
                "config_mode": str(setup.get("config_mode") or ""),
                "config_env": str(setup.get("config_env") or ""),
                "alternate_config_env": str(setup.get("alternate_config_env") or ""),
                "preference_api": str(setup.get("preference_api") or ""),
                "rate_limit_seconds": str(source.get("rate_limit_seconds") or 0),
                "rate_limit_env": str(setup.get("rate_limit_env") or ""),
                "global_rate_limit_env": str(setup.get("global_rate_limit_env") or ""),
                "message": str(source.get("message") or ""),
                "health": str(health.get("error_kind") or ("ok" if health.get("ok") else "")),
                "notes": "；".join(str(note) for note in setup.get("notes") or [] if note),
            }
        )
    return rows


def render_retrieval_source_setup_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 多源检索源配置报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 数据源数量：{report.get('source_count', 0)}",
        f"- 可用源：{report.get('available_count', 0)}",
        f"- 已配置源：{report.get('configured_count', 0)}",
        f"- 包含健康检查：{'是' if report.get('include_health') else '否'}",
        "",
        "| 源 | 可用 | 配置模式 | 配置变量 / 文库入口 | 限流 | 状态 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in retrieval_source_setup_report_rows(report):
        config_parts = [
            part
            for part in [
                row["config_env"],
                row["alternate_config_env"],
                row["preference_api"],
            ]
            if part
        ]
        rate_limit = f"{row['rate_limit_seconds']}s / {row['rate_limit_env'] or '-'}"
        if row["global_rate_limit_env"]:
            rate_limit = f"{rate_limit} / {row['global_rate_limit_env']}"
        cells = [
            row["label"] or row["name"],
            "是" if row["available"] == "true" else "否",
            row["config_mode"] or "-",
            "<br>".join(config_parts) or "-",
            rate_limit,
            row["message"] or row["health"] or "-",
            row["notes"] or "-",
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_source_setup_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "name",
        "label",
        "available",
        "configured",
        "config_mode",
        "config_env",
        "alternate_config_env",
        "preference_api",
        "rate_limit_seconds",
        "rate_limit_env",
        "global_rate_limit_env",
        "message",
        "health",
        "notes",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_source_setup_report_rows(report))
    return output.getvalue()


def render_retrieval_source_setup_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_retrieval_source_setup_report(report: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_source_setup_report_csv(report)
    if normalized == "json":
        return render_retrieval_source_setup_report_json(report)
    return render_retrieval_source_setup_report_markdown(report)


def truthy_query_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "health", "check"}


def request_required_queries() -> list[str]:
    values = request.args.getlist("required_queries")
    if len(values) > 1:
        return normalize_optional_retrieval_queries(values)
    value = values[0] if values else request.args.get("batch_queries", "")
    return normalize_optional_retrieval_queries(value)


def replace_existing_field_map_default(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"0", "false", "no"}


def config_value_present(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return bool(str(value or "").strip())


def bounded_retrieval_sample_size(value: Any, default: int = 2) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 5))


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_guided_search_mode(value: Any) -> str:
    mode = str(value or "quality").strip().lower()
    aliases = {"quick": "fast", "full": "coverage", "high_quality": "quality"}
    mode = aliases.get(mode, mode)
    return mode if mode in {"fast", "quality", "coverage"} else "quality"


def normalize_guided_time_range(value: Any, mode: str) -> dict[str, Any]:
    current_year = 2026
    payload = value if isinstance(value, dict) else {"preset": str(value or "").strip()}
    preset = str(payload.get("preset") or "").strip().lower()
    if not preset:
        preset = "5y" if mode == "fast" else "10y" if mode == "quality" else "all"
    start_year = safe_int(payload.get("start_year")) or None
    end_year = safe_int(payload.get("end_year")) or None
    if preset in {"3y", "last3", "recent3"}:
        start_year = current_year - 2
        end_year = current_year
    elif preset in {"5y", "last5", "recent5"}:
        start_year = current_year - 4
        end_year = current_year
    elif preset in {"10y", "last10", "recent10"}:
        start_year = current_year - 9
        end_year = current_year
    elif preset in {"all", "不限", "any"}:
        start_year = None
        end_year = None
    if start_year and end_year and start_year > end_year:
        start_year, end_year = end_year, start_year
    label = "不限" if not start_year and not end_year else f"{start_year or '不限'}-{end_year or '现在'}"
    return {"preset": preset, "start_year": start_year, "end_year": end_year, "label": label}


def normalize_guided_material_types(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else []
    normalized = [normalized_material_type(item) for item in raw_values if normalized_material_type(item)]
    return list(dict.fromkeys(normalized)) or ["paper", "code", "model", "dataset", "benchmark", "website"]


def guided_planner_queries_per_material(mode: str) -> int:
    return 3 if mode == "fast" else 5


def guided_strategy(mode: str) -> dict[str, Any]:
    if mode == "fast":
        return {"query_limit": 120, "limit_per_source": 5, "minimum_queries_per_group": 3, "queries_per_material_type": 3, "sort_mode": "relevance", "auto_expand": False}
    if mode == "coverage":
        return {"query_limit": 160, "limit_per_source": 20, "minimum_queries_per_group": 5, "queries_per_material_type": 5, "sort_mode": "authority", "auto_expand": True}
    return {"query_limit": 120, "limit_per_source": 8, "minimum_queries_per_group": 5, "queries_per_material_type": 5, "sort_mode": "authority", "auto_expand": False}


def normalize_guided_limit_per_source(value: Any, mode: str) -> int:
    default = 5 if mode == "fast" else 8
    parsed = safe_int(value) or default
    return max(1, min(parsed, 50))


def normalize_guided_query_limit(value: Any, mode: str) -> int:
    default = safe_int(guided_strategy(mode).get("query_limit")) or 120
    parsed = safe_int(value) or default
    return max(1, min(parsed, 200))


def guided_search_options(
    mode: str,
    time_range: dict[str, Any],
    material_types: list[str],
    *,
    query_limit: int | None = None,
    limit_per_source: int | None = None,
    source_limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    strategy = guided_strategy(mode)
    return {
        "start_year": time_range.get("start_year"),
        "end_year": time_range.get("end_year"),
        "material_types": material_types,
        "sort_mode": strategy["sort_mode"],
        "strategy_mode": mode,
        "limit_per_source": normalize_guided_limit_per_source(limit_per_source, mode),
        "source_limits": source_limits or {},
    }


def apply_guided_plan_limit(
    plan: dict[str, Any],
    limit_per_source: int,
    source_limits: dict[str, int] | None = None,
    query_limit: int | None = None,
) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return plan
    strategy = plan.get("strategy") if isinstance(plan.get("strategy"), dict) else {}
    next_strategy = {
        **strategy,
        "limit_per_source": normalize_guided_limit_per_source(limit_per_source, str(plan.get("mode") or "quality")),
    }
    if query_limit:
        next_strategy["query_limit"] = normalize_guided_query_limit(query_limit, str(plan.get("mode") or "quality"))
    if source_limits:
        next_strategy["source_limits"] = source_limits
    plan["strategy"] = next_strategy
    return plan


def guided_limit_for_sources(source_limits: dict[str, Any], sources: list[str], fallback: int) -> int:
    limits = [
        safe_int(source_limits.get(str(source or "").strip().lower()))
        for source in sources
        if str(source or "").strip().lower() in source_limits
    ]
    limits = [limit for limit in limits if limit > 0]
    return max(limits) if limits else max(1, min(safe_int(fallback) or 10, 50))


def normalize_retrieval_search_route(value: Any, *, default: str = "legacy") -> str:
    route = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "topic": "keyword",
        "topic_keyword": "keyword",
        "direct": "keyword",
        "exact": "keyword",
        "nl": "natural_language",
        "natural": "natural_language",
        "semantic": "natural_language",
        "agentic": "agent",
    }
    route = aliases.get(route, route)
    if route in {"keyword", "natural_language", "agent", "legacy"}:
        return route
    return default


def normalize_retrieval_expansion_level(value: Any) -> str:
    level = str(value or "balanced").strip().lower()
    return level if level in {"conservative", "balanced", "high_recall"} else "balanced"


def normalize_retrieval_language_policy(value: Any) -> str:
    policy = str(value or "source_adaptive").strip().lower()
    aliases = {"adaptive": "source_adaptive", "all_bilingual": "bilingual_all", "english": "english_only"}
    policy = aliases.get(policy, policy)
    return policy if policy in {"source_adaptive", "bilingual_all", "english_only"} else "source_adaptive"


def retrieval_text_has_chinese(value: Any) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


def retrieval_v4_source_language(source: str, *, input_text: str, language_policy: str) -> str:
    if language_policy == "english_only":
        return "en"
    if language_policy == "bilingual_all":
        return "bilingual" if retrieval_text_has_chinese(input_text) else "en"
    if retrieval_text_has_chinese(input_text) and source in {"brave", "localfile", "httpjson", "sqlite", "manifest"}:
        return "bilingual"
    return "en"


def retrieval_v4_source_query_style(source: str, resource_types: list[str]) -> dict[str, Any]:
    source_key = str(source or "").strip().lower()
    types = {normalized_material_type(item) or str(item) for item in resource_types or []}
    platform_terms = {
        "github": ["GitHub", "github"],
        "gitlab": ["GitLab", "gitlab"],
        "huggingface": ["HuggingFace", "Hugging Face", "huggingface"],
        "zenodo": ["Zenodo", "zenodo"],
        "figshare": ["Figshare", "figshare"],
        "openml": ["OpenML", "openml"],
        "openreview": ["OpenReview", "openreview"],
        "brave": ["Brave", "brave search"],
    }.get(source_key, [])
    if types & {"code"}:
        style = "code_search: implementation-oriented keywords; avoid papers-only words unless the user asks for papers"
    elif types & {"model"}:
        style = "model_search: model/checkpoint/task keywords; include architecture, method alias, and benchmark terms"
    elif types & {"dataset", "benchmark"}:
        style = "data_search: dataset/benchmark/task keywords; include evaluation, leaderboard, corpus, or benchmark aliases"
    elif types & {"website"}:
        style = "web_search: natural web keywords; project, documentation, demo, or official page queries are allowed"
    else:
        style = "scholarly_search: concise academic phrases; include exact topic, synonym, method alias, and survey/evaluation angles"
    return {
        "query_style": style,
        "avoid_terms": platform_terms,
        "platform_name_policy": "do_not_include_source_platform_name_unless_it_is_the_research_topic",
    }


def retrieval_v4_source_catalog(registry: dict[str, Any], sources: list[str]) -> list[dict[str, Any]]:
    statuses = retrieval_source_statuses(registry=registry)
    selected = set(sources)
    catalog: list[dict[str, Any]] = []
    for status in statuses:
        name = str(status.get("name") or "")
        if not name or name not in selected:
            continue
        resource_types = [normalized_material_type(item) or str(item) for item in status.get("resource_types") or []]
        source_style = retrieval_v4_source_query_style(name, resource_types)
        catalog.append(
            {
                "source": name,
                "label": str(status.get("label") or name),
                "available": bool(status.get("available")),
                "configured": bool(status.get("configured")),
                "resource_types": resource_types,
                "source_category": str(status.get("source_category") or ""),
                "query_style": source_style["query_style"],
                "avoid_terms": source_style["avoid_terms"],
                "platform_name_policy": source_style["platform_name_policy"],
            }
        )
    return catalog


def retrieval_v4_sources_for_material(
    source_catalog: list[dict[str, Any]],
    material_type: str,
    fallback_sources: list[str],
) -> list[str]:
    matched = [
        str(item.get("source") or "")
        for item in source_catalog
        if material_type in {normalized_material_type(value) for value in item.get("resource_types") or []}
    ]
    return [source for source in matched if source] or fallback_sources


def retrieval_v4_concept_terms(input_text: str) -> list[str]:
    text = str(input_text or "").strip()
    lower = text.casefold()
    concepts: list[str] = []
    if any(term in lower for term in ["speculative decoding", "speculative sampling", "投机解码", "推测解码"]):
        concepts.extend(
            [
                "speculative decoding",
                "speculative sampling",
                "LLM inference acceleration",
                "assisted generation",
            ]
        )
    if "self-speculative" in lower or "self speculative" in lower:
        concepts.append("self-speculative decoding")
    if "draft model" in lower or "草稿模型" in text:
        concepts.extend(["draft model", "draft model verification"])
    if "model checkpoint" in lower or ("checkpoint" in lower and "model" in lower) or "检查点" in text:
        concepts.extend(["model checkpoint", "draft model checkpoint"])
    if "huggingface" in lower or "hugging face" in lower:
        concepts.append("Hugging Face")
    known: list[tuple[str, list[str]]] = [
        ("投机解码", ["speculative decoding", "speculative sampling", "LLM inference acceleration"]),
        ("双臂", ["dual-arm robot", "bimanual manipulation"]),
        ("机器人", ["robotics", "robot manipulation"]),
        ("操作", ["manipulation"]),
        ("顶会", ["top conference"]),
        ("代码", ["code implementation"]),
        ("模型", ["model"]),
        ("数据集", ["dataset"]),
        ("基准", ["benchmark"]),
        ("榜单", ["leaderboard"]),
        ("自然语言", ["natural language"]),
        ("大模型", ["large language model"]),
        ("推理加速", ["inference acceleration"]),
        ("推测解码", ["speculative decoding"]),
        ("草稿模型", ["draft model", "draft model verification"]),
        ("检查点", ["checkpoint", "model checkpoint"]),
        ("模型检查点", ["model checkpoint", "HuggingFace checkpoint"]),
        ("HuggingFace", ["HuggingFace", "model checkpoint"]),
    ]
    for token, values in known:
        if token in text:
            concepts.extend(values)
    english_terms = [term for term in retrieval_query_plan_terms(text) if re.search(r"[a-z]", term, re.I)]
    concepts.extend(english_terms)
    if not concepts and lower:
        concepts.append(lower)
    return list(dict.fromkeys(concepts))


def retrieval_v4_fallback_query_variants(input_text: str, material: str) -> list[str]:
    lower = str(input_text or "").casefold()
    if any(term in lower for term in ["speculative decoding", "speculative sampling", "投机解码", "推测解码"]):
        speculative_variants = {
            "paper": [
                "speculative decoding",
                "speculative decoding language models",
                "speculative sampling large language models",
                "draft model verification speculative decoding",
                "LLM inference acceleration speculative decoding",
                "self-speculative decoding language models",
                "Medusa speculative decoding",
                "EAGLE speculative decoding",
                "lookahead decoding language models",
            ],
            "code": [
                "speculative decoding implementation",
                "speculative decoding repository",
                "assisted generation draft model code",
                "vLLM speculative decoding",
                "Medusa speculative decoding implementation",
                "EAGLE speculative decoding implementation",
            ],
            "model": [
                "speculative decoding model checkpoint",
                "speculative decoding draft model",
                "assisted generation draft model",
                "Hugging Face speculative decoding model",
                "Hugging Face draft model checkpoint",
                "LLM inference acceleration checkpoint",
            ],
            "dataset": [
                "speculative decoding benchmark dataset",
                "LLM inference acceleration benchmark dataset",
                "speculative decoding evaluation data",
                "draft model evaluation dataset",
            ],
            "benchmark": [
                "speculative decoding benchmark",
                "LLM inference acceleration benchmark",
                "speculative decoding leaderboard",
                "draft model verification benchmark",
            ],
            "website": [
                "speculative decoding project",
                "speculative decoding documentation",
                "Hugging Face speculative decoding",
                "assisted generation documentation",
            ],
        }
        return [
            query
            for query in dict.fromkeys(clean_v4_query_text(item) for item in speculative_variants.get(material, speculative_variants["paper"]))
            if query
        ]
    concepts = retrieval_v4_concept_terms(input_text)
    english_concepts = [term for term in concepts if re.search(r"[a-z]", term, re.I)]
    base_terms = english_concepts or concepts
    base = " ".join(base_terms[:3]).strip() or str(input_text or "").strip()
    variants_by_material = {
        "paper": [
            base,
            f"{base} method",
            f"{base} survey",
            f"{base} benchmark",
            " ".join(base_terms[:2]).strip(),
        ],
        "code": [
            f"{base} implementation",
            f"{base} repository",
            f"{base} code",
            "speculative decoding implementation repository" if "speculative decoding" in base.casefold() else "",
            "draft model verification code" if "draft model" in base.casefold() or "speculative decoding" in base.casefold() else "",
        ],
        "model": [
            f"{base} model checkpoint",
            f"{base} HuggingFace",
            f"{base} draft model",
            "speculative decoding model checkpoint" if "speculative decoding" in base.casefold() else "",
            "assisted generation draft model" if "speculative decoding" in base.casefold() else "",
        ],
        "dataset": [
            f"{base} dataset",
            f"{base} benchmark dataset",
            f"{base} evaluation data",
            "LLM inference acceleration benchmark dataset" if "speculative decoding" in base.casefold() else "",
        ],
        "benchmark": [
            f"{base} benchmark",
            f"{base} leaderboard",
            f"{base} evaluation",
            "LLM inference acceleration benchmark" if "speculative decoding" in base.casefold() else "",
        ],
        "website": [
            f"{base} project",
            f"{base} documentation",
            f"{base} resource",
            "speculative decoding project page" if "speculative decoding" in base.casefold() else "",
        ],
    }
    return [
        query
        for query in dict.fromkeys(clean_v4_query_text(item) for item in variants_by_material.get(material, [base, f"{base} {material}"]))
        if query
    ]


def retrieval_v4_platform_terms_for_query(source: str, material: str) -> list[str]:
    source_key = str(source or "").strip().lower()
    source_terms = {
        "github": ["GitHub", "github"],
        "gitlab": ["GitLab", "gitlab"],
        "huggingface": ["HuggingFace", "Hugging Face", "huggingface"],
        "zenodo": ["Zenodo", "zenodo"],
        "figshare": ["Figshare", "figshare"],
        "openml": ["OpenML", "openml"],
        "openreview": ["OpenReview", "openreview"],
    }.get(source_key, [])
    if material == "website":
        return source_terms
    generic_platform_terms = [
        "GitHub",
        "github",
        "GitLab",
        "gitlab",
        "HuggingFace",
        "Hugging Face",
        "huggingface",
    ]
    return list(dict.fromkeys([*source_terms, *generic_platform_terms]))


def retrieval_v4_clean_source_query(value: Any, *, source: str, material: str) -> str:
    query = clean_v4_query_text(value)
    if not query:
        return ""
    for term in retrieval_v4_platform_terms_for_query(source, material):
        query = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", " ", query)
    query = clean_v4_query_text(query)
    if not query:
        return ""
    if len(retrieval_query_plan_terms(query)) == 1 and len(query) < 10:
        return ""
    return query


def retrieval_v4_fallback_queries_for_source(input_text: str, material: str, source: str, minimum: int) -> list[str]:
    variants = retrieval_v4_fallback_query_variants(input_text, material)
    queries: list[str] = []
    for variant in variants:
        query = retrieval_v4_clean_source_query(variant, source=source, material=material)
        if query and query not in queries:
            queries.append(query)
    if len(queries) >= minimum:
        return queries
    core_terms = [term for term in retrieval_v4_concept_terms(input_text) if term not in {"GitHub", "GitLab", "Hugging Face", "HuggingFace"}]
    core = " ".join(core_terms[:3]).strip() or str(input_text or "").strip()
    source_hints = {
        "paper": ["survey", "method", "evaluation"],
        "code": ["implementation", "repository", "example"],
        "model": ["draft model", "checkpoint", "assisted generation"],
        "dataset": ["benchmark dataset", "evaluation data", "corpus"],
        "benchmark": ["benchmark", "leaderboard", "evaluation"],
        "website": ["project", "documentation", "tutorial"],
    }
    for hint in source_hints.get(material, [material]):
        query = retrieval_v4_clean_source_query(f"{core} {hint}", source=source, material=material)
        if query and query not in queries:
            queries.append(query)
        if len(queries) >= minimum:
            break
    return queries


def retrieval_v4_fallback_query_groups(
    input_text: str,
    *,
    sources: list[str],
    material_types: list[str],
    registry: dict[str, Any],
    language_policy: str,
    limit: int,
    queries_per_group: int = 3,
) -> list[dict[str, Any]]:
    catalog = retrieval_v4_source_catalog(registry, sources)
    concepts = retrieval_v4_concept_terms(input_text)
    suffixes = {
        "paper": ["survey", "method", "benchmark"],
        "code": ["implementation", "repository", "code"],
        "model": ["model", "checkpoint", "HuggingFace"],
        "dataset": ["dataset", "data", "benchmark"],
        "benchmark": ["benchmark", "leaderboard", "evaluation"],
        "website": ["project", "documentation", "resource"],
    }
    groups: list[dict[str, Any]] = []
    per_group = max(3, min(safe_int(queries_per_group) or 3, 12))
    for material in material_types:
        material_sources = retrieval_v4_sources_for_material(catalog, material, sources)
        for source in material_sources:
            language = retrieval_v4_source_language(source, input_text=input_text, language_policy=language_policy)
            variants = retrieval_v4_fallback_queries_for_source(input_text, material, source, per_group)
            if language == "bilingual" and retrieval_text_has_chinese(input_text):
                chinese_query = retrieval_v4_clean_source_query(str(input_text or "").strip(), source=source, material=material)
                if chinese_query:
                    variants.append(chinese_query)
            queries = [query for query in dict.fromkeys(variants) if query][:per_group]
            if not queries:
                base = " ".join(retrieval_v4_concept_terms(input_text)[:3]).strip() or str(input_text or "").strip()
                raw_query = base if material == "paper" else f"{base} {suffixes.get(material, [material])[0]}".strip()
                query = retrieval_v4_clean_source_query(raw_query, source=source, material=material) or clean_v4_query_text(raw_query)
                queries = [query]
            groups.append(
                {
                    "resource_type": material,
                    "source": source,
                    "language": language,
                    "queries": queries,
                    "must_include": concepts[:3],
                    "optional_terms": suffixes.get(material, [])[:3],
                    "exclude_terms": [],
                    "reason": "deterministic_v4_fallback",
                    "confidence": 0.45,
                    "planning_status": "fallback",
                    "planning_message": "AI 未覆盖该源，已用规则补足。",
                }
            )
            if len(groups) >= limit:
                return groups
    return groups


def retrieval_v4_fallback_material_query_groups(
    input_text: str,
    *,
    sources: list[str],
    material_types: list[str],
    registry: dict[str, Any],
    language_policy: str,
    limit: int,
    queries_per_group: int,
) -> list[dict[str, Any]]:
    catalog = retrieval_v4_source_catalog(registry, sources)
    concepts = retrieval_v4_concept_terms(input_text)
    groups: list[dict[str, Any]] = []
    per_group = max(1, min(safe_int(queries_per_group) or 3, 8))
    suffixes = {
        "paper": ["survey", "method", "evaluation"],
        "code": ["implementation", "repository", "code"],
        "model": ["model", "checkpoint", "task"],
        "dataset": ["dataset", "data", "benchmark"],
        "benchmark": ["benchmark", "leaderboard", "evaluation"],
        "website": ["project", "documentation", "resource"],
    }
    extra_suffixes = {
        "paper": ["review", "benchmark", "applications"],
        "code": ["python implementation", "open source implementation", "library"],
        "model": ["draft model", "pretrained model", "architecture"],
        "dataset": ["evaluation dataset", "corpus", "benchmark data"],
        "benchmark": ["evaluation protocol", "comparison", "results"],
        "website": ["tutorial", "demo", "official documentation"],
    }
    for material in material_types:
        material_sources = retrieval_v4_sources_for_material(catalog, material, sources)
        variants = retrieval_v4_fallback_queries_for_source(input_text, material, "", per_group)
        queries = [query for query in dict.fromkeys(variants) if query][:per_group]
        if len(queries) < per_group:
            base = " ".join(retrieval_v4_concept_terms(input_text)[:3]).strip() or str(input_text or "").strip()
            for suffix in extra_suffixes.get(material, []):
                query = retrieval_v4_clean_source_query(f"{base} {suffix}", source="", material=material)
                if query and query not in queries:
                    queries.append(query)
                if len(queries) >= per_group:
                    break
        if len(queries) < per_group:
            base = " ".join(retrieval_v4_concept_terms(input_text)[:3]).strip() or str(input_text or "").strip()
            filler_suffixes = {
                "paper": ["overview", "recent advances", "applications"],
                "code": ["example implementation", "inference implementation", "serving code"],
                "model": ["model card", "task checkpoint", "inference model"],
                "dataset": ["training data", "evaluation corpus", "test set"],
                "benchmark": ["baseline comparison", "evaluation suite", "performance results"],
                "website": ["project page", "technical blog", "documentation"],
            }.get(material, [material])
            for suffix in filler_suffixes:
                query = retrieval_v4_clean_source_query(f"{base} {suffix}", source="", material=material)
                if query and query not in queries:
                    queries.append(query)
                if len(queries) >= per_group:
                    break
        if not queries:
            base = " ".join(retrieval_v4_concept_terms(input_text)[:3]).strip() or str(input_text or "").strip()
            queries = [clean_v4_query_text(f"{base} {suffixes.get(material, [material])[0]}".strip()) or base]
        groups.append(
            {
                "resource_type": material,
                "source": "",
                "sources": material_sources,
                "language": retrieval_v4_source_language(material_sources[0] if material_sources else "", input_text=input_text, language_policy=language_policy),
                "queries": queries,
                "must_include": concepts[:3],
                "optional_terms": suffixes.get(material, [])[:3],
                "exclude_terms": [],
                "reason": "deterministic_v4_material_fallback",
                "confidence": 0.45,
                "planning_status": "fallback",
                "planning_message": "AI 未覆盖该资料类型，已用规则补足。",
            }
        )
        if len(groups) >= limit:
            return groups
    return groups


def retrieval_v4_expected_pairs(
    source_catalog: list[dict[str, Any]],
    sources: list[str],
    material_types: list[str],
    limit: int,
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for material in material_types:
        for source in retrieval_v4_sources_for_material(source_catalog, material, sources):
            key = (str(source or "").strip().lower(), material)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            pairs.append(key)
            if len(pairs) >= limit:
                return pairs
    return pairs


def retrieval_v4_covered_pairs(query_groups: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(group.get("source") or "").strip().lower(), normalized_material_type(group.get("resource_type")) or "paper")
        for group in query_groups
        if str(group.get("source") or "").strip()
    }


def retrieval_v4_covered_materials(query_groups: list[dict[str, Any]]) -> set[str]:
    return {
        normalized_material_type(group.get("resource_type")) or "paper"
        for group in query_groups
        if isinstance(group, dict)
    }


def retrieval_v4_mark_plan_groups(
    query_groups: list[dict[str, Any]],
    *,
    status: str,
    message: str,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    marked: list[dict[str, Any]] = []
    for group in query_groups:
        if not isinstance(group, dict):
            continue
        next_group = dict(group)
        next_group["planning_status"] = status
        next_group["planning_message"] = message
        if reason:
            next_group["planning_reason"] = reason
        marked.append(next_group)
    return marked


def retrieval_v4_source_materials(
    source_catalog: list[dict[str, Any]],
    source: str,
    material_types: list[str],
) -> list[str]:
    source_key = str(source or "").strip().lower()
    selected = set(material_types)
    for item in source_catalog:
        if str(item.get("source") or "").strip().lower() == source_key:
            resource_types = {normalized_material_type(value) or str(value) for value in item.get("resource_types") or []}
            matched = [material for material in material_types if material in resource_types]
            return matched or material_types
    return [material for material in material_types if material in selected]


def retrieval_v4_group_material_statuses(query_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for group in query_groups:
        material = normalized_material_type(group.get("resource_type")) or "paper"
        status = str(group.get("planning_status") or "ai").strip().lower()
        message = str(group.get("planning_message") or "").strip()
        sources = [str(source or "").strip().lower() for source in group.get("sources") or [] if str(source or "").strip()]
        row = statuses.setdefault(material, {"material_type": material, "status": "ai", "sources": [], "messages": []})
        for source in sources:
            if source not in row["sources"]:
                row["sources"].append(source)
        if message and message not in row["messages"]:
            row["messages"].append(message)
        if status in {"fallback", "error"}:
            row["status"] = "fallback" if row["status"] != "partial" else "partial"
        elif status != row["status"] and row["status"] in {"ai", "fallback"}:
            row["status"] = "partial"
    return statuses


def retrieval_v4_group_source_statuses(query_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for group in query_groups:
        group_sources = [str(group.get("source") or "").strip().lower()]
        group_sources.extend(str(source or "").strip().lower() for source in group.get("sources") or [])
        for source in [source for source in dict.fromkeys(group_sources) if source]:
            material = normalized_material_type(group.get("resource_type")) or "paper"
            status = str(group.get("planning_status") or "ai").strip().lower()
            message = str(group.get("planning_message") or "").strip()
            row = statuses.setdefault(source, {"source": source, "status": "ai", "materials": [], "messages": []})
            if material not in row["materials"]:
                row["materials"].append(material)
            if message and message not in row["messages"]:
                row["messages"].append(message)
            if status in {"fallback", "error"}:
                row["status"] = "fallback" if row["status"] != "partial" else "partial"
            elif status != row["status"] and row["status"] in {"ai", "fallback"}:
                row["status"] = "partial"
    return statuses


def retrieval_v4_plan_messages(
    input_text: str,
    *,
    mode: str,
    material_types: list[str],
    source_catalog: list[dict[str, Any]],
    expansion_level: str,
    language_policy: str,
    limit: int,
    compact: bool = False,
) -> list[dict[str, str]]:
    queries_per_material = guided_planner_queries_per_material(mode)
    payload = {
        "input_text": input_text,
        "mode": mode,
        "material_types": material_types,
        "source_catalog": source_catalog,
        "expansion_level": expansion_level,
        "language_policy": language_policy,
        "safety_limit": limit,
        "planning_granularity": "material_type_only",
        "queries_per_material_type": queries_per_material,
        "minimum_queries_per_source": queries_per_material,
        "minimum_queries_per_group": queries_per_material,
        "compact_retry": compact,
        "request_profile": {
            "route": "natural_language_retrieval",
            "goal": "turn vague or multilingual research needs into material-type search keywords",
            "query_count_policy": "mode decides query count per material type: fast=3, quality=5; never bind it to per-source candidate limit",
        },
        "quality_rules": [
            "translate_non_english_to_english_for_english_sources",
            "group_by_material_type_not_by_source",
            "generate_queries_once_per_material_type",
            "source_query_style_must_inform_terms_but_must_not_create_source_groups",
            "do_not_include_platform_name_when_source_already_implies_it",
            "preserve_exact_technical_phrases_and_standard_aliases",
            "each_query_must_have_a_distinct_retrieval_angle_not_suffix_only",
            "exact_query_count_per_material_type",
            "avoid_one_word_or_overly_broad_queries",
            "do_not_invent_papers_ids_urls_or_facts",
        ],
        "bad_query_patterns": [
            "HuggingFace model checkpoint speculative",
            "HuggingFace model checkpoint speculative method",
            "topic method / topic survey / topic benchmark when only suffix changes",
        ],
        "good_query_examples": {
            "paper": [
                "speculative decoding language models",
                "draft model verification speculative decoding",
                "LLM inference acceleration assisted generation",
            ],
            "model": [
                "speculative decoding draft model",
                "assisted generation model checkpoint",
                "Medusa EAGLE speculative decoding",
            ],
            "code": [
                "speculative decoding implementation",
                "assisted generation draft model code",
                "vLLM speculative decoding",
            ],
        },
        "schema": {
            "detected_language": "zh | en | mixed",
            "normalized_topic": "short English topic when possible",
            "constraints": {
                "time_range": "explicit time constraint if any",
                "venue_or_quality": "conference/journal/authority preference if any",
                "must_include": ["core concepts"],
                "exclude_terms": ["negative terms"],
            },
            "query_groups": [
                {
                    "resource_type": "paper | code | model | dataset | benchmark | website",
                    "language": "en | zh | bilingual",
                    "queries": [f"exactly {queries_per_material} search queries for this material type"],
                    "must_include": ["core terms"],
                    "optional_terms": ["aliases, hypernyms, hyponyms, method names"],
                    "exclude_terms": ["negative terms"],
                    "reason": "short reason",
                    "confidence": 0.0,
                }
            ],
            "coverage_targets": ["core_concept", "synonyms", "methods", "applications"],
            "ambiguities": ["unclear points"],
        },
    }
    if compact:
        payload["source_catalog"] = [
            {
                "source": item.get("source"),
                "resource_types": item.get("resource_types"),
                "query_style": item.get("query_style"),
                "avoid_terms": item.get("avoid_terms"),
            }
            for item in source_catalog
        ]
    system_prompt = (
        "Return only a JSON object matching the requested schema. "
        "You are a senior research librarian writing executable search keywords for heterogeneous APIs. "
        "First identify the core concept, synonyms, aliases, broader/narrower terms, methods, applications, and exclusions. "
        "Group query_groups by resource_type only; do not create separate groups for Crossref, arXiv, GitHub, HuggingFace, or any other source. "
        f"Each requested resource_type must contain exactly {queries_per_material} distinct search queries. "
        "For English sources, translate Chinese or mixed input into English search keywords. "
        "For bilingual/custom/web sources, keep Chinese or bilingual queries only when useful. "
        "Every query must be directly usable by the target source; do not write explanations inside query strings. "
        "Use source_query_style from source_catalog only as background for wording, not as a reason to split by source. "
        "Do not include platform/source names like GitHub or HuggingFace unless the user is explicitly asking for that platform as content. "
        "Do not create suffix chains such as 'topic method', 'topic survey', 'topic benchmark' unless each query changes retrieval intent. "
        f"Generate exactly {queries_per_material} queries for each material-type group. "
        "Do not target a fixed total query count and do not use per-source candidate limits as query counts. "
        "Avoid near duplicates, one-word queries, long sentences, URLs, API keys, or invented facts."
    )
    if compact:
        system_prompt = (
            "Return JSON only. Produce a compact source-adaptive retrieval plan. "
            "Group by resource_type only, never by source. "
            "Use English for English sources; keep Chinese only for bilingual/custom/web sources. "
            f"Each requested material type needs exactly {queries_per_material} distinct keyword queries. "
            "No platform-name pollution, no suffix-only variants, no invented facts."
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def clean_v4_query_text(value: Any) -> str:
    query = re.sub(r"\s+", " ", str(value or "").strip())
    if len(query) < 2 or len(query) > 160:
        return ""
    if re.search(r"https?://|authorization|bearer|api[_\s-]*key|secret|password", query, re.I):
        return ""
    return query


def normalize_v4_query_groups(
    raw_payload: Any,
    *,
    input_text: str,
    sources: list[str],
    material_types: list[str],
    registry: dict[str, Any],
    language_policy: str,
    limit: int,
    queries_per_group: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    raw_groups = payload.get("query_groups")
    if not isinstance(raw_groups, list):
        raw_groups = payload.get("queries") if isinstance(payload.get("queries"), list) else []
    catalog = retrieval_v4_source_catalog(registry, sources)
    groups_by_material: dict[str, dict[str, Any]] = {}
    desired_queries = max(1, min(safe_int(queries_per_group) or 3, 8))
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        material = normalized_material_type(raw.get("resource_type") or raw.get("material_type")) or "paper"
        if material not in material_types:
            continue
        raw_queries = raw.get("queries")
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        if not isinstance(raw_queries, list):
            raw_queries = [raw.get("query") or raw.get("query_text")]
        raw_clean_queries = list(dict.fromkeys(retrieval_v4_clean_source_query(query, source="", material=material) for query in raw_queries))
        raw_clean_queries = [query for query in raw_clean_queries if query]
        if not raw_clean_queries:
            continue
        bucket = groups_by_material.setdefault(
            material,
            {
                "resource_type": material,
                "source": "",
                "sources": retrieval_v4_sources_for_material(catalog, material, sources),
                "language": "",
                "queries": [],
                "must_include": [],
                "optional_terms": [],
                "exclude_terms": [],
                "reason": str(raw.get("reason") or raw.get("model_reason") or "natural_language_planner")[:240],
                "confidence": max(0.0, min(safe_float(raw.get("confidence")) or 0.7, 1.0)),
                "planning_status": "ai",
                "planning_message": "AI Planner 按资料类型生成。",
            },
        )
        language = str(raw.get("language") or "").strip().lower()
        if language in {"en", "zh", "bilingual"} and not bucket.get("language"):
            bucket["language"] = language
        for query in raw_clean_queries:
            if query not in bucket["queries"]:
                bucket["queries"].append(query)
        for key, max_count in [("must_include", 6), ("optional_terms", 10), ("exclude_terms", 8)]:
            values = bucket[key]
            for item in raw.get(key) or []:
                text = str(item)[:80].strip()
                if text and text not in values:
                    values.append(text)
                if len(values) >= max_count:
                    break
    groups: list[dict[str, Any]] = []
    for material in material_types:
        bucket = groups_by_material.get(material)
        if not bucket:
            continue
        material_sources = [str(source or "").strip().lower() for source in bucket.get("sources") or [] if str(source or "").strip()]
        if not bucket.get("language"):
            bucket["language"] = retrieval_v4_source_language(material_sources[0] if material_sources else "", input_text=input_text, language_policy=language_policy)
        for fallback_query in retrieval_v4_fallback_queries_for_source(input_text, material, "", desired_queries):
            if len(bucket["queries"]) >= desired_queries:
                break
            if fallback_query not in bucket["queries"]:
                bucket["queries"].append(fallback_query)
        bucket["queries"] = bucket["queries"][:desired_queries]
        if len(bucket["queries"]) < desired_queries:
            continue
        groups.append(bucket)
        if len(groups) >= limit:
            break
    metadata = {
        "detected_language": str(payload.get("detected_language") or ("zh" if retrieval_text_has_chinese(input_text) else "en")),
        "normalized_topic": str(payload.get("normalized_topic") or "").strip()[:160],
        "constraints": payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {},
        "coverage_targets": payload.get("coverage_targets") if isinstance(payload.get("coverage_targets"), list) else [],
        "ambiguities": payload.get("ambiguities") if isinstance(payload.get("ambiguities"), list) else [],
    }
    return groups, metadata


def flatten_v4_query_groups(query_groups: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in query_groups:
        source = str(group.get("source") or "").strip().lower()
        group_sources = [str(item or "").strip().lower() for item in group.get("sources") or [] if str(item or "").strip()]
        if source and source not in group_sources:
            group_sources.insert(0, source)
        material = normalized_material_type(group.get("resource_type")) or "paper"
        for query in group.get("queries") or []:
            query_text = clean_v4_query_text(query)
            if not query_text:
                continue
            key = (",".join(group_sources), query_text.casefold())
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                {
                    "query": query_text,
                    "query_text": query_text,
                    "intent": str(group.get("reason") or material),
                    "reason": str(group.get("reason") or ""),
                    "resource_type": material,
                    "source": source,
                    "sources": group_sources,
                    "language": str(group.get("language") or ""),
                    "must_include": group.get("must_include") or [],
                    "optional_terms": group.get("optional_terms") or [],
                    "exclude_terms": group.get("exclude_terms") or [],
                    "confidence": group.get("confidence"),
                }
            )
            if len(queries) >= limit:
                return queries
    return queries


def retrieval_v4_plan_from_groups(
    *,
    input_text: str,
    topic: str,
    mode: str,
    strategy: dict[str, Any],
    search_route: str,
    expansion_level: str,
    language_policy: str,
    query_groups: list[dict[str, Any]],
    metadata: dict[str, Any],
    ai_enhancement: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    queries = flatten_v4_query_groups(query_groups, safe_int(strategy.get("query_limit")) or 5)
    coverage_targets = metadata.get("coverage_targets") if isinstance(metadata.get("coverage_targets"), list) else []
    if not coverage_targets:
        coverage_targets = [
            "core_concept",
            "translation",
            "synonyms",
            "hypernyms",
            "hyponyms",
            "methods",
            "applications",
            "paper",
            "code",
            "model",
            "dataset",
            "benchmark",
            "website",
            "time_range",
            "sources",
        ]
    return {
        "schema": "web-library.retrieval-query-plan/v4",
        "planner_version": "v4",
        "search_route": search_route,
        "input_text": input_text,
        "topic": topic,
        "mode": mode,
        "strategy": strategy,
        "expansion_level": expansion_level,
        "language_policy": language_policy,
        "detected_language": metadata.get("detected_language") or ("zh" if retrieval_text_has_chinese(input_text) else "en"),
        "normalized_topic": metadata.get("normalized_topic") or topic,
        "constraints": metadata.get("constraints") if isinstance(metadata.get("constraints"), dict) else {},
        "query_groups": query_groups,
        "queries": queries,
        "query_count": len(queries),
        "coverage_targets": coverage_targets,
        "ambiguities": metadata.get("ambiguities") if isinstance(metadata.get("ambiguities"), list) else [],
        "message": message,
        "ai_enhancement": ai_enhancement,
    }


def retrieval_keyword_plan_for_library(
    library_id: str,
    *,
    topic: str,
    mode: str,
    sources: list[str],
    material_types: list[str],
    expansion_level: str = "balanced",
    language_policy: str = "source_adaptive",
    query_limit: int | None = None,
) -> dict[str, Any]:
    strategy = {**guided_strategy(mode), "query_limit": normalize_guided_query_limit(query_limit, mode)}
    registry = retrieval_provider_registry_for_library(library_id)
    query_groups: list[dict[str, Any]] = []
    catalog = retrieval_v4_source_catalog(registry, sources)
    for material in material_types:
        for source in retrieval_v4_sources_for_material(catalog, material, sources):
            query_groups.append(
                {
                    "resource_type": material,
                    "source": source,
                    "language": retrieval_v4_source_language(source, input_text=topic, language_policy=language_policy),
                    "queries": [topic],
                    "must_include": [topic],
                    "optional_terms": [],
                    "exclude_terms": [],
                    "reason": "keyword_exact_match",
                    "confidence": 1.0,
                }
            )
            if len(query_groups) >= strategy["query_limit"]:
                break
        if len(query_groups) >= strategy["query_limit"]:
            break
    if not query_groups:
        query_groups = [
            {
                "resource_type": "paper",
                "source": sources[0] if sources else "",
                "language": "en",
                "queries": [topic],
                "must_include": [topic],
                "optional_terms": [],
                "exclude_terms": [],
                "reason": "keyword_exact_match",
                "confidence": 1.0,
            }
        ]
    return retrieval_v4_plan_from_groups(
        input_text=topic,
        topic=topic,
        mode=mode,
        strategy=strategy,
        search_route="keyword",
        expansion_level=expansion_level,
        language_policy=language_policy,
        query_groups=query_groups,
        metadata={"detected_language": "zh" if retrieval_text_has_chinese(topic) else "en", "normalized_topic": topic},
        ai_enhancement={"requested": False, "status": "skipped", "message": "主题词检索不进行 AI 拆解。"},
        message="主题词检索已按原始输入生成直接检索任务。",
    )


def retrieval_natural_language_plan_for_library(
    library_id: str,
    *,
    input_text: str,
    mode: str,
    sources: list[str],
    material_types: list[str],
    expansion_level: str = "balanced",
    language_policy: str = "source_adaptive",
    query_limit: int | None = None,
    ai_post_json: Any = None,
) -> dict[str, Any]:
    strategy = {**guided_strategy(mode), "query_limit": normalize_guided_query_limit(query_limit, mode)}
    registry = retrieval_provider_registry_for_library(library_id)
    catalog = retrieval_v4_source_catalog(registry, sources)
    with use_ai_pixel_config(api_config_model_for_library(library_id)):
        model = retrieval_model_status()
        if not model.get("configured"):
            raise ValueError("模型未配置，无法进行自然语言检索；请先配置模型，或切换为主题词检索。")
        ai_enhancement: dict[str, Any] = {
            "requested": True,
            "configured": True,
            "provider": model.get("provider"),
            "base_url": model.get("base_url"),
            "model": model.get("model"),
            "status": "applied",
            "message": "AI natural-language planner generated source-adaptive query groups.",
        }
        query_groups: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        planner_attempts: list[dict[str, Any]] = []

        def call_v4_planner(messages: list[dict[str, str]], *, max_tokens: int, timeout_seconds: int) -> dict[str, Any]:
            if callable(ai_post_json):
                return ai_pixel_chat_json(
                    messages,
                    post_json=ai_post_json,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
            return ai_pixel_chat_json(
                messages,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )

        for compact_attempt in (False, True):
            attempt_label = "compact_retry" if compact_attempt else "primary"
            try:
                messages = retrieval_v4_plan_messages(
                    input_text,
                    mode=mode,
                    material_types=material_types,
                    source_catalog=catalog,
                    expansion_level=expansion_level,
                    language_policy=language_policy,
                    limit=strategy["query_limit"],
                    compact=compact_attempt,
                )
                raw_plan = call_v4_planner(
                    messages,
                    max_tokens=1600 if compact_attempt else 2200,
                    timeout_seconds=RETRIEVAL_PLANNER_RETRY_TIMEOUT_SECONDS if compact_attempt else RETRIEVAL_PLANNER_TIMEOUT_SECONDS,
                )
                query_groups, metadata = normalize_v4_query_groups(
                    raw_plan,
                    input_text=input_text,
                    sources=sources,
                    material_types=material_types,
                    registry=registry,
                    language_policy=language_policy,
                    limit=strategy["query_limit"],
                    queries_per_group=safe_int(strategy.get("queries_per_material_type") or strategy.get("minimum_queries_per_group")) or guided_planner_queries_per_material(mode),
                )
                planner_attempts.append(
                    {
                        "attempt": attempt_label,
                        "status": "applied" if query_groups else "empty",
                        "query_group_count": len(query_groups),
                    }
                )
                if query_groups:
                    if compact_attempt:
                        ai_enhancement["message"] = "AI natural-language planner generated source-adaptive query groups after compact retry."
                        ai_enhancement["retry"] = {"used": True}
                    else:
                        ai_enhancement["retry"] = {"used": False}
                    break
            except Exception as exc:  # noqa: BLE001 - retry once before deterministic fallback.
                planner_attempts.append(
                    {
                        "attempt": attempt_label,
                        "status": "error",
                        "message": str(exc or exc.__class__.__name__)[:240],
                    }
                )

        def append_material_fallbacks(
            groups: list[dict[str, Any]],
            materials: list[str],
            *,
            message: str,
            reason: str,
        ) -> list[dict[str, Any]]:
            covered = retrieval_v4_covered_materials(groups)
            values = list(groups)
            for material in materials:
                if len(values) >= strategy["query_limit"] or material in covered:
                    continue
                fallback_groups = retrieval_v4_fallback_material_query_groups(
                    input_text,
                    sources=sources,
                    material_types=[material],
                    registry=registry,
                    language_policy=language_policy,
                    limit=1,
                    queries_per_group=safe_int(strategy.get("queries_per_material_type") or strategy.get("minimum_queries_per_group")) or guided_planner_queries_per_material(mode),
                )
                marked = retrieval_v4_mark_plan_groups(
                    fallback_groups,
                    status="fallback",
                    message=message,
                    reason=reason,
                )
                values.extend(marked)
                covered.update(retrieval_v4_covered_materials(marked))
            return values

        if query_groups:
            query_groups = append_material_fallbacks(
                query_groups,
                material_types,
                message="AI 未覆盖该资料类型，已用规则补足。",
                reason="missing_material_fallback",
            )

        if not query_groups:
            material_retry_groups: list[dict[str, Any]] = []
            material_metadata: dict[str, Any] = {}
            for material in material_types:
                if len(material_retry_groups) >= strategy["query_limit"]:
                    break
                material_sources = retrieval_v4_sources_for_material(catalog, material, sources)
                material_sources = list(dict.fromkeys(material_sources))
                if not material_sources:
                    continue
                material_catalog = [
                    item
                    for item in catalog
                    if str(item.get("source") or "").strip().lower() in set(material_sources)
                ]
                try:
                    raw_plan = call_v4_planner(
                        retrieval_v4_plan_messages(
                            input_text,
                            mode=mode,
                            material_types=[material],
                            source_catalog=material_catalog,
                            expansion_level=expansion_level,
                            language_policy=language_policy,
                            limit=max(1, strategy["query_limit"] - len(material_retry_groups)),
                            compact=True,
                        ),
                        max_tokens=1200,
                        timeout_seconds=RETRIEVAL_PLANNER_MATERIAL_TIMEOUT_SECONDS,
                    )
                    material_groups, material_metadata = normalize_v4_query_groups(
                        raw_plan,
                        input_text=input_text,
                        sources=material_sources,
                        material_types=[material],
                        registry=registry,
                        language_policy=language_policy,
                        limit=max(1, strategy["query_limit"] - len(material_retry_groups)),
                        queries_per_group=safe_int(strategy.get("queries_per_material_type") or strategy.get("minimum_queries_per_group")) or guided_planner_queries_per_material(mode),
                    )
                    material_groups = retrieval_v4_mark_plan_groups(
                        material_groups,
                        status="ai",
                        message="全量规划不稳定，已按资料类型重试生成。",
                        reason="material_retry",
                    )
                    planner_attempts.append(
                        {
                            "attempt": "material_retry",
                            "material": material,
                            "status": "applied" if material_groups else "empty",
                            "query_group_count": len(material_groups),
                        }
                    )
                    material_retry_groups.extend(material_groups)
                    material_retry_groups = append_material_fallbacks(
                        material_retry_groups,
                        [material],
                        message="该资料类型未被 AI 小批规划覆盖，已用规则补足。",
                        reason="material_retry_missing_material_fallback",
                    )
                except Exception as exc:  # noqa: BLE001 - failed material chunk falls back by material type.
                    message = str(exc or exc.__class__.__name__)[:160]
                    planner_attempts.append(
                        {
                            "attempt": "material_retry",
                            "material": material,
                            "status": "error",
                            "message": message,
                        }
                    )
                    material_retry_groups = append_material_fallbacks(
                        material_retry_groups,
                        [material],
                        message=f"该资料类型 AI 小批规划未返回，已用规则补足：{message}",
                        reason="material_retry_error_fallback",
                    )
            if material_retry_groups:
                query_groups = material_retry_groups[: strategy["query_limit"]]
                metadata = {
                    **metadata,
                    **{key: value for key, value in material_metadata.items() if value},
                    "detected_language": metadata.get("detected_language") or ("zh" if retrieval_text_has_chinese(input_text) else "en"),
                    "normalized_topic": metadata.get("normalized_topic") or material_metadata.get("normalized_topic") or " ".join(retrieval_v4_concept_terms(input_text)[:4]) or input_text,
                    "ambiguities": ["部分资料类型由规则补足；请查看每组状态。"],
                }

        ai_enhancement["attempts"] = planner_attempts
        if not query_groups:
            ai_enhancement["message"] = "; ".join(
                str(attempt.get("message") or f"{attempt.get('attempt')} returned no usable query groups")
                for attempt in planner_attempts
            )[:360]
            query_groups = retrieval_v4_fallback_material_query_groups(
                input_text,
                sources=sources,
                material_types=material_types,
                registry=registry,
                language_policy=language_policy,
                limit=strategy["query_limit"],
                queries_per_group=safe_int(strategy.get("queries_per_material_type") or strategy.get("minimum_queries_per_group")) or guided_planner_queries_per_material(mode),
            )
            metadata = {
                "detected_language": "zh" if retrieval_text_has_chinese(input_text) else "en",
                "normalized_topic": " ".join(retrieval_v4_concept_terms(input_text)[:4]) or input_text,
                "ambiguities": ["模型规划未稳定返回，已按资料类型生成规则兜底计划。"],
            }
        if not query_groups:
            query_groups = retrieval_v4_fallback_material_query_groups(
                input_text,
                sources=sources,
                material_types=material_types,
                registry=registry,
                language_policy=language_policy,
                limit=strategy["query_limit"],
                queries_per_group=safe_int(strategy.get("queries_per_material_type") or strategy.get("minimum_queries_per_group")) or guided_planner_queries_per_material(mode),
            )
            metadata = {
                "detected_language": "zh" if retrieval_text_has_chinese(input_text) else "en",
                "normalized_topic": " ".join(retrieval_v4_concept_terms(input_text)[:4]) or input_text,
                "ambiguities": ["模型规划未返回可用检索词，已按资料类型生成规则兜底计划。"],
            }
        ai_group_count = sum(1 for group in query_groups if str(group.get("planning_status") or "").lower() == "ai")
        fallback_group_count = sum(1 for group in query_groups if str(group.get("planning_status") or "").lower() == "fallback")
        if ai_group_count and fallback_group_count:
            ai_enhancement["status"] = "partial"
            ai_enhancement["message"] = "已生成可编辑计划；部分资料类型使用规则补足，请查看每组状态。"
        elif ai_group_count:
            ai_enhancement["status"] = "applied"
            ai_enhancement["message"] = ai_enhancement.get("message") or "AI natural-language planner generated source-adaptive query groups."
        else:
            ai_enhancement["status"] = "fallback"
            ai_enhancement["message"] = "已生成可编辑计划；模型规划未稳定返回，所有数据源已用规则兜底。"
        ai_enhancement["ai_group_count"] = ai_group_count
        ai_enhancement["fallback_group_count"] = fallback_group_count
        ai_enhancement["material_statuses"] = retrieval_v4_group_material_statuses(query_groups)
        ai_enhancement["source_statuses"] = retrieval_v4_group_source_statuses(query_groups)
    topic = str(metadata.get("normalized_topic") or input_text).strip()
    return retrieval_v4_plan_from_groups(
        input_text=input_text,
        topic=topic,
        mode=mode,
        strategy=strategy,
        search_route="natural_language",
        expansion_level=expansion_level,
        language_policy=language_policy,
        query_groups=query_groups,
        metadata=metadata,
        ai_enhancement=ai_enhancement,
        message="自然语言检索计划已生成：已按资料类型拆解检索词，并会自动分发到对应数据源。",
    )


def default_guided_sources_for_materials(registry: dict[str, Any], material_types: list[str]) -> list[str]:
    selected_types = set(material_types or ["paper", "code", "model", "dataset", "benchmark", "website"])
    statuses = retrieval_source_statuses(registry=registry)
    values: list[str] = []
    for status in statuses:
        name = str(status.get("name") or "")
        if not name or not status.get("available"):
            continue
        source_types = {str(item) for item in status.get("resource_types") or []}
        if source_types & selected_types:
            values.append(name)
    return values or [str(name) for name in registry]


def guided_sources_for_material(
    sources: list[str],
    material_type: str,
    source_statuses: dict[str, dict[str, Any]],
) -> list[str]:
    matched: list[str] = []
    for source in sources:
        status = source_statuses.get(source) or {}
        source_types = {str(item) for item in status.get("resource_types") or []}
        if material_type in source_types:
            matched.append(source)
    return matched or sources


def guided_material_query_text(query: str, material_type: str) -> str:
    text = str(query or "").strip()
    hints = {
        "paper": "",
        "code": "implementation repository code",
        "model": "model checkpoint HuggingFace",
        "dataset": "dataset data",
        "benchmark": "benchmark leaderboard evaluation",
        "website": "project website documentation",
    }
    hint = hints.get(material_type, "")
    if not hint or hint.casefold() in text.casefold():
        return text
    return f"{text} {hint}".strip()


def normalize_guided_plan_queries(plan: dict[str, Any], topic: str, sources: list[str], limit: int) -> list[dict[str, Any]]:
    raw_queries = plan.get("queries") if isinstance(plan.get("queries"), list) else []
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_queries:
        if isinstance(item, dict):
            query = str(item.get("query_text") or item.get("query") or "").strip()
            reason = str(item.get("reason") or item.get("model_reason") or "覆盖主题不同表达").strip()
            intent = str(item.get("intent") or item.get("type") or "topic").strip()
            material_type = normalized_material_type(item.get("resource_type") or item.get("material_type") or item.get("material"))
            item_sources = [str(source or "").strip().lower() for source in item.get("sources") or [] if str(source or "").strip()]
        else:
            query = str(item or "").strip()
            reason = "覆盖主题不同表达"
            intent = "topic"
            material_type = ""
            item_sources = []
        if not query:
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(
            {
                "query": query,
                "query_text": query,
                "intent": intent,
                "reason": reason,
                "resource_type": material_type,
                "sources": [source for source in item_sources if source in sources] or sources,
            }
        )
        if len(values) >= limit:
            break
    if not values:
        values.append({"query": topic, "query_text": topic, "intent": "core", "reason": "原始主题", "resource_type": "", "sources": sources})
    return values


def guided_plan_queries_by_material(
    queries: list[dict[str, Any]],
    *,
    material_types: list[str],
    sources: list[str],
    registry: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    statuses = {
        str(status.get("name") or ""): status
        for status in retrieval_source_statuses(registry=registry)
        if isinstance(status, dict)
    }
    selected_materials = material_types or ["paper", "code", "model", "dataset", "benchmark", "website"]
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    explicit = [query for query in queries if normalized_material_type(query.get("resource_type"))]
    if explicit:
        for query in explicit:
            material = normalized_material_type(query.get("resource_type"))
            query_text = str(query.get("query_text") or query.get("query") or "").strip()
            key = (query_text.casefold(), material)
            if not query_text or key in seen:
                continue
            seen.add(key)
            values.append(
                {
                    **query,
                    "resource_type": material,
                    "sources": guided_sources_for_material(list(query.get("sources") or sources), material, statuses),
                }
            )
            if len(values) >= limit:
                return values
    base_queries = queries or [{"query": "", "query_text": ""}]
    for material in selected_materials:
        for base in base_queries:
            base_text = str(base.get("query_text") or base.get("query") or "").strip() or str(base.get("topic") or "")
            query_text = guided_material_query_text(base_text or "", material) or guided_material_query_text(str(base.get("reason") or ""), material)
            query_text = query_text or guided_material_query_text(str(base.get("query") or ""), material)
            if not query_text:
                continue
            key = (query_text.casefold(), material)
            if key in seen:
                continue
            seen.add(key)
            values.append(
                {
                    **base,
                    "query": query_text,
                    "query_text": query_text,
                    "intent": str(base.get("intent") or material),
                    "resource_type": material,
                    "sources": guided_sources_for_material(list(base.get("sources") or sources), material, statuses),
                }
            )
            break
        if len(values) >= limit:
            break
    return values[:limit] or queries[:limit]


def guided_search_plan_for_library(
    library_id: str,
    *,
    topic: str,
    mode: str,
    sources: list[str],
    material_types: list[str],
    use_ai_planning: bool,
    search_route: str = "legacy",
    input_text: str | None = None,
    expansion_level: str = "balanced",
    language_policy: str = "source_adaptive",
    query_limit: int | None = None,
) -> dict[str, Any]:
    route = normalize_retrieval_search_route(search_route, default="legacy")
    clean_input = str(input_text or topic or "").strip()
    clean_expansion = normalize_retrieval_expansion_level(expansion_level)
    clean_language_policy = normalize_retrieval_language_policy(language_policy)
    if route == "keyword":
        return retrieval_keyword_plan_for_library(
            library_id,
            topic=topic,
            mode=mode,
            sources=sources,
            material_types=material_types,
            expansion_level=clean_expansion,
            language_policy=clean_language_policy,
            query_limit=query_limit,
        )
    if route == "natural_language":
        return retrieval_natural_language_plan_for_library(
            library_id,
            input_text=clean_input or topic,
            mode=mode,
            sources=sources,
            material_types=material_types,
            expansion_level=clean_expansion,
            language_policy=clean_language_policy,
            query_limit=query_limit,
        )
    if route == "agent":
        raise ValueError("智能体检索实验入口需要使用多源检索 skill/CLI；当前 guided job 只执行主题词和自然语言工作流。")
    strategy = {**guided_strategy(mode), "query_limit": normalize_guided_query_limit(query_limit, mode)}
    registry = retrieval_provider_registry_for_library(library_id)
    try:
        base_plan = retrieval_query_plan_for_library(
            library_id,
            seed_query=topic,
            sample_size=5,
            limit=strategy["query_limit"],
            use_ai=use_ai_planning,
            selected_sources=sources,
        )
    except Exception as exc:  # noqa: BLE001 - guided search can still run with rule variants
        base_plan = {"queries": retrieval_query_plan_seed_variants(topic, sources, strategy["query_limit"]), "message": str(exc)}
    queries = normalize_guided_plan_queries(base_plan, topic, sources, strategy["query_limit"])
    queries = guided_plan_queries_by_material(
        queries,
        material_types=material_types,
        sources=sources,
        registry=registry,
        limit=strategy["query_limit"],
    )
    return {
        "topic": topic,
        "mode": mode,
        "strategy": strategy,
        "queries": queries,
        "query_count": len(queries),
        "coverage_targets": [
            "core_concept",
            "synonyms",
            "methods",
            "applications",
            "paper",
            "code",
            "model",
            "dataset",
            "benchmark",
            "website",
            "time_range",
            "sources",
        ],
        "message": str(base_plan.get("message") or "引导式检索计划已生成。"),
        "ai_enhancement": base_plan.get("ai_enhancement") if isinstance(base_plan.get("ai_enhancement"), dict) else {},
    }


def guided_search_coverage(
    *,
    job: dict[str, Any],
    candidates: list[dict[str, Any]],
    auto_expanded: bool = False,
) -> dict[str, Any]:
    material_counts = {"paper": 0, "code": 0, "model": 0, "dataset": 0, "benchmark": 0, "website": 0}
    source_counts: dict[str, int] = {}
    authority_count = 0
    multi_source_count = 0
    missing_authority_count = 0
    for candidate in candidates:
        material = normalized_material_type(candidate.get("resource_type")) or candidate_material_type(str(candidate.get("item_type") or ""), str(candidate.get("source") or ""))
        if material == "data":
            material = "dataset"
        if material in material_counts:
            material_counts[material] += 1
        for source in candidate.get("sources") or [candidate.get("source")]:
            clean_source = str(source or "").strip()
            if clean_source:
                source_counts[clean_source] = source_counts.get(clean_source, 0) + 1
        if "authority" in (candidate.get("coverage_tags") or []):
            authority_count += 1
        if candidate.get("multi_source"):
            multi_source_count += 1
        if candidate.get("missing_authority_signals"):
            missing_authority_count += 1
    selected_materials = [
        normalized_material_type(item)
        for item in (job.get("material_types") if isinstance(job.get("material_types"), list) else ["paper", "code", "model", "dataset", "benchmark", "website"])
        if normalized_material_type(item)
    ]
    selected_sources = job.get("sources") if isinstance(job.get("sources"), list) else []
    covered_materials = [name for name in selected_materials if material_counts.get(name, 0) > 0]
    missing = []
    for name in selected_materials:
        if material_counts.get(name, 0) <= 0:
            missing.append({"area": name, "reason": "该资料类型暂未检到候选"})
    uncovered_sources = [source for source in selected_sources if source not in source_counts]
    for source in uncovered_sources[:6]:
        missing.append({"area": f"source:{source}", "reason": "该数据源暂无候选或本轮失败"})
    if candidates and authority_count <= 0:
        missing.append({"area": "authority", "reason": "暂未检到明确引用、权威来源或使用热度信号"})
    status = "good" if not missing else "needs_more"
    return {
        "status": status,
        "candidate_count": len(candidates),
        "covered_materials": covered_materials,
        "material_counts": material_counts,
        "source_counts": source_counts,
        "authority_count": authority_count,
        "multi_source_count": multi_source_count,
        "missing_authority_count": missing_authority_count,
        "missing": missing,
        "auto_expanded": auto_expanded,
        "message": "覆盖良好，可进入筛选导入。" if status == "good" else "仍有覆盖缺口，可查看建议 query 或继续补检。",
    }


def guided_gap_queries(topic: str, coverage: dict[str, Any], sources: list[str]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    missing_areas = {str(item.get("area") or "") for item in coverage.get("missing") or [] if isinstance(item, dict)}
    if "code" in missing_areas:
        queries.append({"query": f"{topic} github implementation code", "query_text": f"{topic} github implementation code", "intent": "code", "reason": "补检代码实现", "sources": [source for source in sources if source in {"github", "zenodo", "huggingface"}] or sources})
    if "data" in missing_areas:
        queries.append({"query": f"{topic} dataset benchmark", "query_text": f"{topic} dataset benchmark", "intent": "data", "reason": "补检数据集和 benchmark", "sources": [source for source in sources if source in {"datacite", "zenodo", "huggingface"}] or sources})
    if "model" in missing_areas:
        queries.append({"query": f"{topic} model checkpoint HuggingFace", "query_text": f"{topic} model checkpoint HuggingFace", "intent": "model", "resource_type": "model", "reason": "supplement model resources", "sources": [source for source in sources if source in {"huggingface", "zenodo"}] or sources})
    if "dataset" in missing_areas:
        queries.append({"query": f"{topic} dataset data", "query_text": f"{topic} dataset data", "intent": "dataset", "resource_type": "dataset", "reason": "supplement datasets", "sources": [source for source in sources if source in {"datacite", "zenodo", "huggingface", "figshare", "osf", "openml"}] or sources})
    if "benchmark" in missing_areas:
        queries.append({"query": f"{topic} benchmark leaderboard evaluation", "query_text": f"{topic} benchmark leaderboard evaluation", "intent": "benchmark", "resource_type": "benchmark", "reason": "supplement benchmarks", "sources": [source for source in sources if source in {"openml", "brave", "github"}] or sources})
    if "website" in missing_areas:
        queries.append({"query": f"{topic} project website documentation", "query_text": f"{topic} project website documentation", "intent": "website", "resource_type": "website", "reason": "supplement websites", "sources": [source for source in sources if source in {"brave"}] or sources})
    if "authority" in missing_areas:
        queries.append({"query": f"{topic} survey benchmark state of the art", "query_text": f"{topic} survey benchmark state of the art", "intent": "authority", "reason": "补检综述、基准和高影响资料", "sources": sources})
    return queries[:3]


def bounded_retrieval_summary_limit(value: Any, default: int = 100) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 500))


def worst_retrieval_quality_status(statuses: list[str]) -> str:
    order = {"good": 0, "skipped": 1, "empty": 2, "warning": 3, "poor": 4, "error": 5}
    clean_statuses = [str(status or "empty") for status in statuses]
    if not clean_statuses:
        return "empty"
    return max(clean_statuses, key=lambda status: order.get(status, 3))


def retrieval_quality_summary_from_preview(source_name: str, preview: dict[str, Any]) -> dict[str, Any]:
    if source_name != "localfile":
        quality = preview.get("quality") if isinstance(preview.get("quality"), dict) else {}
        return {
            "status": str(quality.get("status") or "empty"),
            "score": quality.get("score", 0),
            "row_count": safe_int(quality.get("row_count")),
            "rows_with_issues": safe_int(quality.get("rows_with_issues")),
            "rows_with_errors": safe_int(quality.get("rows_with_errors")),
            "recommendations": list(quality.get("recommendations") or [])[:6],
        }
    files = [item for item in (preview.get("files") or []) if isinstance(item, dict)]
    qualities = [
        item.get("quality")
        for item in files
        if isinstance(item.get("quality"), dict)
    ]
    recommendations: list[str] = []
    for quality in qualities:
        for recommendation in quality.get("recommendations") or []:
            text = str(recommendation or "").strip()
            if text and text not in recommendations:
                recommendations.append(text)
    return {
        "status": worst_retrieval_quality_status([str(quality.get("status") or "") for quality in qualities]),
        "score": round(sum(float(quality.get("score") or 0) for quality in qualities) / len(qualities), 2) if qualities else 0,
        "file_count": len(files),
        "row_count": sum(safe_int(quality.get("row_count")) for quality in qualities),
        "rows_with_issues": sum(safe_int(quality.get("rows_with_issues")) for quality in qualities),
        "rows_with_errors": sum(safe_int(quality.get("rows_with_errors")) for quality in qualities),
        "recommendations": recommendations[:6],
    }


def retrieval_preview_sample_count(source_name: str, preview: dict[str, Any]) -> int:
    if source_name == "localfile":
        return sum(len(file.get("samples") or []) for file in preview.get("files") or [] if isinstance(file, dict))
    return len(preview.get("samples") or [])


def local_file_field_map_suggestion_summary_for_readiness(preview: dict[str, Any], source: str) -> dict[str, Any]:
    files = [item for item in (preview.get("files") or []) if isinstance(item, dict)]
    file_summaries: list[dict[str, Any]] = []
    combined_field_map: dict[str, Any] = {}
    recommendations: list[str] = []
    statuses: list[str] = []
    sample_count = 0
    unmapped_count = 0
    for file in files:
        suggestion = file.get("field_map_suggestion") if isinstance(file.get("field_map_suggestion"), dict) else {}
        if not suggestion:
            continue
        summary = field_map_suggestion_summary_for_readiness(source, suggestion)
        summary["file"] = str(file.get("name") or file.get("path") or "")
        summary["path"] = str(file.get("path") or "")
        if source == "preference":
            summary["config_draft"] = {"field_map": summary.get("field_map") or {}}
            summary["draft_available"] = bool(summary.get("field_map"))
        else:
            summary["draft_available"] = False
            summary["config_draft"] = {}
        field_map = summary.get("field_map") if isinstance(summary.get("field_map"), dict) else {}
        for target, source_path in field_map.items():
            combined_field_map.setdefault(str(target), source_path)
        for recommendation in summary.get("recommendations") or []:
            text = str(recommendation or "").strip()
            if text and text not in recommendations:
                recommendations.append(text)
        statuses.append(str(summary.get("status") or "empty"))
        sample_count += safe_int(summary.get("sample_count"))
        unmapped_count += safe_int(summary.get("unmapped_source_path_count"))
        file_summaries.append(summary)
    if not file_summaries:
        return {}
    draft_available = source == "preference" and bool(combined_field_map)
    return {
        "source_type": "localfile",
        "status": worst_retrieval_quality_status(statuses),
        "field_map": combined_field_map,
        "suggested_field_count": len(combined_field_map),
        "files": file_summaries,
        "sample_count": sample_count,
        "unmapped_source_path_count": unmapped_count,
        "recommendations": recommendations[:6],
        "draft_available": draft_available,
        "config_draft": {"field_map": combined_field_map} if draft_available else {},
        "message": "Local CSV/JSONL field_map suggestion can be saved with the local source paths.",
    }


def skipped_retrieval_readiness_entry(
    name: str,
    label: str,
    source: str,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "source": source,
        "configured": False,
        "available": False,
        "previewed": False,
        "status": "skipped",
        "message": "No source configuration found.",
        "summary": summary or {"configured": False, "label": label},
        "quality": {"status": "skipped", "recommendations": []},
        "sample_count": 0,
        "recommendations": [],
    }


def error_retrieval_readiness_entry(
    name: str,
    label: str,
    source: str,
    exc: BaseException,
    *,
    configured: bool,
    available: bool,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = retrieval_error_details(exc)
    recommendation = details.get("action") or details.get("error") or str(exc)
    return {
        "name": name,
        "label": label,
        "source": source,
        "configured": configured,
        "available": available,
        "previewed": False,
        "status": "error",
        "error_kind": details.get("error_kind") or "preview_error",
        "error": details.get("error") or str(exc),
        "message": recommendation,
        "summary": summary or {"configured": configured, "label": label},
        "quality": {"status": "error", "recommendations": [recommendation] if recommendation else []},
        "sample_count": 0,
        "recommendations": [recommendation] if recommendation else [],
    }


def preview_retrieval_readiness_entry(
    name: str,
    label: str,
    source: str,
    raw_config: Any,
    *,
    query: str,
    sample_size: int,
    status: dict[str, Any],
    summary_func,
    preview_func,
) -> dict[str, Any]:
    if not config_value_present(raw_config):
        return skipped_retrieval_readiness_entry(name, label, source)
    summary: dict[str, Any] | None = None
    configured = bool(status.get("configured"))
    available = bool(status.get("available"))
    try:
        summary = summary_func(raw_config)
        configured = bool(summary.get("configured"))
        preview = preview_func(raw_config, query=query, sample_size=sample_size)
        quality = retrieval_quality_summary_from_preview(name, preview)
        quality_status = str(quality.get("status") or "empty")
        field_map_suggestion = retrieval_readiness_field_map_suggestion(
            name,
            raw_config,
            source,
            query=query,
            sample_size=sample_size,
        )
        recommendations = list(quality.get("recommendations") or [])[:6]
        suggestion_status = str(field_map_suggestion.get("status") or "")
        if quality_status != "good" and field_map_suggestion:
            suggestion_count = safe_int(field_map_suggestion.get("suggested_field_count"))
            suggestion_label = f"Review {label} field_map suggestion"
            if suggestion_count:
                suggestion_label = f"{suggestion_label} ({suggestion_count} fields)"
            if suggestion_label not in recommendations:
                recommendations.append(suggestion_label)
            if suggestion_status == "error":
                for recommendation in field_map_suggestion.get("recommendations") or []:
                    text = str(recommendation or "").strip()
                    if text and text not in recommendations:
                        recommendations.append(text)
        return {
            "name": name,
            "label": str(preview.get("label") or summary.get("label") or label),
            "source": source,
            "configured": configured,
            "available": available,
            "previewed": True,
            "status": quality_status,
            "message": retrieval_readiness_entry_message(quality_status),
            "summary": summary,
            "quality": quality,
            "sample_count": retrieval_preview_sample_count(name, preview),
            "preview": preview,
            "field_map_suggestion": field_map_suggestion,
            "recommendations": recommendations[:6],
        }
    except Exception as exc:  # noqa: BLE001 - preflight must report every source independently
        return error_retrieval_readiness_entry(
            name,
            label,
            source,
            exc,
            configured=configured,
            available=available,
            summary=summary,
        )


def preview_local_retrieval_readiness_entry(
    config: dict[str, Any],
    source: str,
    *,
    sample_size: int,
    status: dict[str, Any],
) -> dict[str, Any]:
    paths = [str(path).strip() for path in config.get("paths") or [] if str(path).strip()]
    field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
    if not paths:
        return skipped_retrieval_readiness_entry("localfile", "Local CSV/JSONL", source)
    configured = bool(status.get("configured"))
    available = bool(status.get("available"))
    try:
        preview = preview_local_file_mappings(paths, sample_size=sample_size, field_map=field_map)
        quality = retrieval_quality_summary_from_preview("localfile", preview)
        quality_status = str(quality.get("status") or "empty")
        field_map_suggestion = local_file_field_map_suggestion_summary_for_readiness(preview, source)
        recommendations = list(quality.get("recommendations") or [])[:6]
        if quality_status != "good" and field_map_suggestion:
            suggestion_count = safe_int(field_map_suggestion.get("suggested_field_count"))
            suggestion_label = "Review Local CSV/JSONL field_map suggestion"
            if suggestion_count:
                suggestion_label = f"{suggestion_label} ({suggestion_count} fields)"
            if suggestion_label not in recommendations:
                recommendations.append(suggestion_label)
        return {
            "name": "localfile",
            "label": "Local CSV/JSONL",
            "source": source,
            "configured": configured,
            "available": available,
            "previewed": True,
            "status": quality_status,
            "message": retrieval_readiness_entry_message(quality_status),
            "summary": {
                "configured": True,
                "label": "Local CSV/JSONL",
                "paths": paths,
                "field_map": field_map,
                "file_count": preview.get("file_count", 0),
            },
            "quality": quality,
            "sample_count": retrieval_preview_sample_count("localfile", preview),
            "preview": preview,
            "field_map_suggestion": field_map_suggestion,
            "recommendations": recommendations[:6],
        }
    except Exception as exc:  # noqa: BLE001 - preflight must report every source independently
        return error_retrieval_readiness_entry(
            "localfile",
            "Local CSV/JSONL",
            source,
            exc,
            configured=configured,
            available=available,
        )


def retrieval_readiness_entry_message(status: str) -> str:
    return {
        "good": "Preview mapped into the library item format.",
        "warning": "Preview mapped with warnings; review field coverage.",
        "poor": "Preview has required field gaps.",
        "empty": "Configured source returned no sample rows for this query.",
        "error": "Preview failed; fix source configuration or access.",
        "skipped": "No source configuration found.",
    }.get(str(status or ""), "Review source preview.")


def retrieval_readiness_previews(
    library_id: str,
    *,
    query: str,
    sample_size: int,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    status_by_name = {str(source.get("name") or ""): source for source in sources}
    local_config, local_source = local_retrieval_config_for_library(library_id)
    http_raw_config, http_source = http_json_config_for_library(library_id)
    sqlite_raw_config, sqlite_source = sqlite_config_for_library(library_id)
    manifest_raw_config, manifest_source_value = manifest_config_for_library(library_id)
    return [
        preview_local_retrieval_readiness_entry(
            local_config,
            local_source,
            sample_size=sample_size,
            status=status_by_name.get("localfile", {}),
        ),
        preview_retrieval_readiness_entry(
            "httpjson",
            "HTTP JSON",
            http_source,
            http_raw_config,
            query=query,
            sample_size=sample_size,
            status=status_by_name.get("httpjson", {}),
            summary_func=http_json_config_summary,
            preview_func=preview_http_json_mappings,
        ),
        preview_retrieval_readiness_entry(
            "sqlite",
            "SQLite",
            sqlite_source,
            sqlite_raw_config,
            query=query,
            sample_size=sample_size,
            status=status_by_name.get("sqlite", {}),
            summary_func=sqlite_config_summary,
            preview_func=preview_sqlite_mappings,
        ),
        preview_retrieval_readiness_entry(
            "manifest",
            "Object Manifest",
            manifest_source_value,
            manifest_raw_config,
            query=query,
            sample_size=sample_size,
            status=status_by_name.get("manifest", {}),
            summary_func=manifest_config_summary,
            preview_func=preview_manifest_mappings,
        ),
    ]


def retrieval_readiness_report(
    sources: list[dict[str, Any]],
    previews: list[dict[str, Any]],
    *,
    query: str,
    sample_size: int,
    include_health: bool,
) -> dict[str, Any]:
    configured_internal = [entry for entry in previews if entry.get("configured") or entry.get("status") == "error"]
    previewed_internal = [entry for entry in previews if entry.get("previewed")]
    warning_entries = [entry for entry in previewed_internal if entry.get("status") in {"warning", "empty"}]
    blocking_entries = [entry for entry in previews if entry.get("status") in {"poor", "error"}]
    recommendations: list[str] = []
    if not configured_internal:
        recommendations.append("Configure at least one competition/internal source before batch validation.")
    for entry in previews:
        for recommendation in entry.get("recommendations") or []:
            text = str(recommendation or "").strip()
            if text and text not in recommendations:
                recommendations.append(text)
    if blocking_entries:
        readiness_status = "blocked"
        message = "Fix blocking source preview errors before relying on batch retrieval."
    elif warning_entries or not configured_internal:
        readiness_status = "warning"
        message = "Retrieval is usable, but readiness warnings need review."
    else:
        readiness_status = "ready"
        message = "Configured retrieval sources passed the preflight preview."
    summary = {
        "source_count": len(sources),
        "available_source_count": sum(1 for source in sources if source.get("available")),
        "configured_source_count": sum(1 for source in sources if source.get("configured")),
        "internal_source_count": len(previews),
        "configured_internal_count": len(configured_internal),
        "previewed_internal_count": len(previewed_internal),
        "sample_count": sum(safe_int(entry.get("sample_count")) for entry in previews),
        "warning_count": len(warning_entries),
        "blocking_count": len(blocking_entries),
        "error_count": sum(1 for entry in previews if entry.get("status") == "error"),
    }
    return {
        "generated_at": now_iso(),
        "status": readiness_status,
        "message": message,
        "query": query,
        "sample_size": sample_size,
        "include_health": include_health,
        "summary": summary,
        "sources": sources,
        "previews": previews,
        "recommendations": recommendations[:8],
    }


def retrieval_readiness_report_for_library(
    library_id: str,
    *,
    query: str,
    sample_size: int,
    include_health: bool,
) -> dict[str, Any]:
    sources = retrieval_source_statuses(
        registry=retrieval_provider_registry_for_library(library_id),
        include_health=include_health,
        health_query=query,
    )
    previews = retrieval_readiness_previews(
        library_id,
        query=query,
        sample_size=sample_size,
        sources=sources,
    )
    return retrieval_readiness_report(
        sources,
        previews,
        query=query,
        sample_size=sample_size,
        include_health=include_health,
    )


QUERY_PLAN_STOPWORDS = {
    "about",
    "after",
    "and",
    "article",
    "based",
    "benchmark",
    "benchmarks",
    "case",
    "data",
    "dataset",
    "datasets",
    "for",
    "from",
    "into",
    "metadata",
    "method",
    "methods",
    "model",
    "models",
    "paper",
    "report",
    "reports",
    "retrieval",
    "source",
    "study",
    "test",
    "tests",
    "the",
    "this",
    "using",
    "with",
}


def retrieval_query_plan_terms(value: Any) -> list[str]:
    text = str(value or "")
    terms: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_+-]{2,}", text):
        raw = match.group(0).strip("_-+")
        normalized = raw.casefold()
        if not raw or normalized in QUERY_PLAN_STOPWORDS or normalized.isdigit():
            continue
        if normalized not in terms:
            terms.append(normalized)
    return terms


def retrieval_query_plan_tag_terms(tags: Any) -> list[str]:
    values: list[Any] = tags if isinstance(tags, list) else []
    terms: list[str] = []
    for tag in values:
        if isinstance(tag, dict):
            text = tag.get("tag") or tag.get("name") or tag.get("label") or ""
        else:
            text = tag
        for term in retrieval_query_plan_terms(text):
            if term not in terms:
                terms.append(term)
    return terms


def retrieval_query_plan_creators(creators: Any) -> str:
    if not isinstance(creators, list):
        return ""
    names: list[str] = []
    for creator in creators[:3]:
        if not isinstance(creator, dict):
            continue
        first = str(creator.get("first_name") or creator.get("firstName") or "").strip()
        last = str(creator.get("last_name") or creator.get("lastName") or creator.get("name") or "").strip()
        name = " ".join(part for part in [first, last] if part)
        if name:
            names.append(name)
    return "; ".join(names)


def retrieval_query_plan_item_from_sample(sample: dict[str, Any]) -> dict[str, Any]:
    item = sample.get("item") if isinstance(sample.get("item"), dict) else {}
    return item if isinstance(item, dict) else {}


def retrieval_query_plan_samples(entry: dict[str, Any]) -> list[dict[str, Any]]:
    preview = entry.get("preview") if isinstance(entry.get("preview"), dict) else {}
    samples: list[dict[str, Any]] = []
    if str(entry.get("name") or "") == "localfile":
        for file_preview in preview.get("files") or []:
            if not isinstance(file_preview, dict):
                continue
            for sample in file_preview.get("samples") or []:
                if isinstance(sample, dict):
                    samples.append(sample)
        return samples
    for sample in preview.get("samples") or []:
        if isinstance(sample, dict):
            samples.append(sample)
    return samples


def retrieval_query_plan_query_from_item(
    item: dict[str, Any], seed_query: str, *, anchor_seed: bool = False
) -> tuple[str, str]:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    title = str(fields.get("title") or "").strip()
    abstract = str(fields.get("abstractNote") or fields.get("abstract") or "").strip()
    seed_terms = retrieval_query_plan_terms(seed_query)
    title_terms = retrieval_query_plan_terms(title)
    tag_terms = retrieval_query_plan_tag_terms(item.get("tags"))
    abstract_terms = retrieval_query_plan_terms(abstract)
    evidence_terms = [*title_terms, *tag_terms, *abstract_terms]
    if anchor_seed and seed_terms:
        seed_term_set = {term.casefold() for term in seed_terms}
        evidence_term_set = {term.casefold() for term in evidence_terms}
        if not (seed_term_set & evidence_term_set):
            return "", "unrelated_sample"
    terms: list[str] = []
    ordered_terms = (
        [*seed_terms[:3], *title_terms[:3], *tag_terms[:2], *abstract_terms[:2]]
        if anchor_seed
        else [*title_terms[:3], *tag_terms[:2], *abstract_terms[:2], *seed_terms[:2]]
    )
    for term in ordered_terms:
        if term not in terms:
            terms.append(term)
    query = " ".join(terms[:4] if anchor_seed else terms[:3]).strip()
    if anchor_seed:
        reason = "seed_matched_sample_title" if title_terms else "seed_matched_sample_metadata"
    else:
        reason = "sample_title" if title_terms else "sample_tags_or_abstract" if tag_terms or abstract_terms else "seed_query"
    return query, reason


def retrieval_query_plan_source_names(sources: list[str], preferred_sources: list[str]) -> list[str]:
    source_set = {str(source or "") for source in sources if str(source or "")}
    return [source for source in preferred_sources if source in source_set]


def retrieval_query_plan_related_specs(seed_query: str) -> list[tuple[str, str, list[str]]]:
    clean_seed = re.sub(r"\s+", " ", str(seed_query or "").strip())
    seed_lower = clean_seed.casefold()
    paper_sources = ["crossref", "arxiv", "pubmed", "semanticscholar", "openalex", "biorxiv", "medrxiv"]
    data_sources = ["datacite", "zenodo", "huggingface", "localfile", "sqlite", "manifest"]
    code_sources = ["github", "huggingface", "zenodo", "manifest"]
    benchmark_sources = ["huggingface", "github", "zenodo", "datacite"]

    if "speculative" in seed_lower and ("decoding" in seed_lower or "sampling" in seed_lower):
        return [
            ("speculative decoding draft model", "related_method_draft_model", paper_sources + code_sources),
            ("speculative sampling verification", "related_method_verification", paper_sources),
            ("LLM inference acceleration", "related_application_inference", paper_sources + code_sources),
            ("assisted generation draft verify", "related_method_assisted_generation", code_sources + benchmark_sources),
            ("Medusa EAGLE speculative decoding", "related_system_names", paper_sources + code_sources),
        ]
    if any(term in seed_lower for term in ["llm", "language model", "transformer", "decoding", "generation"]):
        return [
            (f"{clean_seed} inference", "related_inference_query", paper_sources + code_sources),
            (f"{clean_seed} benchmark", "related_benchmark_query", benchmark_sources),
            (f"{clean_seed} implementation", "related_implementation_query", code_sources),
            (f"{clean_seed} evaluation", "related_evaluation_query", paper_sources + data_sources),
        ]
    if any(term in seed_lower for term in ["graph", "gnn", "protein", "molecule", "catalyst"]):
        return [
            (f"{clean_seed} benchmark", "related_benchmark_query", benchmark_sources + data_sources),
            (f"{clean_seed} dataset", "related_dataset_query", data_sources),
            (f"{clean_seed} model", "related_model_query", paper_sources + code_sources),
            (f"{clean_seed} screening", "related_application_query", paper_sources + data_sources),
        ]
    return [
        (f"{clean_seed} survey", "related_survey_query", paper_sources),
        (f"{clean_seed} benchmark", "related_benchmark_query", benchmark_sources),
        (f"{clean_seed} implementation", "related_implementation_query", code_sources),
        (f"{clean_seed} dataset", "related_dataset_query", data_sources),
    ]


def retrieval_query_plan_related_terms(seed_query: str) -> set[str]:
    terms: set[str] = set()
    for query, _reason, _sources in retrieval_query_plan_related_specs(seed_query):
        terms.update(term for term in retrieval_query_plan_terms(query) if len(term) >= 3)
    return terms


def retrieval_query_plan_expansion_hints(seed_query: str) -> list[dict[str, Any]]:
    clean_seed = re.sub(r"\s+", " ", str(seed_query or "").strip())
    seed_lower = clean_seed.casefold()
    if not clean_seed:
        return []
    if "speculative" in seed_lower and ("decoding" in seed_lower or "sampling" in seed_lower):
        return [
            {
                "intent": "core_concept",
                "query": "speculative decoding",
                "terms": ["speculative decoding", "推测解码"],
                "sources": ["crossref", "arxiv", "semanticscholar", "github"],
            },
            {
                "intent": "method_alias",
                "query": "assisted generation draft model verification",
                "terms": ["assisted generation", "draft model", "verify", "草稿模型", "验证采样"],
                "sources": ["arxiv", "semanticscholar", "github", "huggingface"],
            },
            {
                "intent": "system_name",
                "query": "Medusa EAGLE speculative decoding",
                "terms": ["Medusa", "EAGLE", "speculative decoding"],
                "sources": ["arxiv", "semanticscholar", "github"],
            },
            {
                "intent": "benchmark",
                "query": "speculative decoding benchmark latency acceptance rate",
                "terms": ["benchmark", "latency", "acceptance rate", "throughput"],
                "sources": ["arxiv", "semanticscholar", "huggingface", "github"],
            },
            {
                "intent": "code_data",
                "query": "speculative decoding implementation inference acceleration",
                "terms": ["implementation", "inference acceleration", "LLM serving"],
                "sources": ["github", "huggingface", "zenodo"],
            },
        ]
    if any(term in seed_lower for term in ["llm", "language model", "transformer", "decoding", "generation"]):
        return [
            {
                "intent": "core_concept",
                "query": clean_seed,
                "terms": retrieval_query_plan_terms(clean_seed),
                "sources": ["crossref", "arxiv", "semanticscholar"],
            },
            {
                "intent": "method_alias",
                "query": f"{clean_seed} inference optimization",
                "terms": ["inference", "optimization", "serving", "latency"],
                "sources": ["arxiv", "semanticscholar", "github"],
            },
            {
                "intent": "benchmark",
                "query": f"{clean_seed} benchmark evaluation",
                "terms": ["benchmark", "evaluation", "leaderboard"],
                "sources": ["huggingface", "github", "zenodo", "datacite"],
            },
            {
                "intent": "code_data",
                "query": f"{clean_seed} implementation dataset",
                "terms": ["implementation", "dataset", "model"],
                "sources": ["github", "huggingface", "zenodo"],
            },
        ]
    if any(term in seed_lower for term in ["graph", "gnn", "protein", "molecule", "catalyst"]):
        return [
            {
                "intent": "core_concept",
                "query": clean_seed,
                "terms": retrieval_query_plan_terms(clean_seed),
                "sources": ["crossref", "arxiv", "semanticscholar"],
            },
            {
                "intent": "benchmark",
                "query": f"{clean_seed} benchmark",
                "terms": ["benchmark", "screening", "evaluation"],
                "sources": ["huggingface", "github", "zenodo", "datacite"],
            },
            {
                "intent": "data",
                "query": f"{clean_seed} dataset",
                "terms": ["dataset", "molecule", "materials", "protein"],
                "sources": ["datacite", "zenodo", "huggingface"],
            },
            {
                "intent": "model_code",
                "query": f"{clean_seed} model code",
                "terms": ["model", "code", "implementation"],
                "sources": ["github", "huggingface"],
            },
        ]
    return [
        {
            "intent": "core_concept",
            "query": clean_seed,
            "terms": retrieval_query_plan_terms(clean_seed),
            "sources": ["crossref", "arxiv", "semanticscholar"],
        },
        {
            "intent": "survey",
            "query": f"{clean_seed} survey",
            "terms": ["survey", "review", "overview"],
            "sources": ["crossref", "arxiv", "semanticscholar"],
        },
        {
            "intent": "benchmark",
            "query": f"{clean_seed} benchmark",
            "terms": ["benchmark", "evaluation"],
            "sources": ["huggingface", "github", "zenodo", "datacite"],
        },
        {
            "intent": "code_data",
            "query": f"{clean_seed} implementation dataset",
            "terms": ["implementation", "dataset", "code"],
            "sources": ["github", "huggingface", "zenodo"],
        },
    ]


def retrieval_query_plan_expansion_terms(seed_query: str) -> set[str]:
    terms: set[str] = set()
    for hint in retrieval_query_plan_expansion_hints(seed_query):
        if not isinstance(hint, dict):
            continue
        terms.update(term for term in retrieval_query_plan_terms(hint.get("query")) if len(term) >= 2)
        for value in hint.get("terms") or []:
            terms.update(term for term in retrieval_query_plan_terms(value) if len(term) >= 2)
    return terms


def retrieval_query_plan_seed_variants(seed_query: str, sources: list[str], limit: int) -> list[dict[str, Any]]:
    clean_seed = re.sub(r"\s+", " ", str(seed_query or "").strip())
    if not clean_seed:
        return []
    variant_specs = [(clean_seed, "seed_query", [])]
    variant_specs.extend(retrieval_query_plan_related_specs(clean_seed))
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query, reason, preferred_sources in variant_specs:
        key = retrieval_query_coverage_key(query)
        if not key or key in seen:
            continue
        matched_sources = retrieval_query_plan_source_names(sources, preferred_sources)
        variants.append(
            {
                "query": query,
                "reason": reason,
                "source_count": len(matched_sources),
                "sample_count": 0,
                "sources": matched_sources,
                "evidence": [],
                "priority": 100 if reason == "seed_query" else 90,
            }
        )
        seen.add(key)
        if len(variants) >= max(1, int(limit or 5)):
            break
    return variants


def retrieval_query_plan_ai_messages(
    seed_query: str,
    queries: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, str]]:
    task_queries: list[dict[str, Any]] = []
    for query_item in queries[:10]:
        if not isinstance(query_item, dict):
            continue
        evidence = [
            {
                "source": str(item.get("source") or ""),
                "title": str(item.get("title") or "")[:180],
                "identifier": str(item.get("identifier") or "")[:120],
                "creators": str(item.get("creators") or "")[:120],
            }
            for item in (query_item.get("evidence") or [])[:5]
            if isinstance(item, dict)
        ]
        task_queries.append(
            {
                "query": str(query_item.get("query") or ""),
                "reason": str(query_item.get("reason") or ""),
                "sources": [str(item) for item in query_item.get("sources") or []],
                "evidence": evidence,
            }
        )
    task = {
        "seed_query": seed_query,
        "core_seed_terms": retrieval_query_plan_terms(seed_query),
        "related_terms": sorted(retrieval_query_plan_related_terms(seed_query))[:30],
        "expansion_hints": retrieval_query_plan_expansion_hints(seed_query),
        "rule_queries": task_queries,
        "sources": [
            {
                "source": str(source.get("source") or ""),
                "label": str(source.get("label") or ""),
                "sample_count": safe_int(source.get("sample_count")),
            }
            for source in sources
            if isinstance(source, dict)
        ],
        "limit": limit,
        "response_schema": {
            "queries": [
                {
                    "query": "3 to 6 concise search terms; must preserve the seed_query topic",
                    "reason": "why this query helps one source category and intent",
                    "intent": "core_concept | method_alias | benchmark | data | code | bilingual",
                    "sources": ["optional exact source names from sources"],
                }
            ],
            "notes": ["optional short notes"],
        },
        "planning_rules": [
            "Return 3 to 5 high-signal queries.",
            "Use real neighboring concepts, aliases, methods, evaluation terms, or system names; do not only append generic words like paper, code, dataset.",
            "Every query should include a core_seed_terms token or a term from related_terms.",
            "Use expansion_hints to cover distinct intents: core concept, method/alias, benchmark/evaluation, code/model, and dataset/source discovery.",
            "For Chinese/English bilingual terms, include them only when they are direct translations or common aliases of the seed topic.",
            "Prefer one broad query, one method/alias query, one evaluation/benchmark query, one implementation query, and one data/model query when sources support them.",
            "Do not copy sample titles that are unrelated to seed_query.",
            "Do not add facts, identifiers, URLs, or source names that are not in the payload.",
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "You refine user search text into a small heterogeneous retrieval plan. "
                "Return only JSON. Do not invent credentials, URLs, source paths, paper facts, or IDs. "
                "Keep the user's seed_query as the anchor for every query. "
                "Use source samples only as weak evidence; ignore samples that are not about the seed_query. "
                "Prefer concept expansion over suffix-only variants such as '<seed> paper' or '<seed> code'."
            ),
        },
        {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
    ]


def retrieval_query_plan_ai_terms(seed_query: str, queries: list[dict[str, Any]]) -> set[str]:
    values: list[str] = [seed_query]
    for query_item in queries:
        if not isinstance(query_item, dict):
            continue
        values.append(str(query_item.get("query") or ""))
        for evidence in query_item.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            values.extend(
                [
                    str(evidence.get("title") or ""),
                    str(evidence.get("identifier") or ""),
                    str(evidence.get("creators") or ""),
                ]
            )
    terms: set[str] = set()
    for value in values:
        terms.update(term for term in retrieval_query_plan_terms(value) if len(term) >= 3)
    return terms


def clean_retrieval_query_plan_ai_query(value: Any) -> str:
    query = re.sub(r"\s+", " ", str(value or "").strip())
    if len(query) < 2 or len(query) > 120:
        return ""
    if re.search(r"https?://|authorization|bearer|api[_\s-]*key|secret|password", query, re.I):
        return ""
    return query


def apply_retrieval_query_plan_ai_enhancement(
    seed_query: str,
    queries: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    limit: int,
    use_ai: bool = False,
    ai_post_json: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    model = retrieval_model_status()
    enhancement: dict[str, Any] = {
        "requested": bool(use_ai),
        "configured": bool(model.get("configured")),
        "provider": model.get("provider"),
        "base_url": model.get("base_url"),
        "model": model.get("model"),
        "status": "skipped",
        "message": "",
        "suggested_query_count": 0,
        "accepted_query_count": 0,
        "applied_query_count": 0,
        "rejected": [],
    }
    if not use_ai:
        enhancement["message"] = "Set use_ai=true to request AI query-plan enhancement."
        return queries, enhancement
    if not enhancement["configured"]:
        enhancement["status"] = "not_configured"
        enhancement["message"] = f"Set {AI_PIXEL_API_KEY_ENV} before using AI query-plan enhancement."
        return queries, enhancement
    if not queries:
        enhancement["status"] = "empty"
        enhancement["message"] = "AI query-plan enhancement skipped because no rule queries were available."
        return queries, enhancement

    try:
        if callable(ai_post_json):
            model_response = ai_pixel_chat_json(
                retrieval_query_plan_ai_messages(seed_query, queries, sources, limit=limit),
                post_json=ai_post_json,
            )
        else:
            model_response = ai_pixel_chat_json(
                retrieval_query_plan_ai_messages(seed_query, queries, sources, limit=limit)
            )
    except Exception as exc:  # noqa: BLE001 - query planning must keep deterministic fallbacks usable.
        enhancement["status"] = "error"
        enhancement["message"] = str(exc or exc.__class__.__name__)
        return queries, enhancement

    raw_queries = model_response.get("queries") if isinstance(model_response, dict) else []
    if isinstance(raw_queries, str):
        raw_queries = [{"query": item} for item in raw_queries.splitlines()]
    if not isinstance(raw_queries, list):
        raw_queries = []
    enhancement["suggested_query_count"] = len(raw_queries)
    allowed_terms = retrieval_query_plan_ai_terms(seed_query, queries)
    allowed_terms.update(retrieval_query_plan_related_terms(seed_query))
    allowed_terms.update(retrieval_query_plan_expansion_terms(seed_query))
    seed_terms = {term.casefold() for term in retrieval_query_plan_terms(seed_query)}
    related_terms = {term.casefold() for term in retrieval_query_plan_related_terms(seed_query)}
    related_terms.update(term.casefold() for term in retrieval_query_plan_expansion_terms(seed_query))
    source_names = {str(source.get("source") or "") for source in sources if isinstance(source, dict)}
    seen = {retrieval_query_coverage_key(item.get("query")) for item in queries if isinstance(item, dict)}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for raw_item in raw_queries:
        raw_query = raw_item.get("query") if isinstance(raw_item, dict) else raw_item
        query = clean_retrieval_query_plan_ai_query(raw_query)
        if not query:
            rejected.append({"query": str(raw_query or "")[:120], "reason": "invalid query text"})
            continue
        key = retrieval_query_coverage_key(query)
        if key in seen:
            rejected.append({"query": query, "reason": "duplicate query"})
            continue
        query_terms = set(retrieval_query_plan_terms(query))
        if allowed_terms and not (query_terms & allowed_terms):
            rejected.append({"query": query, "reason": "query terms not supported by seed or evidence"})
            continue
        query_terms_casefold = {term.casefold() for term in query_terms}
        if seed_terms and not (query_terms_casefold & (seed_terms | related_terms)):
            rejected.append({"query": query, "reason": "query does not preserve seed topic"})
            continue
        raw_sources = raw_item.get("sources") if isinstance(raw_item, dict) else []
        item_sources = [str(source) for source in raw_sources or [] if str(source) in source_names]
        accepted.append(
            {
                "query": query,
                "reason": "ai_suggestion",
                "source_count": len(item_sources),
                "sample_count": 0,
                "sources": item_sources,
                "evidence": [],
                "ai": True,
                "intent": str(raw_item.get("intent") or "")[:80] if isinstance(raw_item, dict) else "",
                "model_reason": str(raw_item.get("reason") or "")[:240] if isinstance(raw_item, dict) else "",
            }
        )
        seen.add(key)
        if len(accepted) >= limit:
            break

    enhancement["accepted_query_count"] = len(accepted)
    enhancement["rejected"] = rejected[:20]
    notes = model_response.get("notes") if isinstance(model_response, dict) else []
    if isinstance(notes, list):
        enhancement["notes"] = [str(note)[:240] for note in notes[:5] if str(note or "").strip()]
    elif str(notes or "").strip():
        enhancement["notes"] = [str(notes)[:240]]
    if not accepted:
        enhancement["status"] = "empty"
        enhancement["message"] = "AI Pixel returned no valid query-plan suggestions."
        return queries, enhancement

    merged = accepted[:]
    merged_keys = {retrieval_query_coverage_key(item.get("query")) for item in merged}
    for query_item in queries:
        key = retrieval_query_coverage_key(query_item.get("query") if isinstance(query_item, dict) else "")
        if key and key not in merged_keys:
            merged.append(query_item)
            merged_keys.add(key)
        if len(merged) >= limit:
            break
    enhancement["status"] = "applied"
    enhancement["applied_query_count"] = len([item for item in merged if isinstance(item, dict) and item.get("ai")])
    enhancement["message"] = f"AI Pixel applied {enhancement['applied_query_count']} query-plan suggestions."
    return merged[:limit], enhancement


def retrieval_query_plan_for_library(
    library_id: str,
    *,
    seed_query: str = "robot",
    sample_size: int = 5,
    limit: int = 5,
    use_ai: bool = False,
    selected_sources: Any = None,
    ai_post_json: Any = None,
) -> dict[str, Any]:
    clean_seed = str(seed_query or "").strip() or "robot"
    sample_limit = max(1, min(int(sample_size or 5), 5))
    query_limit = max(1, min(int(limit or 5), 10))
    sources = retrieval_source_statuses(
        registry=retrieval_provider_registry_for_library(library_id),
        include_health=False,
        health_query=clean_seed,
    )
    selected_source_names = retrieval_requested_source_names(selected_sources)
    if selected_source_names:
        sources = [source for source in sources if str(source.get("name") or "") in selected_source_names]
    previews = retrieval_readiness_previews(
        library_id,
        query=clean_seed,
        sample_size=sample_limit,
        sources=sources,
    )
    by_query: dict[str, dict[str, Any]] = {}
    source_summaries: list[dict[str, Any]] = []
    for entry in previews:
        name = str(entry.get("name") or "")
        samples = retrieval_query_plan_samples(entry)
        source_summaries.append(
            {
                "source": name,
                "label": str(entry.get("label") or name),
                "status": str(entry.get("status") or ""),
                "configured": bool(entry.get("configured")),
                "previewed": bool(entry.get("previewed")),
                "sample_count": len(samples),
            }
        )
        for index, sample in enumerate(samples, start=1):
            item = retrieval_query_plan_item_from_sample(sample)
            if not item:
                continue
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            identifiers = item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {}
            query, reason = retrieval_query_plan_query_from_item(item, clean_seed, anchor_seed=True)
            if not query:
                continue
            key = query.casefold()
            record = by_query.setdefault(
                key,
                {
                    "query": query,
                    "reason": reason,
                    "source_count": 0,
                    "sample_count": 0,
                    "sources": [],
                    "evidence": [],
                    "priority": 50,
                },
            )
            record["sample_count"] = safe_int(record.get("sample_count")) + 1
            if name and name not in record["sources"]:
                record["sources"].append(name)
                record["source_count"] = len(record["sources"])
            record["evidence"].append(
                {
                    "source": name,
                    "sample_index": index,
                    "title": str(fields.get("title") or "")[:180],
                    "identifier": next((str(value) for value in identifiers.values() if value), ""),
                    "creators": retrieval_query_plan_creators(item.get("creators")),
                }
            )
    for variant in retrieval_query_plan_seed_variants(
        clean_seed,
        [str(source.get("source") or "") for source in source_summaries],
        query_limit,
    ):
        key = str(variant.get("query") or "").casefold()
        if not key:
            continue
        existing = by_query.get(key)
        if existing:
            existing["priority"] = max(safe_int(existing.get("priority")), safe_int(variant.get("priority")))
            existing["reason"] = existing.get("reason") or variant.get("reason")
            for source in variant.get("sources") or []:
                if source and source not in existing["sources"]:
                    existing["sources"].append(source)
            existing["source_count"] = len(existing.get("sources") or [])
        else:
            by_query[key] = variant
    queries = sorted(
        by_query.values(),
        key=lambda item: (
            -safe_int(item.get("priority")),
            -safe_int(item.get("source_count")),
            -safe_int(item.get("sample_count")),
            str(item.get("query") or ""),
        ),
    )[:query_limit]
    fallback = False
    if not queries:
        fallback = True
        queries = [
            {
                "query": clean_seed,
                "reason": "seed_query",
                "source_count": 0,
                "sample_count": 0,
                "sources": [],
                "evidence": [],
            }
        ]
    with use_ai_pixel_config(api_config_model_for_library(library_id)):
        queries, ai_enhancement = apply_retrieval_query_plan_ai_enhancement(
            clean_seed,
            queries,
            source_summaries,
            limit=query_limit,
            use_ai=use_ai,
            ai_post_json=ai_post_json,
        )
    if fallback:
        status = "empty"
        message = "No configured source samples produced query drafts; using the seed query as a fallback."
    elif len(queries) >= min(3, query_limit):
        status = "ready"
        message = "Query plan drafted from configured source preview samples."
    else:
        status = "low_sample"
        message = "Query plan drafted, but fewer than 3 distinct query samples were available."
    if ai_enhancement.get("status") == "applied":
        message += " AI Pixel suggestions were applied."
    recommendations: list[str] = []
    if fallback:
        recommendations.append("Configure or preview at least one internal source before batch validation.")
    elif status == "low_sample":
        recommendations.append("Add more representative source records or raise sample_size before handoff.")
    if ai_enhancement.get("requested") and ai_enhancement.get("status") == "not_configured":
        recommendations.append(f"Set {AI_PIXEL_API_KEY_ENV} before using AI query-plan enhancement.")
    elif ai_enhancement.get("status") == "applied":
        recommendations.append("Review AI-enhanced queries before starting the validation batch.")
    recommendations.append("Review the query text, then run it as a 3-5 query batch for ONB validation.")
    return {
        "generated_at": now_iso(),
        "status": status,
        "message": message,
        "seed_query": clean_seed,
        "sample_size": sample_limit,
        "limit": query_limit,
        "query_count": len(queries),
        "queries": queries,
        "query_text": "\n".join(str(item.get("query") or "") for item in queries if item.get("query")),
        "sources": source_summaries,
        "ai_enhancement": ai_enhancement,
        "recommendations": recommendations,
    }


def retrieval_query_plan_report_rows(plan: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = [
        {
            "section": "overview",
            "query": str(plan.get("seed_query") or ""),
            "status": str(plan.get("status") or ""),
            "reason": str(plan.get("message") or ""),
            "source": "",
            "source_count": str(len(plan.get("sources") or [])),
            "sample_count": str(plan.get("sample_size") or ""),
            "title": "",
            "identifier": "",
            "creators": "",
        }
    ]
    ai = plan.get("ai_enhancement") if isinstance(plan.get("ai_enhancement"), dict) else {}
    if ai:
        rows.append(
            {
                "section": "ai_enhancement",
                "query": "",
                "status": str(ai.get("status") or ""),
                "reason": str(ai.get("message") or ""),
                "source": str(ai.get("provider") or ""),
                "source_count": str(ai.get("accepted_query_count") or 0),
                "sample_count": str(ai.get("suggested_query_count") or 0),
                "title": str(ai.get("model") or ""),
                "identifier": str(ai.get("base_url") or ""),
                "creators": "",
            }
        )
        for rejected in ai.get("rejected") or []:
            if not isinstance(rejected, dict):
                continue
            rows.append(
                {
                    "section": "ai_rejected_query",
                    "query": str(rejected.get("query") or ""),
                    "status": "rejected",
                    "reason": str(rejected.get("reason") or ""),
                    "source": str(ai.get("provider") or ""),
                    "source_count": "",
                    "sample_count": "",
                    "title": "",
                    "identifier": "",
                    "creators": "",
                }
            )
    for query_item in plan.get("queries") or []:
        if not isinstance(query_item, dict):
            continue
        query_text = str(query_item.get("query") or "")
        rows.append(
            {
                "section": "query",
                "query": query_text,
                "status": str(plan.get("status") or ""),
                "reason": str(query_item.get("reason") or ""),
                "source": ", ".join(str(item) for item in query_item.get("sources") or []),
                "source_count": str(query_item.get("source_count") or 0),
                "sample_count": str(query_item.get("sample_count") or 0),
                "title": "",
                "identifier": "",
                "creators": "",
            }
        )
        for evidence in query_item.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            rows.append(
                {
                    "section": "evidence",
                    "query": query_text,
                    "status": "",
                    "reason": str(query_item.get("reason") or ""),
                    "source": str(evidence.get("source") or ""),
                    "source_count": "",
                    "sample_count": str(evidence.get("sample_index") or ""),
                    "title": str(evidence.get("title") or ""),
                    "identifier": str(evidence.get("identifier") or ""),
                    "creators": str(evidence.get("creators") or ""),
                }
            )
    for source in plan.get("sources") or []:
        if not isinstance(source, dict):
            continue
        rows.append(
            {
                "section": "source",
                "query": "",
                "status": str(source.get("status") or ""),
                "reason": (
                    f"configured={str(bool(source.get('configured'))).lower()}; "
                    f"previewed={str(bool(source.get('previewed'))).lower()}"
                ),
                "source": str(source.get("source") or ""),
                "source_count": "",
                "sample_count": str(source.get("sample_count") or 0),
                "title": str(source.get("label") or ""),
                "identifier": "",
                "creators": "",
            }
        )
    for recommendation in plan.get("recommendations") or []:
        text = str(recommendation or "").strip()
        if not text:
            continue
        rows.append(
            {
                "section": "recommendation",
                "query": "",
                "status": "",
                "reason": text,
                "source": "",
                "source_count": "",
                "sample_count": "",
                "title": "",
                "identifier": "",
                "creators": "",
            }
        )
    return rows


def retrieval_query_plan_markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("\n", " ").replace("|", "\\|")


def candidate_author_names(candidate: dict[str, Any]) -> list[str]:
    creators: Any = candidate.get("creators")
    item = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
    if not isinstance(creators, list):
        creators = item.get("creators") if isinstance(item.get("creators"), list) else []
    names: list[str] = []
    for creator in creators[:8]:
        if not isinstance(creator, dict):
            continue
        name = str(
            creator.get("name")
            or " ".join(str(creator.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
        ).strip()
        if name:
            names.append(name)
    return names


def candidate_field(candidate: dict[str, Any], key: str) -> str:
    if str(candidate.get(key) or "").strip():
        return str(candidate.get(key) or "").strip()
    item = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    return str(fields.get(key) or "").strip()


def candidate_metadata_for_ai(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
    sources = candidate.get("sources") if isinstance(candidate.get("sources"), list) else []
    return {
        "candidate_id": f"candidate-{index}",
        "title": candidate_field(candidate, "title"),
        "authors": candidate_author_names(candidate),
        "year": str(candidate.get("year") or "").strip(),
        "abstract": str(candidate.get("abstract") or candidate_field(candidate, "abstractNote") or "").strip()[
            :AI_CANDIDATE_METADATA_ABSTRACT_LIMIT
        ],
        "source": str(candidate.get("source") or "").strip(),
        "sources": [str(source) for source in sources[:8] if str(source or "").strip()],
        "source_count": safe_int(candidate.get("source_count")) or len(sources) or 1,
        "multi_source": bool(candidate.get("multi_source") or len(sources) > 1),
        "doi": str(identifiers.get("doi") or "").strip(),
        "pmid": str(identifiers.get("pmid") or "").strip(),
        "arxiv": str(identifiers.get("arxiv") or "").strip(),
        "isbn": str(identifiers.get("isbn") or "").strip(),
        "url": str(candidate.get("landing_url") or candidate_field(candidate, "url") or "").strip(),
        "item_type": str(candidate.get("item_type") or "").strip(),
    }


def retrieval_candidate_ai_messages(query: str, metadata: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 Zotero 风格科研文库的检索候选评审助手。"
                "只能依据提供的元数据判断，不得编造标识符、作者、摘要、来源或结论。"
                "必须只返回严格 JSON，顶层包含 evaluations 数组，不能输出额外文字。"
                "每个 evaluation 必须包含 candidate_id、decision（recommend|review|reject）、"
                "topic_relevance_score、metadata_quality_score、source_evidence_score、import_risk_score、"
                "final_confidence_score、risk_level（low|medium|high）、reason、missing_fields。"
                "所有分数为 0 到 100 的整数。评分准则："
                "topic_relevance_score 衡量 query 与标题/摘要/资料类型的语义匹配；"
                "metadata_quality_score 衡量标题、作者、年份、摘要、标识符、URL 是否足够支撑 Zotero 入库；"
                "source_evidence_score 衡量 DOI/PMID/arXiv/ISBN/URL、来源名称、多源命中的可追溯性和证据强度；"
                "import_risk_score 衡量重复、噪声、缺字段、来源不确定性等入库风险，分数越高风险越大；"
                "final_confidence_score 是综合推荐置信度，不要求按固定公式计算。"
                "统一使用这个尺度：90-100 非常强；75-89 较强；55-74 需要复核；30-54 较弱；0-29 无关或不安全。"
                "如果缺标题，或标识符和 URL 都缺失，final_confidence_score 不应超过 60。"
                "如果标题/摘要明显偏离主题，即使元数据完整也要降低 final_confidence_score。"
                "证据不足或不确定性明显时优先给 review。"
                "reason 必须使用简洁中文，说明为什么推荐、复核或不建议，不超过 80 个汉字。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "query": query,
                    "metadata_only": True,
                    "score_framework": AI_CANDIDATE_SCORE_FRAMEWORK,
                    "score_scale": "0-100; higher is better except import_risk_score",
                    "candidates": metadata,
                },
                ensure_ascii=False,
            ),
        },
    ]


def chunked_ai_candidate_metadata(metadata: list[dict[str, Any]], size: int = AI_CANDIDATE_EVALUATION_BATCH_SIZE) -> list[list[dict[str, Any]]]:
    chunk_size = max(1, int(size or AI_CANDIDATE_EVALUATION_BATCH_SIZE))
    return [metadata[index : index + chunk_size] for index in range(0, len(metadata), chunk_size)]


def clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(1.0, number)), 3)


def clamp_percent_score(value: Any, default: float = 0.0) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if 0 <= number <= 1:
        number *= 100
    return int(round(max(0.0, min(100.0, number))))


def candidate_missing_fields(candidate: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
    creators = candidate_author_names(candidate)
    if not candidate_field(candidate, "title"):
        missing.append("title")
    if not creators and str(candidate.get("item_type") or "") not in {"computerProgram", "dataset"}:
        missing.append("authors")
    if not str(candidate.get("abstract") or candidate_field(candidate, "abstractNote") or "").strip():
        missing.append("abstract")
    if not any(str(identifiers.get(key) or "").strip() for key in ("doi", "pmid", "arxiv", "isbn")) and not str(candidate.get("landing_url") or candidate_field(candidate, "url") or "").strip():
        missing.append("identifier_or_url")
    return missing


def strict_ai_auto_select(
    decision: str,
    final_confidence: int,
    import_risk: int,
    risk: str,
    missing_fields: list[str],
) -> bool:
    critical_missing = {"title", "identifier_or_url"}
    return (
        decision == "recommend"
        and final_confidence >= 75
        and import_risk <= 40
        and risk != "high"
        and not (critical_missing & set(missing_fields or []))
    )


def deterministic_source_evidence_score(candidate: dict[str, Any], identifiers: dict[str, Any]) -> int:
    score = 25
    if any(identifiers.get(key) for key in ("doi", "pmid", "arxiv", "isbn")):
        score += 35
    if candidate.get("landing_url") or candidate_field(candidate, "url"):
        score += 20
    if candidate.get("multi_source"):
        score += 20
    elif candidate.get("source") or candidate.get("sources"):
        score += 10
    return clamp_percent_score(score)


def deterministic_candidate_evaluation(candidate: dict[str, Any], query: str, *, status: str, reason: str = "") -> dict[str, Any]:
    title = candidate_field(candidate, "title")
    abstract = str(candidate.get("abstract") or candidate_field(candidate, "abstractNote") or "").strip()
    identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
    missing = candidate_missing_fields(candidate)
    query_terms = {part.casefold() for part in re.findall(r"[A-Za-z0-9_\-]{3,}", query)}
    haystack = f"{title} {abstract}".casefold()
    matched_terms = sum(1 for term in query_terms if term in haystack)
    relevance = 0.35
    if query_terms:
        relevance += min(0.35, matched_terms / max(1, len(query_terms)) * 0.35)
    if candidate.get("multi_source"):
        relevance += 0.12
    if any(identifiers.get(key) for key in ("doi", "pmid", "arxiv", "isbn")):
        relevance += 0.1
    if abstract:
        relevance += 0.08
    quality = 0.35
    if title:
        quality += 0.18
    if candidate_author_names(candidate):
        quality += 0.12
    if abstract:
        quality += 0.14
    if identifiers:
        quality += 0.14
    if candidate.get("landing_url") or candidate_field(candidate, "url"):
        quality += 0.07
    risk = "low"
    if candidate.get("existing_matches") or candidate.get("duplicate_hint"):
        risk = "high"
    elif missing:
        risk = "medium"
    relevance = clamp_score(relevance)
    quality = clamp_score(quality)
    decision = "review"
    if not title:
        decision = "reject"
    elif relevance >= 0.76 and quality >= 0.68 and risk != "high":
        decision = "recommend"
    topic_score = clamp_percent_score(relevance)
    metadata_score = clamp_percent_score(quality)
    source_score = deterministic_source_evidence_score(candidate, identifiers)
    import_risk_score = 72 if risk == "high" else (42 if risk == "medium" else 18)
    final_confidence = clamp_percent_score((topic_score + metadata_score + source_score + (100 - import_risk_score)) / 4)
    return {
        "status": status,
        "score_source": "deterministic_rules",
        "score_framework": DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK,
        "decision": decision,
        "topic_relevance_score": topic_score,
        "metadata_quality_score": metadata_score,
        "source_evidence_score": source_score,
        "import_risk_score": import_risk_score,
        "final_confidence_score": final_confidence,
        "relevance_score": relevance,
        "quality_score": quality,
        "risk_level": risk,
        "reason": reason or ("元数据完整且与检索主题较匹配，可作为初步推荐。" if decision == "recommend" else "当前元数据仍需人工复核后再入库。"),
        "missing_fields": missing,
        "auto_select": False,
    }


def normalized_ai_evaluation(raw: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    decision = str(raw.get("decision") or "").strip().lower()
    if decision not in {"recommend", "review", "reject"}:
        decision = "review"
    topic_relevance = clamp_percent_score(raw.get("topic_relevance_score", raw.get("relevance_score")), 0.0)
    metadata_quality = clamp_percent_score(raw.get("metadata_quality_score", raw.get("quality_score")), 0.0)
    source_evidence = clamp_percent_score(raw.get("source_evidence_score"), 0.0)
    import_risk = clamp_percent_score(raw.get("import_risk_score"), 50.0)
    final_confidence = clamp_percent_score(raw.get("final_confidence_score"), topic_relevance)
    risk = str(raw.get("risk_level") or "").strip().lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    missing = raw.get("missing_fields") if isinstance(raw.get("missing_fields"), list) else candidate_missing_fields(candidate)
    missing_fields = [str(field).strip() for field in missing if str(field or "").strip()][:8]
    return {
        "status": "evaluated",
        "score_source": "ai_model",
        "score_framework": AI_CANDIDATE_SCORE_FRAMEWORK,
        "decision": decision,
        "topic_relevance_score": topic_relevance,
        "metadata_quality_score": metadata_quality,
        "source_evidence_score": source_evidence,
        "import_risk_score": import_risk,
        "final_confidence_score": final_confidence,
        "relevance_score": round(topic_relevance / 100, 3),
        "quality_score": round(metadata_quality / 100, 3),
        "risk_level": risk,
        "reason": str(raw.get("reason") or "").strip()[:360] or "AI 已根据候选元数据完成评分。",
        "missing_fields": missing_fields,
        "auto_select": strict_ai_auto_select(decision, final_confidence, import_risk, risk, missing_fields),
    }


def ai_evaluation_error_message(exc: Exception) -> str:
    message = str(exc or exc.__class__.__name__).strip()
    return message[:240] or exc.__class__.__name__


def apply_ai_evaluations_to_candidates(
    candidates: list[dict[str, Any]],
    raw_evaluations: Any,
    id_map: dict[str, dict[str, Any]],
    *,
    fallback_candidates: list[dict[str, Any]] | None = None,
    fallback_reason: str = "AI 未返回该候选的有效评分，已使用规则兜底。",
) -> dict[str, int]:
    evaluations = raw_evaluations if isinstance(raw_evaluations, list) else []
    accepted = 0
    rejected = 0
    for raw in evaluations:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        candidate_id = str(raw.get("candidate_id") or "").strip()
        candidate = id_map.get(candidate_id)
        if not candidate:
            rejected += 1
            continue
        candidate["ai_evaluation"] = normalized_ai_evaluation(raw, candidate)
        accepted += 1
    targets = fallback_candidates if fallback_candidates is not None else candidates
    for candidate in targets:
        if isinstance(candidate.get("ai_evaluation"), dict):
            continue
        candidate["ai_evaluation"] = deterministic_candidate_evaluation(candidate, "", status="fallback", reason=fallback_reason)
    return {"accepted": accepted, "rejected": rejected}


def ai_evaluation_decision_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"recommend": 0, "review": 0, "reject": 0}
    for candidate in candidates:
        evaluation = candidate.get("ai_evaluation") if isinstance(candidate.get("ai_evaluation"), dict) else {}
        decision = str(evaluation.get("decision") or "review")
        if decision in counts:
            counts[decision] += 1
    return counts


def sort_candidates_by_ai_evaluation(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decision_weight = {"recommend": 0, "review": 1, "reject": 2}

    def key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        evaluation = candidate.get("ai_evaluation") if isinstance(candidate.get("ai_evaluation"), dict) else {}
        return (
            0 if evaluation.get("auto_select") else 1,
            decision_weight.get(str(evaluation.get("decision") or "review"), 1),
            -float(evaluation.get("final_confidence_score") or 0),
            -float(evaluation.get("topic_relevance_score") or 0),
            -float(evaluation.get("metadata_quality_score") or 0),
            int(candidate.get("rank") or 9999),
        )

    ordered = sorted(candidates, key=key)
    for index, candidate in enumerate(ordered, start=1):
        candidate["rank"] = index
    return ordered


def retrieval_background_job_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def retrieval_background_inline_enabled(name: str) -> bool:
    names = {
        "WEB_LIBRARY_RETRIEVAL_BACKGROUND_INLINE",
        f"WEB_LIBRARY_RETRIEVAL_{name.upper()}_INLINE",
    }
    return any(os.environ.get(env_name, "").strip().lower() in {"1", "true", "yes"} for env_name in names)


def retrieval_job_snapshot(job: dict[str, Any] | None) -> dict[str, Any] | None:
    return copy.deepcopy(job) if isinstance(job, dict) else None


def latest_retrieval_background_job(store: dict[str, dict[str, Any]], library_id: str) -> dict[str, Any] | None:
    jobs = [job for job in store.values() if job.get("library_id") == library_id]
    jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    return jobs[0] if jobs else None


def trim_retrieval_background_jobs(store: dict[str, dict[str, Any]], library_id: str) -> None:
    jobs = [job for job in store.values() if job.get("library_id") == library_id]
    jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    for job in jobs[RETRIEVAL_BACKGROUND_JOB_HISTORY_LIMIT:]:
        status = str(job.get("status") or "")
        if status not in {"queued", "running", "canceling"}:
            store.pop(str(job.get("job_id") or ""), None)


def retrieval_candidate_rule_confidence_value(candidate: dict[str, Any]) -> float:
    def normalized(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        return number / 100 if number > 1 else number

    confidence = normalized(candidate.get("confidence"))
    if confidence is not None:
        return confidence
    evaluation = candidate.get("ai_evaluation") if isinstance(candidate.get("ai_evaluation"), dict) else {}
    final_confidence = normalized(evaluation.get("final_confidence_score"))
    if final_confidence is not None:
        return final_confidence
    sources = candidate.get("sources") if isinstance(candidate.get("sources"), list) else []
    source_count = safe_int(candidate.get("source_count")) or len(sources)
    return min(0.95, 0.45 + source_count * 0.08)


def retrieval_candidate_job_key(candidate: dict[str, Any], index: int) -> str:
    for key in (candidate.get("client_key"), candidate.get("candidate_id"), candidate.get("external_id")):
        clean = str(key or "").strip()
        if clean:
            return clean
    title = candidate_field(candidate, "title") or candidate.get("title") or "candidate"
    return f"ai-job-{index}-{hashlib.sha1(str(title).encode('utf-8')).hexdigest()[:10]}"


def retrieval_candidate_has_ai_model_evaluation(candidate: dict[str, Any]) -> bool:
    evaluation = candidate.get("ai_evaluation") if isinstance(candidate.get("ai_evaluation"), dict) else {}
    return evaluation.get("score_source") == "ai_model" or evaluation.get("status") == "evaluated"


def retrieval_ai_scoring_job_summary(job: dict[str, Any]) -> dict[str, Any]:
    candidates = job.get("candidates") if isinstance(job.get("candidates"), list) else []
    model = job.get("model_status") if isinstance(job.get("model_status"), dict) else {}
    ai_evaluated = [candidate for candidate in candidates if retrieval_candidate_has_ai_model_evaluation(candidate)]
    processed = safe_int(job.get("completed_count"))
    failed = safe_int(job.get("failed_count"))
    total = safe_int(job.get("total_count")) or len(candidates)
    status = str(job.get("status") or "queued")
    if status in {"queued", "running", "canceling"}:
        summary_status = "evaluating"
    elif status == "completed" and len(ai_evaluated) >= total and failed == 0:
        summary_status = "evaluated"
    elif status in {"canceled", "partial", "completed"} and ai_evaluated:
        summary_status = "partial"
    elif status == "failed":
        summary_status = "error"
    else:
        summary_status = status or "skipped"
    score_source = "deterministic_rules"
    score_framework = DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK
    if ai_evaluated and len(ai_evaluated) >= total and failed == 0 and status == "completed":
        score_source = "ai_model"
        score_framework = AI_CANDIDATE_SCORE_FRAMEWORK
    elif ai_evaluated:
        score_source = "mixed_ai_rules"
        score_framework = AI_CANDIDATE_SCORE_FRAMEWORK
    return {
        "requested": True,
        "configured": bool(model.get("configured")),
        "provider": model.get("provider"),
        "model": model.get("model"),
        "score_source": score_source,
        "score_framework": score_framework,
        "status": summary_status,
        "candidate_count": total,
        "ai_evaluated_candidate_count": len(ai_evaluated),
        "skipped_candidate_count": max(0, total - len(ai_evaluated)),
        "failed_batch_count": failed,
        "processed_candidate_count": processed,
        "auto_selected_count": sum(1 for candidate in candidates if candidate.get("ai_evaluation", {}).get("auto_select")),
        "decision_counts": ai_evaluation_decision_counts(ai_evaluated),
        "error": str(job.get("error") or ""),
    }


def evaluate_retrieval_candidates_with_ai(
    library_id: str,
    query: str,
    candidates: list[dict[str, Any]],
    *,
    use_ai_evaluation: bool = True,
    ai_post_json: Any = None,
) -> dict[str, Any]:
    with use_ai_pixel_config(api_config_model_for_library(library_id)):
        model = retrieval_model_status()
        summary: dict[str, Any] = {
            "requested": bool(use_ai_evaluation),
            "configured": bool(model.get("configured")),
            "provider": model.get("provider"),
            "model": model.get("model"),
            "score_source": "ai_model" if model.get("configured") else "deterministic_rules",
            "score_framework": AI_CANDIDATE_SCORE_FRAMEWORK if model.get("configured") else DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK,
            "status": "skipped",
            "candidate_count": len(candidates),
            "auto_selected_count": 0,
            "decision_counts": {"recommend": 0, "review": 0, "reject": 0},
        }
        if not candidates:
            summary["status"] = "empty"
            return summary
        if not use_ai_evaluation:
            for candidate in candidates:
                candidate["ai_evaluation"] = deterministic_candidate_evaluation(candidate, query, status="skipped", reason="当前检索默认使用规则评分。")
            summary["score_source"] = "deterministic_rules"
            summary["score_framework"] = DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK
            summary["decision_counts"] = ai_evaluation_decision_counts(candidates)
            return summary
        if not model.get("configured"):
            for candidate in candidates:
                candidate["ai_evaluation"] = deterministic_candidate_evaluation(candidate, query, status="not_configured", reason="模型 API 未配置，已使用规则评分。")
            summary["status"] = "not_configured"
            summary["score_source"] = "deterministic_rules"
            summary["score_framework"] = DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK
            summary["decision_counts"] = ai_evaluation_decision_counts(candidates)
            return summary

        metadata = [candidate_metadata_for_ai(candidate, index) for index, candidate in enumerate(candidates, start=1)]
        id_map = {item["candidate_id"]: candidate for item, candidate in zip(metadata, candidates, strict=False)}
        accepted_total = 0
        rejected_total = 0
        failed_errors: list[str] = []
        chunks = chunked_ai_candidate_metadata(metadata)
        for chunk in chunks:
            chunk_candidates = [id_map[item["candidate_id"]] for item in chunk if item.get("candidate_id") in id_map]
            try:
                messages = retrieval_candidate_ai_messages(query, chunk)
                model_response = (
                    ai_pixel_chat_json(
                        messages,
                        post_json=ai_post_json,
                        max_tokens=1800,
                        timeout_seconds=AI_CANDIDATE_EVALUATION_TIMEOUT_SECONDS,
                    )
                    if callable(ai_post_json)
                    else ai_pixel_chat_json(
                        messages,
                        max_tokens=1800,
                        timeout_seconds=AI_CANDIDATE_EVALUATION_TIMEOUT_SECONDS,
                    )
                )
                raw_evaluations = model_response.get("evaluations") or model_response.get("candidates") or []
                applied = apply_ai_evaluations_to_candidates(
                    candidates,
                    raw_evaluations,
                    id_map,
                    fallback_candidates=chunk_candidates,
                )
                accepted_total += applied["accepted"]
                rejected_total += applied["rejected"]
            except Exception as exc:  # noqa: BLE001 - one slow/invalid batch should not discard prior AI scores.
                message = ai_evaluation_error_message(exc)
                failed_errors.append(message)
                for candidate in chunk_candidates:
                    if isinstance(candidate.get("ai_evaluation"), dict):
                        continue
                    candidate["ai_evaluation"] = deterministic_candidate_evaluation(
                        candidate,
                        query,
                        status="fallback",
                        reason=f"该候选所在批次 AI 评分失败：{message}",
                    )
        summary["evaluation_batch_count"] = len(chunks)
        summary["accepted_evaluation_count"] = accepted_total
        summary["rejected_evaluation_count"] = rejected_total
        summary["failed_batch_count"] = len(failed_errors)
        if failed_errors and accepted_total:
            summary["status"] = "partial"
            summary["score_source"] = "mixed_ai_rules"
            summary["score_framework"] = AI_CANDIDATE_SCORE_FRAMEWORK
            summary["error"] = "; ".join(dict.fromkeys(failed_errors[:3]))
        elif failed_errors:
            summary["status"] = "error"
            summary["error"] = "; ".join(dict.fromkeys(failed_errors[:3]))
            summary["score_source"] = "deterministic_rules"
            summary["score_framework"] = DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK
        elif accepted_total:
            summary["status"] = "evaluated"
            summary["score_source"] = "ai_model"
            summary["score_framework"] = AI_CANDIDATE_SCORE_FRAMEWORK
        else:
            summary["status"] = "error"
            summary["error"] = "AI 未返回有效候选评分。"
            summary["score_source"] = "deterministic_rules"
            summary["score_framework"] = DETERMINISTIC_CANDIDATE_SCORE_FRAMEWORK
            for candidate in candidates:
                if not isinstance(candidate.get("ai_evaluation"), dict):
                    candidate["ai_evaluation"] = deterministic_candidate_evaluation(
                        candidate,
                        query,
                        status="fallback",
                        reason="AI 评分失败，已使用规则元数据检查。",
                    )
        candidates[:] = sort_candidates_by_ai_evaluation(candidates)
        summary["decision_counts"] = ai_evaluation_decision_counts(candidates)
        summary["auto_selected_count"] = sum(1 for candidate in candidates if candidate.get("ai_evaluation", {}).get("auto_select"))
        return summary


def render_retrieval_query_plan_report_markdown(plan: dict[str, Any]) -> str:
    ai = plan.get("ai_enhancement") if isinstance(plan.get("ai_enhancement"), dict) else {}
    lines = [
        "# Retrieval query plan",
        "",
        f"- Generated at: {plan.get('generated_at', '')}",
        f"- Status: {plan.get('status', '')}",
        f"- Seed query: {plan.get('seed_query', '')}",
        f"- Sample size: {plan.get('sample_size', '')}",
        f"- Query count: {plan.get('query_count', '')}",
        f"- AI enhancement: {ai.get('status', 'skipped') if ai else 'skipped'}",
        f"- Conclusion: {plan.get('message', '')}",
        "",
        "## Draft Queries",
        "",
        "| Query | Reason | Sources | Samples |",
        "| --- | --- | --- | ---: |",
    ]
    for query_item in plan.get("queries") or []:
        if not isinstance(query_item, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                retrieval_query_plan_markdown_cell(value)
                for value in [
                    query_item.get("query"),
                    query_item.get("reason"),
                    ", ".join(str(source) for source in query_item.get("sources") or []) or "-",
                    query_item.get("sample_count") or 0,
                ]
            )
            + " |"
        )
    if ai:
        lines.extend(
            [
                "",
                "## AI Enhancement",
                "",
                "| Provider | Model | Status | Suggested | Accepted | Applied | Message |",
                "| --- | --- | --- | ---: | ---: | ---: | --- |",
                "| "
                + " | ".join(
                    retrieval_query_plan_markdown_cell(value)
                    for value in [
                        ai.get("provider") or "-",
                        ai.get("model") or "-",
                        ai.get("status") or "-",
                        ai.get("suggested_query_count") or 0,
                        ai.get("accepted_query_count") or 0,
                        ai.get("applied_query_count") or 0,
                        ai.get("message") or "-",
                    ]
                )
                + " |",
            ]
        )
        rejected = [item for item in ai.get("rejected") or [] if isinstance(item, dict)]
        if rejected:
            lines.extend(["", "| Rejected Query | Reason |", "| --- | --- |"])
            for item in rejected[:10]:
                lines.append(
                    "| "
                    + " | ".join(
                        retrieval_query_plan_markdown_cell(value)
                        for value in [item.get("query") or "", item.get("reason") or ""]
                    )
                    + " |"
                )
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            "| Query | Source | Sample | Title | Identifier | Creators |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    evidence_count = 0
    for query_item in plan.get("queries") or []:
        if not isinstance(query_item, dict):
            continue
        query_text = str(query_item.get("query") or "")
        for evidence in query_item.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            evidence_count += 1
            lines.append(
                "| "
                + " | ".join(
                    retrieval_query_plan_markdown_cell(value)
                    for value in [
                        query_text,
                        evidence.get("source"),
                        evidence.get("sample_index"),
                        evidence.get("title"),
                        evidence.get("identifier"),
                        evidence.get("creators"),
                    ]
                )
                + " |"
            )
    if not evidence_count:
        lines.append("| - | - | 0 | No preview evidence available. | - | - |")
    lines.extend(
        [
            "",
            "## Source Coverage",
            "",
            "| Source | Label | Status | Configured | Previewed | Samples |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for source in plan.get("sources") or []:
        if not isinstance(source, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                retrieval_query_plan_markdown_cell(value)
                for value in [
                    source.get("source"),
                    source.get("label"),
                    source.get("status"),
                    str(bool(source.get("configured"))).lower(),
                    str(bool(source.get("previewed"))).lower(),
                    source.get("sample_count") or 0,
                ]
            )
            + " |"
        )
    lines.extend(["", "## Next Steps", ""])
    recommendations = [str(item) for item in plan.get("recommendations") or [] if str(item or "").strip()]
    lines.extend(f"- {item}" for item in recommendations) if recommendations else lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_query_plan_report_csv(plan: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "section",
        "query",
        "status",
        "reason",
        "source",
        "source_count",
        "sample_count",
        "title",
        "identifier",
        "creators",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_query_plan_report_rows(plan))
    return output.getvalue()


def render_retrieval_query_plan_report_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2)


def render_retrieval_query_plan_report(plan: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_query_plan_report_csv(plan)
    if normalized == "json":
        return render_retrieval_query_plan_report_json(plan)
    return render_retrieval_query_plan_report_markdown(plan)


def retrieval_readiness_report_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in report.get("previews") or []:
        if not isinstance(entry, dict):
            continue
        quality = entry.get("quality") if isinstance(entry.get("quality"), dict) else {}
        field_map_suggestion = (
            entry.get("field_map_suggestion") if isinstance(entry.get("field_map_suggestion"), dict) else {}
        )
        field_map_quality = (
            field_map_suggestion.get("quality") if isinstance(field_map_suggestion.get("quality"), dict) else {}
        )
        recommendations = entry.get("recommendations") or quality.get("recommendations") or []
        row_recommendations = [str(item) for item in recommendations if item]
        if field_map_suggestion:
            field_count = safe_int(field_map_suggestion.get("suggested_field_count"))
            draft_label = "draft yes" if field_map_suggestion.get("draft_available") else "draft no"
            field_map_note = f"field_map {field_count} fields; {draft_label}"
            if field_map_note not in row_recommendations:
                row_recommendations.append(field_map_note)
        rows.append(
            {
                "name": str(entry.get("name") or ""),
                "label": str(entry.get("label") or ""),
                "source": str(entry.get("source") or ""),
                "configured": str(bool(entry.get("configured"))).lower(),
                "available": str(bool(entry.get("available"))).lower(),
                "previewed": str(bool(entry.get("previewed"))).lower(),
                "status": str(entry.get("status") or ""),
                "sample_count": str(entry.get("sample_count") or 0),
                "score": str(quality.get("score") if quality.get("score") is not None else ""),
                "row_count": str(quality.get("row_count") or 0),
                "rows_with_issues": str(quality.get("rows_with_issues") or 0),
                "rows_with_errors": str(quality.get("rows_with_errors") or 0),
                "field_map_status": str(field_map_suggestion.get("status") or field_map_quality.get("status") or ""),
                "field_map_fields": str(field_map_suggestion.get("suggested_field_count") or ""),
                "field_map_draft_available": str(bool(field_map_suggestion.get("draft_available"))).lower(),
                "field_map_message": str(field_map_suggestion.get("message") or ""),
                "message": str(entry.get("message") or ""),
                "recommendations": "；".join(row_recommendations),
            }
        )
    return rows


def render_retrieval_readiness_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# 多源检索上线前预检报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 预检状态：{report.get('status', '')}",
        f"- 查询词：{report.get('query', '')}",
        f"- 样本数：{report.get('sample_size', '')}",
        f"- 包含健康检查：{'是' if report.get('include_health') else '否'}",
        f"- 结论：{report.get('message', '')}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 数据源总数 | {summary.get('source_count', 0)} |",
        f"| 可用数据源 | {summary.get('available_source_count', 0)} |",
        f"| 已配置内部源 | {summary.get('configured_internal_count', 0)} |",
        f"| 已预览内部源 | {summary.get('previewed_internal_count', 0)} |",
        f"| 样本总数 | {summary.get('sample_count', 0)} |",
        f"| 警告数 | {summary.get('warning_count', 0)} |",
        f"| 阻断数 | {summary.get('blocking_count', 0)} |",
        "",
        "## 内部源预检",
        "",
        "| 源 | 配置来源 | 配置 | 预览 | 状态 | 样本 | 分数 | 问题行 | 建议 |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in retrieval_readiness_report_rows(report):
        cells = [
            row["label"] or row["name"],
            row["source"] or "-",
            "是" if row["configured"] == "true" else "否",
            "是" if row["previewed"] == "true" else "否",
            row["status"] or "-",
            row["sample_count"],
            row["score"] or "-",
            row["rows_with_issues"],
            row["recommendations"] or row["message"] or "-",
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    recommendations = [str(item) for item in report.get("recommendations") or [] if str(item or "").strip()]
    lines.extend(["", "## 修复建议", ""])
    if recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- 暂无。")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_readiness_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "name",
        "label",
        "source",
        "configured",
        "available",
        "previewed",
        "status",
        "sample_count",
        "score",
        "row_count",
        "rows_with_issues",
        "rows_with_errors",
        "field_map_status",
        "field_map_fields",
        "field_map_draft_available",
        "field_map_message",
        "message",
        "recommendations",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_readiness_report_rows(report))
    return output.getvalue()


def render_retrieval_readiness_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_retrieval_readiness_report(report: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_readiness_report_csv(report)
    if normalized == "json":
        return render_retrieval_readiness_report_json(report)
    return render_retrieval_readiness_report_markdown(report)


def retrieval_config_bundle_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), dict) else {}
    source_rows: list[dict[str, Any]] = []
    for name, entry in sources.items():
        if not isinstance(entry, dict):
            continue
        source_rows.append(
            {
                "name": str(name),
                "label": str(entry.get("label") or name),
                "configured": bool(entry.get("configured")),
                "source": str(entry.get("source") or ""),
                "redacted": str(name) in set(bundle.get("redacted_sources") or []),
            }
        )
    return {
        "schema": str(bundle.get("schema") or ""),
        "redacted": bool(bundle.get("redacted")),
        "redacted_sources": list(bundle.get("redacted_sources") or []),
        "source_count": len(source_rows),
        "configured_source_count": sum(1 for row in source_rows if row.get("configured")),
        "sources": source_rows,
        "download_endpoint": "/retrieval/config-bundle/download",
    }


def dedupe_recommendations(values: list[str], *, limit: int = 10) -> list[str]:
    recommendations: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in recommendations:
            recommendations.append(text)
        if len(recommendations) >= limit:
            break
    return recommendations


def unique_retrieval_source_names(values: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(values, (list, tuple, set)):
        return names
    for value in values:
        raw = value.get("name") if isinstance(value, dict) else value
        name = str(raw or "").strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def configured_internal_source_names(readiness: dict[str, Any]) -> list[str]:
    previews = readiness.get("previews") if isinstance(readiness.get("previews"), list) else []
    return unique_retrieval_source_names(
        [
            entry
            for entry in previews
            if isinstance(entry, dict) and (entry.get("configured") or entry.get("status") == "error")
        ]
    )


SOURCE_FIELD_MAP_REPORTS = {
    "localfile": {
        "slug": "local-files",
        "endpoint": "local-files",
        "label": "Local field_map report",
        "include_query": False,
    },
    "httpjson": {
        "slug": "http-json",
        "endpoint": "http-json",
        "label": "HTTP JSON field_map report",
        "include_query": True,
    },
    "sqlite": {
        "slug": "sqlite",
        "endpoint": "sqlite",
        "label": "SQLite field_map report",
        "include_query": True,
    },
    "manifest": {
        "slug": "manifest",
        "endpoint": "manifest",
        "label": "Object Manifest field_map report",
        "include_query": False,
    },
}


def source_field_map_report_params(config: dict[str, Any], *, query: str, sample_size: int) -> dict[str, Any]:
    params: dict[str, Any] = {"format": "markdown"}
    if config.get("include_query"):
        params["query"] = query
    params["sample_size"] = sample_size
    return params


def retrieval_source_field_map_report_artifacts(
    readiness: dict[str, Any], *, query: str, sample_size: int
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for source_name in configured_internal_source_names(readiness):
        config = SOURCE_FIELD_MAP_REPORTS.get(source_name)
        if not config:
            continue
        endpoint = (
            f"/retrieval/{config['endpoint']}/field-map/report?"
            + urlencode(source_field_map_report_params(config, query=query, sample_size=sample_size))
        )
        artifacts.append({"label": str(config["label"]), "endpoint": endpoint})
    return artifacts


def retrieval_readiness_field_map_report_entries(
    readiness: dict[str, Any], *, query: str, sample_size: int
) -> list[dict[str, Any]]:
    previews = readiness.get("previews") if isinstance(readiness.get("previews"), list) else []
    previews_by_name = {
        str(entry.get("name") or ""): entry
        for entry in previews
        if isinstance(entry, dict) and str(entry.get("name") or "")
    }
    entries: list[dict[str, Any]] = []
    for source_name in configured_internal_source_names(readiness):
        config = SOURCE_FIELD_MAP_REPORTS.get(source_name)
        preview = previews_by_name.get(source_name)
        if not config or not isinstance(preview, dict):
            continue
        suggestion = preview.get("field_map_suggestion")
        if not isinstance(suggestion, dict) or not suggestion:
            continue
        report = dict(suggestion)
        report.setdefault("schema", "web-library.retrieval-field-map-report/v1")
        report.setdefault("generated_at", readiness.get("generated_at") or now_iso())
        report.setdefault("source_type", source_name)
        report.setdefault(
            "quality",
            {
                "status": report.get("status") or preview.get("status") or "",
                "recommendations": report.get("recommendations") or preview.get("recommendations") or [],
            },
        )
        report["source_name"] = source_name
        report["source_label"] = str(preview.get("label") or config["label"])
        report["source_config_source"] = str(preview.get("source") or "")
        report["sample_size"] = sample_size
        if config.get("include_query"):
            report["query"] = query
        entries.append(
            {
                "source": source_name,
                "slug": str(config["slug"]),
                "label": str(config["label"]),
                "report": report,
            }
        )
    return entries


def retrieval_batch_source_evidence(
    items: list[dict[str, Any]], requested_sources: Any = None
) -> list[dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for source in unique_retrieval_source_names(requested_sources or []):
        evidence[source] = {
            "source": source,
            "requested": True,
            "query_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "candidate_count": 0,
            "elapsed_ms": 0,
            "latest_error_kind": "",
            "latest_diagnostic": "",
        }
    for item in items:
        if not isinstance(item, dict):
            continue
        source_stats = item.get("source_stats") if isinstance(item.get("source_stats"), dict) else {}
        for raw_source, stats in source_stats.items():
            source = str(raw_source or "").strip().lower()
            if not source or not isinstance(stats, dict):
                continue
            entry = evidence.setdefault(
                source,
                {
                    "source": source,
                    "requested": False,
                    "query_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "candidate_count": 0,
                    "elapsed_ms": 0,
                    "latest_error_kind": "",
                    "latest_diagnostic": "",
                },
            )
            entry["query_count"] = safe_int(entry.get("query_count")) + 1
            entry["candidate_count"] = safe_int(entry.get("candidate_count")) + safe_int(stats.get("count"))
            entry["elapsed_ms"] = safe_int(entry.get("elapsed_ms")) + safe_int(stats.get("elapsed_ms"))
            if stats.get("ok"):
                entry["success_count"] = safe_int(entry.get("success_count")) + 1
            else:
                entry["failure_count"] = safe_int(entry.get("failure_count")) + 1
                entry["latest_error_kind"] = str(stats.get("error_kind") or "").strip()
                entry["latest_diagnostic"] = str(stats.get("action") or stats.get("error") or "").strip()
    return sorted(evidence.values(), key=lambda item: str(item.get("source") or ""))


def merge_retrieval_batch_source_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for row in rows:
        for item in row.get("source_evidence") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip().lower()
            if not source:
                continue
            entry = evidence.setdefault(
                source,
                {
                    "source": source,
                    "requested": False,
                    "query_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "candidate_count": 0,
                    "elapsed_ms": 0,
                    "latest_error_kind": "",
                    "latest_diagnostic": "",
                },
            )
            entry["requested"] = bool(entry.get("requested") or item.get("requested"))
            for key in ("query_count", "success_count", "failure_count", "candidate_count", "elapsed_ms"):
                entry[key] = safe_int(entry.get(key)) + safe_int(item.get(key))
            if item.get("latest_error_kind"):
                entry["latest_error_kind"] = str(item.get("latest_error_kind") or "")
            if item.get("latest_diagnostic"):
                entry["latest_diagnostic"] = str(item.get("latest_diagnostic") or "")
    return sorted(evidence.values(), key=lambda item: str(item.get("source") or ""))


def retrieval_batch_validation_state(summary: dict[str, Any]) -> tuple[str, str]:
    if not safe_int(summary.get("job_count")):
        return (
            "missing",
            "No batch validation jobs found. Run a 3-5 query batch and download its report before handoff.",
        )
    if safe_int(summary.get("active_job_count")):
        return (
            "active",
            "Batch validation is still active. Wait for queued, running or paused jobs to finish, then refresh ONB.",
        )
    if safe_int(summary.get("failed_queries")) or safe_int(summary.get("failed_job_count")):
        return (
            "failed_queries",
            "Batch validation found failed queries. Review the latest batch report and retry failures before handoff.",
        )
    if safe_int(summary.get("canceled_job_count")) or safe_int(summary.get("completed_queries")) < safe_int(
        summary.get("total_queries")
    ):
        return (
            "incomplete",
            "Batch validation did not finish cleanly. Resume, retry or rerun the batch before handoff.",
        )
    if str(summary.get("config_context_status") or "") == "mismatch":
        return (
            "config_drift",
            "Latest batch validation was created under a different retrieval config. Rerun the batch after saving current sources.",
        )
    missing_sources = unique_retrieval_source_names(summary.get("missing_sources") or [])
    if missing_sources:
        return (
            "source_gap",
            f"Batch validation did not cover configured internal source(s): {', '.join(missing_sources)}. "
            "Run a batch with those sources before handoff.",
        )
    source_errors = unique_retrieval_source_names(summary.get("source_errors") or [])
    if source_errors:
        return (
            "source_errors",
            f"Batch validation found source-level error(s): {', '.join(source_errors)}. "
            "Review source_stats in the latest batch report before handoff.",
        )
    missing_queries = [str(query) for query in summary.get("missing_queries") or [] if str(query or "").strip()]
    if missing_queries:
        return (
            "query_gap",
            "Batch validation did not cover current intake draft query/queries: "
            + ", ".join(missing_queries)
            + ". Run a batch from Use queries before accepting this source intake.",
        )
    if not safe_int(summary.get("total_candidates")):
        return (
            "no_candidates",
            "Batch validation finished without failures but returned no candidates. Recheck queries, sources and field_map.",
        )
    required_completed_queries = safe_int(summary.get("required_completed_queries"))
    if required_completed_queries and safe_int(summary.get("completed_queries")) < required_completed_queries:
        return (
            "low_sample",
            f"Batch validation only completed {safe_int(summary.get('completed_queries'))}/"
            f"{required_completed_queries} required queries. Run a 3-5 query batch before handoff.",
        )
    return (
        "passed",
        "Recent batch validation completed with enough query samples, candidates and no failed queries.",
    )


def retrieval_batch_validation_remediation(summary: dict[str, Any]) -> dict[str, Any]:
    status = str(summary.get("status") or "")
    latest_job_id = str(summary.get("latest_job_id") or "")
    latest_report_endpoint = str(summary.get("latest_report_endpoint") or "")
    latest_source_report_endpoint = str(summary.get("latest_source_report_endpoint") or "")
    latest_job = (summary.get("jobs") or [{}])[0] if isinstance(summary.get("jobs"), list) else {}
    latest_queries = unique_retrieval_query_texts(latest_job.get("queries") if isinstance(latest_job, dict) else [])
    latest_sources = unique_retrieval_source_names(latest_job.get("sources") if isinstance(latest_job, dict) else [])
    required_queries = unique_retrieval_query_texts(summary.get("required_queries") or [])
    missing_queries = unique_retrieval_query_texts(summary.get("missing_queries") or [])
    required_sources = unique_retrieval_source_names(summary.get("required_sources") or [])
    missing_sources = unique_retrieval_source_names(summary.get("missing_sources") or [])

    def action(
        name: str,
        label: str,
        endpoint: str,
        *,
        method: str = "GET",
        queries: list[str] | None = None,
        sources: list[str] | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        return {
            "action": name,
            "label": label,
            "endpoint": endpoint,
            "method": method,
            "queries": queries or [],
            "sources": sources or [],
            "message": message or str(summary.get("message") or ""),
        }

    if status == "failed_queries" and latest_job_id:
        return action(
            "retry_failed_queries",
            "Retry failed queries",
            f"/retrieval/batches/{latest_job_id}/retry-failed",
            method="POST",
            queries=latest_queries,
            sources=latest_sources,
        )
    if status == "config_drift":
        return action(
            "rerun_current_config_batch",
            "Run current-config batch",
            "/retrieval/batches",
            method="POST",
            queries=required_queries or latest_queries,
            sources=required_sources or latest_sources,
        )
    if status == "query_gap":
        return action(
            "run_required_query_batch",
            "Run required-query batch",
            "/retrieval/batches",
            method="POST",
            queries=missing_queries or required_queries,
            sources=required_sources or latest_sources,
        )
    if status == "source_gap":
        return action(
            "run_missing_source_batch",
            "Run missing-source batch",
            "/retrieval/batches",
            method="POST",
            queries=required_queries or latest_queries,
            sources=missing_sources,
        )
    if status in {"missing", "low_sample", "no_candidates"}:
        return action(
            "run_validation_batch",
            "Run validation batch",
            "/retrieval/batches",
            method="POST",
            queries=missing_queries or required_queries or latest_queries,
            sources=required_sources or latest_sources,
        )
    if status == "source_errors":
        return action(
            "review_source_errors",
            "Download Source CSV",
            latest_source_report_endpoint or latest_report_endpoint,
        )
    if status in {"active", "incomplete"}:
        return action(
            "review_batch_progress",
            "Open latest batch report",
            latest_report_endpoint,
        )
    if status == "passed":
        return action(
            "download_batch_report",
            "Download batch report",
            latest_report_endpoint,
        )
    return action("review_batch_validation", "Review batch validation", latest_report_endpoint or "/retrieval/batches")


def retrieval_query_coverage_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def unique_retrieval_query_texts(values: Any) -> list[str]:
    if isinstance(values, str):
        raw_values = re.split(r"[\r\n]+", values)
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    else:
        return []
    queries: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        query = re.sub(r"\s+", " ", str(value or "").strip())
        key = query.casefold()
        if not query or key in seen:
            continue
        queries.append(query)
        seen.add(key)
    return queries


def retrieval_batch_validation_summary(
    library_id: str,
    limit: int = 5,
    required_sources: Any = None,
    required_queries: Any = None,
    required_completed_queries: int = RETRIEVAL_BATCH_VALIDATION_MIN_COMPLETED_QUERIES,
) -> dict[str, Any]:
    jobs = app_store.recent_retrieval_batch_jobs(library_id, limit=limit)
    current_context = retrieval_batch_context_for_library(library_id)
    current_config_fingerprint = str(current_context.get("config_fingerprint") or "")
    rows: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "")
        try:
            job_items = app_store.retrieval_batch_items_for_job(library_id, job_id) if job_id else []
        except ValueError:
            job_items = []
        source_evidence = retrieval_batch_source_evidence(job_items, job.get("sources") or [])
        context = job.get("context") if isinstance(job.get("context"), dict) else {}
        context_fingerprint = str(context.get("config_fingerprint") or "")
        if not context_fingerprint:
            config_context_status = "unknown"
        elif context_fingerprint == current_config_fingerprint:
            config_context_status = "matched"
        else:
            config_context_status = "mismatch"
        rows.append(
            {
                "job_id": job_id,
                "status": str(job.get("status") or ""),
                "total_queries": safe_int(job.get("total_queries")),
                "completed_queries": safe_int(job.get("completed_queries")),
                "failed_queries": safe_int(job.get("failed_queries")),
                "remaining_queries": safe_int(job.get("remaining_queries")),
                "total_candidates": safe_int(job.get("total_candidates")),
                "progress": job.get("progress") or 0,
                "sources": list(job.get("sources") or []),
                "queries": unique_retrieval_query_texts(job.get("queries") or []),
                "completed_queries_text": unique_retrieval_query_texts(
                    [
                        item.get("query")
                        for item in job_items
                        if isinstance(item, dict) and item.get("status") == "completed"
                    ]
                ),
                "source_evidence": source_evidence,
                "config_context_status": config_context_status,
                "config_fingerprint": context_fingerprint,
                "config_context_generated_at": str(context.get("generated_at") or ""),
                "created_at": str(job.get("created_at") or ""),
                "updated_at": str(job.get("updated_at") or ""),
                "report_endpoint": f"/retrieval/batches/{job_id}/report" if job_id else "",
                "source_report_endpoint": f"/retrieval/batches/{job_id}/report?format=csv&scope=sources"
                if job_id
                else "",
            }
        )
    latest = rows[0] if rows else {}
    config_context_status = str(latest.get("config_context_status") or ("missing" if not rows else "unknown"))
    required_source_names = unique_retrieval_source_names(required_sources or [])
    source_evidence = merge_retrieval_batch_source_evidence(rows)
    validated_source_names = [
        str(item.get("source") or "")
        for item in source_evidence
        if str(item.get("source") or "") and safe_int(item.get("query_count"))
    ]
    missing_source_names = [source for source in required_source_names if source not in validated_source_names]
    source_error_names = [
        str(item.get("source") or "")
        for item in source_evidence
        if str(item.get("source") or "") and safe_int(item.get("failure_count"))
    ]
    required_query_texts = unique_retrieval_query_texts(required_queries or [])
    completed_query_texts = unique_retrieval_query_texts(
        [query for row in rows for query in row.get("completed_queries_text") or []]
    )
    completed_query_keys = {retrieval_query_coverage_key(query) for query in completed_query_texts}
    covered_query_texts = [
        query for query in required_query_texts if retrieval_query_coverage_key(query) in completed_query_keys
    ]
    missing_query_texts = [
        query for query in required_query_texts if retrieval_query_coverage_key(query) not in completed_query_keys
    ]
    completed_queries = sum(safe_int(row.get("completed_queries")) for row in rows)
    required_query_count = max(1, int(required_completed_queries or RETRIEVAL_BATCH_VALIDATION_MIN_COMPLETED_QUERIES))
    summary = {
        "job_count": len(rows),
        "completed_job_count": sum(1 for row in rows if row.get("status") == "completed"),
        "active_job_count": sum(1 for row in rows if row.get("status") in {"queued", "running", "paused"}),
        "failed_job_count": sum(1 for row in rows if row.get("status") == "failed"),
        "canceled_job_count": sum(1 for row in rows if row.get("status") == "canceled"),
        "total_queries": sum(safe_int(row.get("total_queries")) for row in rows),
        "completed_queries": completed_queries,
        "required_completed_queries": required_query_count,
        "completed_query_gap": max(0, required_query_count - completed_queries),
        "failed_queries": sum(safe_int(row.get("failed_queries")) for row in rows),
        "total_candidates": sum(safe_int(row.get("total_candidates")) for row in rows),
        "latest_job_id": str(latest.get("job_id") or ""),
        "latest_status": str(latest.get("status") or ""),
        "latest_report_endpoint": str(latest.get("report_endpoint") or ""),
        "latest_source_report_endpoint": str(latest.get("source_report_endpoint") or ""),
        "current_config_fingerprint": current_config_fingerprint,
        "config_context_status": config_context_status,
        "config_matched_job_count": sum(1 for row in rows if row.get("config_context_status") == "matched"),
        "config_mismatch_job_count": sum(1 for row in rows if row.get("config_context_status") == "mismatch"),
        "config_unknown_job_count": sum(1 for row in rows if row.get("config_context_status") == "unknown"),
        "latest_config_fingerprint": str(latest.get("config_fingerprint") or ""),
        "required_sources": required_source_names,
        "required_source_count": len(required_source_names),
        "validated_sources": validated_source_names,
        "validated_source_count": len(validated_source_names),
        "missing_sources": missing_source_names,
        "missing_source_count": len(missing_source_names),
        "source_evidence": source_evidence,
        "source_errors": source_error_names,
        "source_error_count": len(source_error_names),
        "required_queries": required_query_texts,
        "required_query_count": len(required_query_texts),
        "covered_queries": covered_query_texts,
        "covered_query_count": len(covered_query_texts),
        "missing_queries": missing_query_texts,
        "missing_query_count": len(missing_query_texts),
        "completed_query_texts": completed_query_texts,
        "jobs": rows,
    }
    status, message = retrieval_batch_validation_state(summary)
    summary["status"] = status
    summary["message"] = message
    summary["remediation"] = retrieval_batch_validation_remediation(summary)
    return summary


def retrieval_batch_import_readiness(
    library_id: str, batch_validation: dict[str, Any], sample_limit: int = 10
) -> dict[str, Any]:
    clean_limit = max(1, min(int(sample_limit or 10), 50))
    candidates: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    run_ids: list[str] = []
    for job in batch_validation.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        try:
            items = app_store.retrieval_batch_items_for_job(library_id, job_id)
        except ValueError:
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("status") != "completed":
                continue
            run_id = str(item.get("run_id") or "")
            if not run_id or run_id in run_ids:
                continue
            run_ids.append(run_id)
            try:
                run_report = app_store.retrieval_run_report(library_id, run_id)
            except ValueError:
                continue
            for row in run_report.get("candidates") or []:
                if not isinstance(row, dict):
                    continue
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                candidate = payload or row
                candidate_id = str(candidate.get("candidate_id") or row.get("candidate_id") or "")
                if candidate_id and candidate_id in seen_candidate_ids:
                    continue
                if candidate_id:
                    seen_candidate_ids.add(candidate_id)
                candidates.append(candidate)
                if len(candidates) >= clean_limit:
                    break
            if len(candidates) >= clean_limit:
                break
        if len(candidates) >= clean_limit:
            break

    ready_count = 0
    title_missing_count = 0
    error_count = 0
    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        source = str(candidate.get("source") or "")
        title = str(candidate.get("title") or "")
        try:
            imported = imported_item_from_candidate(candidate)
            ready_count += 1
            imported_title = str(imported.fields.get("title") or title).strip()
            if not imported_title:
                title_missing_count += 1
            checked.append(
                {
                    "candidate_id": candidate_id,
                    "source": source or imported.source,
                    "title": imported_title or title,
                    "item_type": imported.item_type,
                    "status": "warning" if not imported_title else "ready",
                    "message": "missing title" if not imported_title else "ready for import model",
                }
            )
        except CandidateImportError as exc:
            error_count += 1
            message = str(exc)
            errors.append(message)
            checked.append(
                {
                    "candidate_id": candidate_id,
                    "source": source,
                    "title": title,
                    "item_type": "",
                    "status": "error",
                    "message": message,
                }
            )

    checked_count = len(candidates)
    if not batch_validation.get("job_count"):
        status = "missing"
        message = "No batch candidates are available for import readiness."
    elif not checked_count:
        status = "needs_sampling"
        message = "Recent batch evidence has no cached candidates to validate against the import model."
    elif ready_count == 0:
        status = "blocked"
        message = "No sampled batch candidates can be converted into the existing library import model."
    elif error_count or title_missing_count:
        status = "warning"
        message = "Some sampled candidates need field mapping fixes before import."
    else:
        status = "passed"
        message = "Sampled batch candidates can be converted into the existing library import model."

    return {
        "status": status,
        "message": message,
        "sample_limit": clean_limit,
        "checked_candidate_count": checked_count,
        "ready_candidate_count": ready_count,
        "error_candidate_count": error_count,
        "title_missing_count": title_missing_count,
        "run_count": len(run_ids),
        "run_ids": run_ids,
        "batch_candidate_count": safe_int(batch_validation.get("total_candidates")),
        "errors": errors[:5],
        "candidates": checked[:clean_limit],
    }


def retrieval_onboarding_status(
    readiness_status: str,
    tuning_status: str,
    batch_validation_status: str = "",
    import_readiness_status: str = "",
) -> tuple[str, str]:
    if readiness_status == "blocked" or tuning_status == "blocked":
        return "blocked", "先修复配置、鉴权或字段映射阻断项，再做批量接入。"
    if import_readiness_status == "blocked":
        return "blocked", "Batch candidates cannot be converted into the existing library import model yet."
    if batch_validation_status == "failed_queries":
        return "warning", "Batch validation found failed queries; review and retry them before accepting the setup."
    if batch_validation_status == "source_errors":
        return "warning", "Batch validation found source-level errors; review source_stats before accepting the setup."
    if import_readiness_status == "warning":
        return "warning", "Batch candidates are mostly importable, but field mapping issues still need review."
    if batch_validation_status == "config_drift":
        return "needs_sampling", "Batch validation was generated from an older source config; rerun a current-config batch."
    if readiness_status == "warning" or tuning_status == "warning":
        return "warning", "接入链路可继续验证，但仍有预检或稳定性警告需要复核。"
    if batch_validation_status == "active":
        return "needs_sampling", "Batch validation is still active; refresh ONB after the batch finishes."
    if batch_validation_status in {"missing", "incomplete", "query_gap", "source_gap", "no_candidates", "low_sample"}:
        return "needs_sampling", "Batch validation evidence is not complete enough for handoff yet."
    if tuning_status in {"no_data", "observing"}:
        return "needs_sampling", "配置预检已可用，下一步需要用真实 query 小批量采样。"
    return "ready", "当前配置、预检和调优信号满足阶段验收要求。"


def retrieval_onboarding_acceptance_gates(
    readiness: dict[str, Any],
    tuning: dict[str, Any],
    batch_validation: dict[str, Any],
    import_readiness: dict[str, Any],
    bundle_summary: dict[str, Any],
    totals: dict[str, Any],
    query_plan: dict[str, Any],
    validation_queries: list[str],
    *,
    validation_query_source: str,
    query: str,
    sample_size: int,
    limit: int,
) -> list[dict[str, Any]]:
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    tuning_summary = tuning.get("summary") if isinstance(tuning.get("summary"), dict) else {}
    readiness_status = str(readiness.get("status") or "")
    if readiness_status == "ready":
        readiness_gate_status = "passed"
    elif readiness_status == "blocked":
        readiness_gate_status = "blocked"
    elif readiness_status == "warning":
        readiness_gate_status = "warning"
    else:
        readiness_gate_status = "needs_sampling"

    batch_status = str(batch_validation.get("status") or "")
    if batch_status == "passed":
        batch_gate_status = "passed"
    elif batch_status in {"failed_queries", "source_errors"}:
        batch_gate_status = "warning"
    else:
        batch_gate_status = "needs_sampling"

    import_status = str(import_readiness.get("status") or "")
    if import_status == "passed":
        import_gate_status = "passed"
    elif import_status == "blocked":
        import_gate_status = "blocked"
    elif import_status == "warning":
        import_gate_status = "warning"
    else:
        import_gate_status = "needs_sampling"

    tuning_status = str(tuning.get("status") or "")
    if tuning_status == "healthy":
        tuning_gate_status = "passed"
    elif tuning_status == "blocked":
        tuning_gate_status = "blocked"
    elif tuning_status == "warning":
        tuning_gate_status = "warning"
    else:
        tuning_gate_status = "needs_sampling"

    configured_bundle_sources = safe_int(bundle_summary.get("configured_source_count"))
    redacted_bundle_sources = len(bundle_summary.get("redacted_sources") or [])
    if not configured_bundle_sources:
        bundle_gate_status = "needs_sampling"
    elif redacted_bundle_sources:
        bundle_gate_status = "warning"
    else:
        bundle_gate_status = "passed"

    has_batch_report = bool(batch_validation.get("latest_report_endpoint"))
    has_source_csv = bool(batch_validation.get("latest_source_report_endpoint"))
    has_config_bundle = bool(bundle_summary.get("download_endpoint"))
    handoff_gate_status = "passed" if has_batch_report and has_source_csv and has_config_bundle else "needs_sampling"
    query_plan_ai = query_plan.get("ai_enhancement") if isinstance(query_plan.get("ai_enhancement"), dict) else {}
    query_plan_params = {"format": "markdown", "seed_query": query, "sample_size": sample_size, "limit": 5}
    onboarding_params: dict[str, Any] = {"format": "markdown", "query": query, "sample_size": sample_size, "limit": limit}
    onboarding_package_params: dict[str, Any] = {"query": query, "sample_size": sample_size, "limit": limit}
    validation_query_text = "\n".join(validation_queries)
    if query_plan_ai.get("requested"):
        query_plan_params["use_ai"] = "1"
        onboarding_params["use_ai"] = "1"
        onboarding_package_params["use_ai"] = "1"
    if validation_query_source == "explicit" and validation_query_text:
        onboarding_params["required_queries"] = validation_query_text
        onboarding_package_params["required_queries"] = validation_query_text
    readiness_report_endpoint = "/retrieval/readiness/report?" + urlencode(
        {"format": "markdown", "query": query, "sample_size": sample_size}
    )
    query_plan_report_endpoint = "/retrieval/query-plan/report?" + urlencode(query_plan_params)
    tuning_report_endpoint = "/retrieval/tuning/report?" + urlencode({"format": "markdown", "limit": limit})
    source_setup_report_endpoint = "/retrieval/sources/report?" + urlencode({"format": "markdown"})
    config_bundle_endpoint = str(bundle_summary.get("download_endpoint") or "/retrieval/config-bundle/download")
    onboarding_report_endpoint = "/retrieval/onboarding/report?" + urlencode(onboarding_params)
    onboarding_package_endpoint = "/retrieval/onboarding/package?" + urlencode(onboarding_package_params)
    batch_report_endpoint = str(batch_validation.get("latest_report_endpoint") or "")
    source_csv_endpoint = str(batch_validation.get("latest_source_report_endpoint") or "")
    batch_remediation = batch_validation.get("remediation") if isinstance(batch_validation.get("remediation"), dict) else {}
    if batch_status == "query_gap":
        batch_action_label = "Download ONB report" if validation_query_source == "explicit" else "Download PLAN report"
        batch_action_endpoint = onboarding_report_endpoint if validation_query_source == "explicit" else query_plan_report_endpoint
    elif batch_report_endpoint:
        batch_action_label = "Download batch report"
        batch_action_endpoint = batch_report_endpoint
    else:
        batch_action_label = "Run 3-5 query batch"
        batch_action_endpoint = batch_report_endpoint
    if batch_remediation.get("label"):
        batch_action_label = str(batch_remediation.get("label") or batch_action_label)
        batch_action_endpoint = str(batch_remediation.get("endpoint") or batch_action_endpoint)
    field_map_report_artifacts = retrieval_source_field_map_report_artifacts(
        readiness, query=query, sample_size=sample_size
    )

    return [
        {
            "name": "source_readiness",
            "label": "Source readiness",
            "status": readiness_gate_status,
            "required": True,
            "evidence": (
                f"{safe_int(readiness_summary.get('configured_internal_count'))} configured, "
                f"{safe_int(readiness_summary.get('previewed_internal_count'))} previewed, "
                f"{safe_int(readiness_summary.get('blocking_count'))} blocking, "
                f"{safe_int(readiness_summary.get('warning_count'))} warnings"
            ),
            "message": str(readiness.get("message") or "Run readiness before handoff."),
            "action_label": "Download readiness report",
            "action_endpoint": readiness_report_endpoint,
            "artifacts": [{"label": "READY report", "endpoint": readiness_report_endpoint}],
        },
        {
            "name": "batch_validation",
            "label": "Batch validation",
            "status": batch_gate_status,
            "required": True,
            "evidence": (
                f"{safe_int(batch_validation.get('completed_queries'))}/"
                f"{safe_int(batch_validation.get('required_completed_queries'))} query samples, "
                f"{safe_int(batch_validation.get('covered_query_count'))}/"
                f"{safe_int(batch_validation.get('required_query_count'))} "
                f"{'explicit queries' if validation_query_source == 'explicit' else 'PLAN queries'}, "
                f"{safe_int(batch_validation.get('validated_source_count'))}/"
                f"{safe_int(batch_validation.get('required_source_count'))} sources, "
                f"{safe_int(batch_validation.get('failed_queries'))} failed queries, "
                f"{safe_int(batch_validation.get('source_error_count'))} source errors, "
                f"config {str(batch_validation.get('config_context_status') or 'unknown')}"
            ),
            "message": str(batch_validation.get("message") or "Run a 3-5 query batch before handoff."),
            "action_label": batch_action_label,
            "action_endpoint": batch_action_endpoint,
            "action_method": str(batch_remediation.get("method") or "GET"),
            "remediation": batch_remediation,
            "artifacts": [
                item
                for item in [
                    {
                        "label": "ONB report" if validation_query_source == "explicit" else "PLAN report",
                        "endpoint": onboarding_report_endpoint if validation_query_source == "explicit" else query_plan_report_endpoint,
                    },
                    {"label": "Batch report", "endpoint": batch_report_endpoint},
                    {"label": "Source CSV", "endpoint": source_csv_endpoint},
                ]
                if item["endpoint"]
            ],
        },
        {
            "name": "tuning_signal",
            "label": "Tuning signal",
            "status": tuning_gate_status,
            "required": True,
            "evidence": (
                f"{safe_int(totals.get('run_count'))} runs, "
                f"{safe_int(tuning_summary.get('source_attempt_count'))} source attempts, "
                f"{safe_int(tuning_summary.get('needs_action_count'))} actions"
            ),
            "message": str(tuning.get("message") or "Collect tuning evidence from real queries."),
            "action_label": "Download tuning report",
            "action_endpoint": tuning_report_endpoint,
            "artifacts": [{"label": "TUNE report", "endpoint": tuning_report_endpoint}],
        },
        {
            "name": "import_readiness",
            "label": "Import readiness",
            "status": import_gate_status,
            "required": True,
            "evidence": (
                f"{safe_int(import_readiness.get('ready_candidate_count'))}/"
                f"{safe_int(import_readiness.get('checked_candidate_count'))} sampled candidates importable, "
                f"{safe_int(import_readiness.get('title_missing_count'))} missing titles, "
                f"{safe_int(import_readiness.get('error_candidate_count'))} conversion errors"
            ),
            "message": str(import_readiness.get("message") or "Run batch validation before checking import readiness."),
            "action_label": "Download batch report" if batch_report_endpoint else "Run validation batch",
            "action_endpoint": batch_report_endpoint or "/retrieval/batches",
            "artifacts": [
                item
                for item in [
                    {"label": "Batch report", "endpoint": batch_report_endpoint},
                    {"label": "Source CSV", "endpoint": source_csv_endpoint},
                ]
                if item["endpoint"]
            ],
        },
        {
            "name": "config_bundle",
            "label": "Config bundle",
            "status": bundle_gate_status,
            "required": True,
            "evidence": (
                f"{configured_bundle_sources}/{safe_int(bundle_summary.get('source_count'))} configured sources, "
                f"{redacted_bundle_sources} redacted sources"
            ),
            "message": (
                "Config bundle is ready; fill redacted secrets in the target environment."
                if redacted_bundle_sources
                else "Config bundle can be downloaded for handoff."
            ),
            "action_label": "Download config bundle",
            "action_endpoint": config_bundle_endpoint,
            "artifacts": [{"label": "CFG bundle", "endpoint": config_bundle_endpoint}],
        },
        {
            "name": "handoff_artifacts",
            "label": "Handoff artifacts",
            "status": handoff_gate_status,
            "required": True,
            "evidence": (
                f"batch report {'yes' if has_batch_report else 'no'}, "
                f"source csv {'yes' if has_source_csv else 'no'}, "
                f"config bundle {'yes' if has_config_bundle else 'no'}, "
                f"field map reports {len(field_map_report_artifacts)}"
            ),
            "message": (
                "Latest batch report, source CSV, field_map reports and redacted config bundle are linked."
                if handoff_gate_status == "passed"
                else "Run a batch and download report artifacts before handoff."
            ),
            "action_label": "Download ONB package",
            "action_endpoint": onboarding_package_endpoint,
            "artifacts": [
                item
                for item in [
                    {"label": "ONB package", "endpoint": onboarding_package_endpoint},
                    {"label": "ONB report", "endpoint": onboarding_report_endpoint},
                    {"label": "Source setup", "endpoint": source_setup_report_endpoint},
                    {"label": "PLAN report", "endpoint": query_plan_report_endpoint},
                    *field_map_report_artifacts,
                    {"label": "Batch report", "endpoint": batch_report_endpoint},
                    {"label": "Source CSV", "endpoint": source_csv_endpoint},
                    {"label": "CFG bundle", "endpoint": config_bundle_endpoint},
                ]
                if item["endpoint"]
            ],
        },
    ]


def retrieval_onboarding_gate_summary(gates: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [str(gate.get("status") or "") for gate in gates if isinstance(gate, dict)]
    return {
        "acceptance_gate_count": len(statuses),
        "acceptance_gate_passed_count": sum(1 for status in statuses if status == "passed"),
        "acceptance_gate_warning_count": sum(1 for status in statuses if status == "warning"),
        "acceptance_gate_blocked_count": sum(1 for status in statuses if status == "blocked"),
        "acceptance_gate_needs_sampling_count": sum(1 for status in statuses if status == "needs_sampling"),
    }


def retrieval_onboarding_gate_action_text(gate: dict[str, Any]) -> str:
    parts = [str(gate.get("message") or "").strip()]
    action_label = str(gate.get("action_label") or "").strip()
    action_endpoint = str(gate.get("action_endpoint") or "").strip()
    action_method = str(gate.get("action_method") or "GET").strip().upper()
    if action_label and action_endpoint:
        method_label = f" [{action_method}]" if action_method and action_method != "GET" else ""
        parts.append(f"{action_label}{method_label}: {action_endpoint}")
    artifacts = []
    for artifact in gate.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        label = str(artifact.get("label") or "").strip()
        endpoint = str(artifact.get("endpoint") or "").strip()
        if label and endpoint:
            artifacts.append(f"{label}={endpoint}")
    if artifacts:
        parts.append("artifacts: " + "; ".join(artifacts))
    return " | ".join(part for part in parts if part)


def retrieval_onboarding_report_for_library(
    library_id: str,
    *,
    query: str,
    sample_size: int,
    include_health: bool,
    limit: int,
    use_ai: bool = False,
    required_queries: Any = None,
) -> dict[str, Any]:
    readiness = retrieval_readiness_report_for_library(
        library_id,
        query=query,
        sample_size=sample_size,
        include_health=include_health,
    )
    run_summary = app_store.retrieval_run_summary(library_id, limit=limit)
    tuning = retrieval_tuning_report(run_summary, readiness.get("sources") or [])
    bundle_summary = retrieval_config_bundle_summary(retrieval_config_bundle_for_library(library_id, redact=True))
    query_plan = retrieval_query_plan_for_library(
        library_id,
        seed_query=query,
        sample_size=sample_size,
        limit=5,
        use_ai=use_ai,
    )
    query_plan_queries = [
        str(item.get("query") or "")
        for item in query_plan.get("queries") or []
        if isinstance(item, dict) and str(item.get("query") or "").strip()
    ]
    explicit_required_queries = normalize_optional_retrieval_queries(required_queries)
    validation_queries = explicit_required_queries or query_plan_queries
    validation_query_source = "explicit" if explicit_required_queries else "query_plan"
    batch_validation = retrieval_batch_validation_summary(
        library_id,
        required_sources=configured_internal_source_names(readiness),
        required_queries=validation_queries,
    )
    import_readiness = retrieval_batch_import_readiness(library_id, batch_validation)
    status, message = retrieval_onboarding_status(
        str(readiness.get("status") or ""),
        str(tuning.get("status") or ""),
        str(batch_validation.get("status") or ""),
        str(import_readiness.get("status") or ""),
    )
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    tuning_summary = tuning.get("summary") if isinstance(tuning.get("summary"), dict) else {}
    totals = run_summary.get("totals") if isinstance(run_summary.get("totals"), dict) else {}
    acceptance_gates = retrieval_onboarding_acceptance_gates(
        readiness,
        tuning,
        batch_validation,
        import_readiness,
        bundle_summary,
        totals,
        query_plan,
        validation_queries,
        validation_query_source=validation_query_source,
        query=query,
        sample_size=sample_size,
        limit=limit,
    )
    gate_summary = retrieval_onboarding_gate_summary(acceptance_gates)
    field_map_report_artifacts = retrieval_source_field_map_report_artifacts(
        readiness, query=query, sample_size=sample_size
    )
    recommendations = dedupe_recommendations(
        [
            *[str(item) for item in readiness.get("recommendations") or []],
            *[str(item) for item in tuning.get("recommendations") or []],
            *[
                f"{gate.get('label')}: {gate.get('message')}"
                for gate in acceptance_gates
                if gate.get("status") != "passed"
            ],
            str(batch_validation.get("message") or "")
            if batch_validation.get("status") != "passed"
            else "",
            str(import_readiness.get("message") or "")
            if import_readiness.get("status") not in {"passed", ""}
            else "",
            "用 5-10 条真实 query 跑小批量检索，并复查 tuning report。"
            if tuning.get("status") in {"no_data", "observing"}
            else "",
            "下载 CFG 脱敏配置包，与队友同步环境变量名和本机路径差异。",
            "配置包中存在脱敏占位符；导入到其他环境前用 ${ENV:...} 或真实部署密钥补齐。"
            if bundle_summary.get("redacted_sources")
            else "",
        ]
    )
    return {
        "generated_at": now_iso(),
        "status": status,
        "message": message,
        "query": query,
        "sample_size": sample_size,
        "include_health": include_health,
        "limit": limit,
        "validation_queries": validation_queries,
        "validation_query_source": validation_query_source,
        "summary": {
            "readiness_status": readiness.get("status", ""),
            "tuning_status": tuning.get("status", ""),
            "query_plan_status": query_plan.get("status", ""),
            "query_plan_query_count": query_plan.get("query_count", 0),
            "validation_query_source": validation_query_source,
            "validation_query_count": len(validation_queries),
            "query_plan_ai_status": (
                (query_plan.get("ai_enhancement") or {}).get("status", "")
                if isinstance(query_plan.get("ai_enhancement"), dict)
                else ""
            ),
            "query_plan_ai_applied_count": (
                (query_plan.get("ai_enhancement") or {}).get("applied_query_count", 0)
                if isinstance(query_plan.get("ai_enhancement"), dict)
                else 0
            ),
            "source_count": readiness_summary.get("source_count", 0),
            "available_source_count": readiness_summary.get("available_source_count", 0),
            "configured_internal_count": readiness_summary.get("configured_internal_count", 0),
            "previewed_internal_count": readiness_summary.get("previewed_internal_count", 0),
            "blocking_count": readiness_summary.get("blocking_count", 0),
            "warning_count": readiness_summary.get("warning_count", 0),
            "run_count": totals.get("run_count", 0),
            "candidate_count": totals.get("candidate_count", 0),
            "imported_count": totals.get("imported_count", 0),
            "source_success_rate": totals.get("source_success_rate", 0),
            "tuning_needs_action_count": tuning_summary.get("needs_action_count", 0),
            "config_bundle_configured_source_count": bundle_summary.get("configured_source_count", 0),
            "config_bundle_redacted_source_count": len(bundle_summary.get("redacted_sources") or []),
            "batch_job_count": batch_validation.get("job_count", 0),
            "batch_validation_status": batch_validation.get("status", ""),
            "batch_validation_message": batch_validation.get("message", ""),
            "batch_required_source_count": batch_validation.get("required_source_count", 0),
            "batch_validated_source_count": batch_validation.get("validated_source_count", 0),
            "batch_missing_source_count": batch_validation.get("missing_source_count", 0),
            "batch_missing_sources": batch_validation.get("missing_sources", []),
            "batch_source_error_count": batch_validation.get("source_error_count", 0),
            "batch_source_errors": batch_validation.get("source_errors", []),
            "batch_completed_job_count": batch_validation.get("completed_job_count", 0),
            "batch_active_job_count": batch_validation.get("active_job_count", 0),
            "batch_total_queries": batch_validation.get("total_queries", 0),
            "batch_completed_queries": batch_validation.get("completed_queries", 0),
            "batch_required_completed_queries": batch_validation.get("required_completed_queries", 0),
            "batch_completed_query_gap": batch_validation.get("completed_query_gap", 0),
            "batch_required_query_count": batch_validation.get("required_query_count", 0),
            "batch_covered_query_count": batch_validation.get("covered_query_count", 0),
            "batch_missing_query_count": batch_validation.get("missing_query_count", 0),
            "batch_missing_queries": batch_validation.get("missing_queries", []),
            "batch_failed_queries": batch_validation.get("failed_queries", 0),
            "batch_total_candidates": batch_validation.get("total_candidates", 0),
            "import_readiness_status": import_readiness.get("status", ""),
            "import_readiness_checked_candidate_count": import_readiness.get("checked_candidate_count", 0),
            "import_readiness_ready_candidate_count": import_readiness.get("ready_candidate_count", 0),
            "import_readiness_error_candidate_count": import_readiness.get("error_candidate_count", 0),
            "import_readiness_title_missing_count": import_readiness.get("title_missing_count", 0),
            "batch_config_context_status": batch_validation.get("config_context_status", ""),
            "batch_config_matched_job_count": batch_validation.get("config_matched_job_count", 0),
            "batch_config_mismatch_job_count": batch_validation.get("config_mismatch_job_count", 0),
            "batch_config_unknown_job_count": batch_validation.get("config_unknown_job_count", 0),
            "latest_batch_status": batch_validation.get("latest_status", ""),
            "latest_batch_report_endpoint": batch_validation.get("latest_report_endpoint", ""),
            "latest_batch_source_report_endpoint": batch_validation.get("latest_source_report_endpoint", ""),
            "source_field_map_report_count": len(field_map_report_artifacts),
            **gate_summary,
        },
        "readiness": readiness,
        "query_plan": query_plan,
        "tuning": tuning,
        "config_bundle": bundle_summary,
        "batch_validation": batch_validation,
        "import_readiness": import_readiness,
        "acceptance_gates": acceptance_gates,
        "recommendations": recommendations,
    }


def retrieval_onboarding_report_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    rows = [
        {
            "section": "overview",
            "name": "onboarding",
            "status": str(report.get("status") or ""),
            "configured": str(summary.get("configured_internal_count", "")),
            "previewed": str(summary.get("previewed_internal_count", "")),
            "sample_count": str((report.get("readiness") or {}).get("summary", {}).get("sample_count", "")),
            "score": "",
            "action": str(report.get("message") or ""),
        }
    ]
    validation_queries = report.get("validation_queries") if isinstance(report.get("validation_queries"), list) else []
    rows.append(
        {
            "section": "validation_query_source",
            "name": str(report.get("validation_query_source") or summary.get("validation_query_source") or ""),
            "status": "",
            "configured": "",
            "previewed": "",
            "sample_count": str(len(validation_queries)),
            "score": "",
            "action": "Batch validation required query source.",
        }
    )
    for validation_query in validation_queries:
        rows.append(
            {
                "section": "validation_query",
                "name": str(validation_query or ""),
                "status": "",
                "configured": "",
                "previewed": "",
                "sample_count": "",
                "score": "",
                "action": "Must be covered by a completed batch item.",
            }
        )
    for gate in report.get("acceptance_gates") or []:
        if not isinstance(gate, dict):
            continue
        rows.append(
            {
                "section": "acceptance_gate",
                "name": str(gate.get("name") or ""),
                "status": str(gate.get("status") or ""),
                "configured": str(bool(gate.get("required", True))).lower(),
                "previewed": "",
                "sample_count": str(gate.get("evidence") or ""),
                "score": "",
                "action": retrieval_onboarding_gate_action_text(gate),
            }
        )
    for row in retrieval_readiness_report_rows(report.get("readiness") if isinstance(report.get("readiness"), dict) else {}):
        rows.append(
            {
                "section": "readiness",
                "name": row["label"] or row["name"],
                "status": row["status"],
                "configured": row["configured"],
                "previewed": row["previewed"],
                "sample_count": row["sample_count"],
                "score": row["score"],
                "action": row["recommendations"] or row["message"],
            }
        )
    query_plan = report.get("query_plan") if isinstance(report.get("query_plan"), dict) else {}
    if query_plan:
        rows.append(
            {
                "section": "query_plan",
                "name": "plan",
                "status": str(query_plan.get("status") or ""),
                "configured": "",
                "previewed": "",
                "sample_count": str(query_plan.get("query_count") or 0),
                "score": str(query_plan.get("sample_size") or ""),
                "action": str(query_plan.get("message") or ""),
            }
        )
        for query_item in query_plan.get("queries") or []:
            if not isinstance(query_item, dict):
                continue
            rows.append(
                {
                    "section": "query_plan_query",
                    "name": str(query_item.get("query") or ""),
                    "status": str(query_item.get("reason") or ""),
                    "configured": ", ".join(str(item) for item in query_item.get("sources") or []),
                    "previewed": "",
                    "sample_count": str(query_item.get("sample_count") or 0),
                    "score": str(query_item.get("source_count") or 0),
                    "action": "Use this query in batch validation.",
                }
            )
    for row in retrieval_tuning_report_rows(report.get("tuning") if isinstance(report.get("tuning"), dict) else {}):
        rows.append(
            {
                "section": "tuning",
                "name": row["label"] or row["source"],
                "status": row["level"],
                "configured": row["configured"],
                "previewed": "",
                "sample_count": row["run_count"],
                "score": row["success_rate"],
                "action": row["action"],
            }
        )
    import_readiness = report.get("import_readiness") if isinstance(report.get("import_readiness"), dict) else {}
    if import_readiness:
        rows.append(
            {
                "section": "import_readiness",
                "name": "batch_candidates",
                "status": str(import_readiness.get("status") or ""),
                "configured": str(bool(import_readiness.get("checked_candidate_count"))).lower(),
                "previewed": str(import_readiness.get("run_count") or 0),
                "sample_count": str(import_readiness.get("checked_candidate_count") or 0),
                "score": str(import_readiness.get("ready_candidate_count") or 0),
                "action": str(import_readiness.get("message") or ""),
            }
        )
        for candidate in import_readiness.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            rows.append(
                {
                    "section": "import_candidate",
                    "name": str(candidate.get("candidate_id") or candidate.get("title") or ""),
                    "status": str(candidate.get("status") or ""),
                    "configured": str(candidate.get("source") or ""),
                    "previewed": str(candidate.get("item_type") or ""),
                    "sample_count": "",
                    "score": "",
                    "action": str(candidate.get("message") or ""),
                }
            )
    config_bundle = report.get("config_bundle") if isinstance(report.get("config_bundle"), dict) else {}
    for source in config_bundle.get("sources") or []:
        if not isinstance(source, dict):
            continue
        rows.append(
            {
                "section": "config_bundle",
                "name": str(source.get("label") or source.get("name") or ""),
                "status": "redacted" if source.get("redacted") else "ready",
                "configured": str(bool(source.get("configured"))).lower(),
                "previewed": "",
                "sample_count": "",
                "score": "",
                "action": "contains redacted placeholders" if source.get("redacted") else "safe to export as redacted bundle",
            }
        )
    batch_validation = report.get("batch_validation") if isinstance(report.get("batch_validation"), dict) else {}
    if batch_validation:
        rows.append(
            {
                "section": "batch_validation",
                "name": "recent_batch_validation",
                "status": str(batch_validation.get("status") or ""),
                "configured": str(bool(batch_validation.get("job_count"))).lower(),
                "previewed": "",
                "sample_count": (
                    f"{batch_validation.get('validated_source_count') or 0}/"
                    f"{batch_validation.get('required_source_count') or 0}"
                ),
                "score": str(batch_validation.get("missing_source_count") or 0),
                "action": str(batch_validation.get("message") or ""),
            }
        )
        rows.append(
            {
                "section": "batch_context",
                "name": "config_fingerprint",
                "status": str(batch_validation.get("config_context_status") or "unknown"),
                "configured": str(bool(batch_validation.get("current_config_fingerprint"))).lower(),
                "previewed": str(batch_validation.get("latest_job_id") or ""),
                "sample_count": (
                    f"matched={batch_validation.get('config_matched_job_count') or 0}; "
                    f"mismatch={batch_validation.get('config_mismatch_job_count') or 0}; "
                    f"unknown={batch_validation.get('config_unknown_job_count') or 0}"
                ),
                "score": str(batch_validation.get("latest_config_fingerprint") or ""),
                "action": (
                    "Rerun batch after source config changes."
                    if batch_validation.get("config_context_status") == "mismatch"
                    else "Latest batch config context."
                ),
            }
        )
    for job in batch_validation.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "")
        rows.append(
            {
                "section": "batch_validation",
                "name": job_id,
                "status": str(job.get("status") or ""),
                "configured": str(bool(job.get("sources"))).lower(),
                "previewed": "",
                "sample_count": str(job.get("completed_queries") or 0),
                "score": str(job.get("total_candidates") or 0),
                "action": str(job.get("report_endpoint") or ""),
            }
        )
        if job.get("source_report_endpoint"):
            rows.append(
                {
                    "section": "batch_evidence",
                    "name": f"{job_id}_source_csv" if job_id else "source_csv",
                    "status": str(job.get("status") or ""),
                    "configured": str(bool(job.get("sources"))).lower(),
                    "previewed": "",
                    "sample_count": str(job.get("completed_queries") or 0),
                    "score": str(job.get("total_candidates") or 0),
                    "action": str(job.get("source_report_endpoint") or ""),
                }
            )
    for source in batch_validation.get("source_evidence") or []:
        if not isinstance(source, dict):
            continue
        rows.append(
            {
                "section": "batch_source",
                "name": str(source.get("source") or ""),
                "status": "error" if safe_int(source.get("failure_count")) else "ok",
                "configured": str(bool(source.get("requested"))).lower(),
                "previewed": "",
                "sample_count": str(source.get("query_count") or 0),
                "score": str(source.get("candidate_count") or 0),
                "action": str(source.get("latest_error_kind") or source.get("latest_diagnostic") or ""),
            }
        )
    return rows


def render_retrieval_onboarding_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# 多源检索接入验收报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 验收状态：{report.get('status', '')}",
        f"- 结论：{report.get('message', '')}",
        f"- 预检 query：{report.get('query', '')}",
        f"- 样本数：{report.get('sample_size', '')}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| readiness 状态 | {summary.get('readiness_status', '')} |",
        f"| query plan 状态 | {summary.get('query_plan_status', '')} |",
        f"| query plan 草案数 | {summary.get('query_plan_query_count', 0)} |",
        f"| query plan AI 状态 | {summary.get('query_plan_ai_status', '') or '-'} |",
        f"| query plan AI 应用数 | {summary.get('query_plan_ai_applied_count', 0)} |",
        f"| 验收 query 来源 | {summary.get('validation_query_source', '') or '-'} |",
        f"| 验收 query 数 | {summary.get('validation_query_count', 0)} |",
        f"| tuning 状态 | {summary.get('tuning_status', '')} |",
        f"| 可用源 | {summary.get('available_source_count', 0)} |",
        f"| 已配置内部源 | {summary.get('configured_internal_count', 0)} |",
        f"| 已预览内部源 | {summary.get('previewed_internal_count', 0)} |",
        f"| 阻断项 | {summary.get('blocking_count', 0)} |",
        f"| 警告项 | {summary.get('warning_count', 0)} |",
        f"| 检索批次 | {summary.get('run_count', 0)} |",
        f"| 候选数 | {summary.get('candidate_count', 0)} |",
        f"| 批量任务 | {summary.get('batch_job_count', 0)} |",
        f"| 批量已完成 query | {summary.get('batch_completed_queries', 0)} |",
        f"| PLAN query 覆盖 | {summary.get('batch_covered_query_count', 0)}/{summary.get('batch_required_query_count', 0)} |",
        f"| PLAN query 缺口 | {summary.get('batch_missing_query_count', 0)} |",
        f"| 批量失败 query | {summary.get('batch_failed_queries', 0)} |",
        f"| 批量候选数 | {summary.get('batch_total_candidates', 0)} |",
        f"| 入库模型检查 | {summary.get('import_readiness_status', '') or '-'} |",
        f"| 可入库候选样本 | {summary.get('import_readiness_ready_candidate_count', 0)}/{summary.get('import_readiness_checked_candidate_count', 0)} |",
        f"| 入库转换错误 | {summary.get('import_readiness_error_candidate_count', 0)} |",
        f"| 最新批量任务状态 | {summary.get('latest_batch_status', '') or '-'} |",
        f"| 配置包已配置源 | {summary.get('config_bundle_configured_source_count', 0)} |",
        f"| 配置包脱敏源 | {summary.get('config_bundle_redacted_source_count', 0)} |",
        f"| acceptance gates | {summary.get('acceptance_gate_count', 0)} |",
        f"| gates passed | {summary.get('acceptance_gate_passed_count', 0)} |",
        f"| gates warning | {summary.get('acceptance_gate_warning_count', 0)} |",
        f"| gates needs sampling | {summary.get('acceptance_gate_needs_sampling_count', 0)} |",
        f"| gates blocked | {summary.get('acceptance_gate_blocked_count', 0)} |",
        "",
        "## Acceptance Gates",
        "",
        "| Gate | Status | Evidence | Action | Artifacts |",
        "| --- | --- | --- | --- | --- |",
        *[
            "| "
            + " | ".join(
                str(value).replace("|", "\\|")
                for value in [
                    gate.get("label") or gate.get("name") or "",
                    gate.get("status") or "",
                    gate.get("evidence") or "",
                    gate.get("action_label") or gate.get("message") or "",
                    "; ".join(
                        f"{artifact.get('label')}: {artifact.get('endpoint')}"
                        for artifact in gate.get("artifacts") or []
                        if isinstance(artifact, dict) and artifact.get("endpoint")
                    ),
                ]
            )
            + " |"
            for gate in report.get("acceptance_gates") or []
            if isinstance(gate, dict)
        ],
        "",
        "## 验收明细",
        "",
        "| 环节 | 名称 | 状态 | 配置 | 预览 | 样本/批次 | 分数/成功率 | 建议动作 |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for row in retrieval_onboarding_report_rows(report):
        cells = [
            row["section"],
            row["name"],
            row["status"],
            row["configured"],
            row["previewed"],
            row["sample_count"],
            row["score"],
            row["action"],
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines.extend(["", "## 下一步建议", ""])
    recommendations = [str(item) for item in report.get("recommendations") or [] if str(item or "").strip()]
    lines.extend(f"- {item}" for item in recommendations) if recommendations else lines.append("- 暂无。")
    lines.append("")
    return "\n".join(lines)


def render_retrieval_onboarding_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = ["section", "name", "status", "configured", "previewed", "sample_count", "score", "action"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(retrieval_onboarding_report_rows(report))
    return output.getvalue()


def render_retrieval_onboarding_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_retrieval_onboarding_report(report: dict[str, Any], fmt: str) -> str:
    normalized = normalize_retrieval_report_format(fmt)
    if normalized == "csv":
        return render_retrieval_onboarding_report_csv(report)
    if normalized == "json":
        return render_retrieval_onboarding_report_json(report)
    return render_retrieval_onboarding_report_markdown(report)


def retrieval_package_markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("\n", " ").replace("|", "\\|")


def render_retrieval_onboarding_package_readme(report: dict[str, Any], files: list[dict[str, Any]]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    gates = report.get("acceptance_gates") if isinstance(report.get("acceptance_gates"), list) else []
    lines = [
        "# Retrieval onboarding handoff package",
        "",
        "This ZIP contains the retrieval onboarding evidence needed for review, handoff, and replay.",
        "",
        "## Summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Status | {retrieval_package_markdown_cell(report.get('status'))} |",
        f"| Message | {retrieval_package_markdown_cell(report.get('message'))} |",
        f"| Query | {retrieval_package_markdown_cell(report.get('query'))} |",
        f"| Sample size | {retrieval_package_markdown_cell(report.get('sample_size'))} |",
        f"| Configured sources | {retrieval_package_markdown_cell(summary.get('configured_sources'))} |",
        f"| Ready sources | {retrieval_package_markdown_cell(summary.get('ready_sources'))} |",
        f"| Needs action | {retrieval_package_markdown_cell(summary.get('needs_action'))} |",
        "",
        "## Acceptance Gates",
        "",
        "| Gate | Status | Evidence | Action |",
        "| --- | --- | --- | --- |",
    ]
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        action = gate.get("action_label") or gate.get("message") or gate.get("action_endpoint")
        lines.append(
            "| "
            + " | ".join(
                [
                    retrieval_package_markdown_cell(gate.get("label") or gate.get("id")),
                    retrieval_package_markdown_cell(gate.get("status")),
                    retrieval_package_markdown_cell(gate.get("evidence")),
                    retrieval_package_markdown_cell(action),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Files",
            "",
            "| Path | Kind | Bytes | SHA256 |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for file_entry in files:
        lines.append(
            "| "
            + " | ".join(
                [
                    retrieval_package_markdown_cell(file_entry.get("path")),
                    retrieval_package_markdown_cell(file_entry.get("kind")),
                    retrieval_package_markdown_cell(file_entry.get("bytes")),
                    retrieval_package_markdown_cell(file_entry.get("sha256")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `manifest.json` records package metadata, acceptance gates, and checksums for all payload files.",
            "- `field-map/` contains sampled field_map reports for configured internal sources.",
            "- The config bundle is redacted by default; fill secrets through environment variables in the target setup.",
            "- Use the SHA256 values to verify package contents after transfer.",
            "",
        ]
    )
    return "\n".join(lines)


def retrieval_onboarding_package_for_library(
    library_id: str,
    *,
    query: str,
    sample_size: int,
    include_health: bool,
    limit: int,
    use_ai: bool = False,
    required_queries: Any = None,
) -> bytes:
    report = retrieval_onboarding_report_for_library(
        library_id,
        query=query,
        sample_size=sample_size,
        include_health=include_health,
        limit=limit,
        use_ai=use_ai,
        required_queries=required_queries,
    )
    query_plan = report.get("query_plan") if isinstance(report.get("query_plan"), dict) else {}
    if not query_plan:
        query_plan = retrieval_query_plan_for_library(
            library_id,
            seed_query=query,
            sample_size=sample_size,
            limit=5,
            use_ai=use_ai,
        )
    source_setup = retrieval_source_setup_report(
        retrieval_source_statuses(
            registry=retrieval_provider_registry_for_library(library_id),
            include_health=include_health,
        ),
        include_health=include_health,
    )
    files: list[dict[str, Any]] = []
    buffer = io.BytesIO()

    def add_text(zip_file: zipfile.ZipFile, path: str, content: str, kind: str) -> None:
        encoded = content.encode("utf-8")
        zip_file.writestr(path, encoded)
        files.append(
            {
                "path": path,
                "kind": kind,
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        add_text(
            zip_file,
            "onboarding/retrieval-onboarding-report.md",
            render_retrieval_onboarding_report(report, "markdown"),
            "onboarding_markdown",
        )
        add_text(
            zip_file,
            "onboarding/retrieval-onboarding-report.csv",
            render_retrieval_onboarding_report(report, "csv"),
            "onboarding_csv",
        )
        add_text(
            zip_file,
            "onboarding/retrieval-onboarding-report.json",
            render_retrieval_onboarding_report(report, "json"),
            "onboarding_json",
        )
        add_text(
            zip_file,
            "query-plan/retrieval-query-plan.md",
            render_retrieval_query_plan_report(query_plan, "markdown"),
            "query_plan_markdown",
        )
        add_text(
            zip_file,
            "query-plan/retrieval-query-plan.csv",
            render_retrieval_query_plan_report(query_plan, "csv"),
            "query_plan_csv",
        )
        add_text(
            zip_file,
            "query-plan/retrieval-query-plan.json",
            render_retrieval_query_plan_report(query_plan, "json"),
            "query_plan_json",
        )
        add_text(
            zip_file,
            "source-setup/retrieval-source-setup-report.md",
            render_retrieval_source_setup_report(source_setup, "markdown"),
            "source_setup_markdown",
        )
        add_text(
            zip_file,
            "source-setup/retrieval-source-setup-report.csv",
            render_retrieval_source_setup_report(source_setup, "csv"),
            "source_setup_csv",
        )
        add_text(
            zip_file,
            "source-setup/retrieval-source-setup-report.json",
            render_retrieval_source_setup_report(source_setup, "json"),
            "source_setup_json",
        )
        readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
        add_text(
            zip_file,
            "readiness/retrieval-readiness-report.md",
            render_retrieval_readiness_report(readiness, "markdown"),
            "readiness_markdown",
        )
        add_text(
            zip_file,
            "readiness/retrieval-readiness-report.csv",
            render_retrieval_readiness_report(readiness, "csv"),
            "readiness_csv",
        )
        add_text(
            zip_file,
            "readiness/retrieval-readiness-report.json",
            render_retrieval_readiness_report(readiness, "json"),
            "readiness_json",
        )
        field_map_report_entries = retrieval_readiness_field_map_report_entries(
            readiness, query=query, sample_size=sample_size
        )
        for entry in field_map_report_entries:
            slug = str(entry.get("slug") or entry.get("source") or "source")
            source = str(entry.get("source") or slug)
            field_map_report = entry.get("report") if isinstance(entry.get("report"), dict) else {}
            if not field_map_report:
                continue
            add_text(
                zip_file,
                f"field-map/{slug}/{retrieval_source_field_map_report_filename(slug, 'markdown')}",
                render_retrieval_field_map_report(field_map_report, "markdown"),
                f"field_map_{source}_markdown",
            )
            add_text(
                zip_file,
                f"field-map/{slug}/{retrieval_source_field_map_report_filename(slug, 'csv')}",
                render_retrieval_field_map_report(field_map_report, "csv"),
                f"field_map_{source}_csv",
            )
            add_text(
                zip_file,
                f"field-map/{slug}/{retrieval_source_field_map_report_filename(slug, 'json')}",
                render_retrieval_field_map_report(field_map_report, "json"),
                f"field_map_{source}_json",
            )
        tuning = report.get("tuning") if isinstance(report.get("tuning"), dict) else {}
        add_text(
            zip_file,
            "tuning/retrieval-tuning-report.md",
            render_retrieval_tuning_report(tuning, "markdown"),
            "tuning_markdown",
        )
        add_text(
            zip_file,
            "tuning/retrieval-tuning-report.csv",
            render_retrieval_tuning_report(tuning, "csv"),
            "tuning_csv",
        )
        add_text(
            zip_file,
            "tuning/retrieval-tuning-report.json",
            render_retrieval_tuning_report(tuning, "json"),
            "tuning_json",
        )
        add_text(
            zip_file,
            "config/retrieval-config-bundle.json",
            json.dumps(retrieval_config_bundle_for_library(library_id, redact=True), ensure_ascii=False, indent=2),
            "config_bundle_json",
        )
        batch_validation = report.get("batch_validation") if isinstance(report.get("batch_validation"), dict) else {}
        latest_job_id = str(batch_validation.get("latest_job_id") or "").strip()
        if latest_job_id:
            try:
                batch_report = retrieval_batch_report(app_store.retrieval_batch_job(library_id, latest_job_id))
            except ValueError:
                batch_report = {}
            if batch_report:
                add_text(
                    zip_file,
                    f"batch/{retrieval_batch_report_filename(latest_job_id, 'markdown')}",
                    render_retrieval_batch_report(batch_report, "markdown"),
                    "batch_markdown",
                )
                add_text(
                    zip_file,
                    f"batch/{retrieval_batch_report_filename(latest_job_id, 'csv')}",
                    render_retrieval_batch_report(batch_report, "csv"),
                    "batch_csv",
                )
                add_text(
                    zip_file,
                    f"batch/{retrieval_batch_report_filename(latest_job_id, 'csv', 'sources')}",
                    render_retrieval_batch_report(batch_report, "csv", "sources"),
                    "batch_sources_csv",
                )
                add_text(
                    zip_file,
                    f"batch/{retrieval_batch_report_filename(latest_job_id, 'json')}",
                    render_retrieval_batch_report(batch_report, "json"),
                    "batch_json",
                )
        add_text(
            zip_file,
            "README.md",
            render_retrieval_onboarding_package_readme(report, files),
            "package_readme",
        )
        manifest = {
            "schema": "web-library.retrieval-onboarding-package/v1",
            "generated_at": now_iso(),
            "library_id": library_id,
            "query": query,
            "sample_size": sample_size,
            "include_health": include_health,
            "limit": limit,
            "status": report.get("status"),
            "message": report.get("message"),
            "summary": report.get("summary"),
            "query_plan": {
                "status": query_plan.get("status"),
                "query_count": query_plan.get("query_count"),
                "seed_query": query_plan.get("seed_query"),
                "query_text": query_plan.get("query_text"),
            },
            "source_setup": {
                "source_count": source_setup.get("source_count"),
                "available_count": source_setup.get("available_count"),
                "configured_count": source_setup.get("configured_count"),
                "include_health": source_setup.get("include_health"),
            },
            "field_map_reports": [
                {
                    "source": entry.get("source"),
                    "label": entry.get("label"),
                    "path_prefix": f"field-map/{entry.get('slug')}",
                }
                for entry in field_map_report_entries
            ],
            "acceptance_gates": report.get("acceptance_gates") or [],
            "files": files,
        }
        add_text(zip_file, "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2), "manifest_json")
    return buffer.getvalue()


def normalize_similarity_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value or "").casefold())).strip()


def creator_last_name(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("last_name") or value.get("lastName"):
            return normalize_similarity_text(str(value.get("last_name") or value.get("lastName")))
        name = str(value.get("name") or "").strip()
    else:
        name = str(value or "").strip()
    if not name:
        return ""
    return normalize_similarity_text(name.split(",")[0] if "," in name else name.split()[-1])


def candidate_first_author(candidate: dict[str, Any]) -> str:
    payload = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
    creators = candidate.get("creators") or payload.get("creators") or []
    if not isinstance(creators, list) or not creators:
        return ""
    return creator_last_name(creators[0])


def candidate_year(candidate: dict[str, Any]) -> str:
    payload = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    value = str(candidate.get("year") or fields.get("date") or "")
    match = re.search(r"\b\d{4}\b", value)
    return match.group(0) if match else ""


def weak_similarity_matches(candidate: dict[str, Any], existing_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    candidate_title = normalize_similarity_text(str(candidate.get("title") or fields.get("title") or ""))
    if len(candidate_title) < 12:
        return []
    year = candidate_year(candidate)
    author = candidate_first_author(candidate)
    matches: list[dict[str, Any]] = []
    for item in existing_items:
        item_title = normalize_similarity_text(str(item.get("title") or ""))
        if len(item_title) < 12:
            continue
        item_year = str(item.get("year") or "").strip()
        if year and item_year and year != item_year:
            continue
        score = SequenceMatcher(None, candidate_title, item_title).ratio()
        item_author = creator_last_name((item.get("creator_names") or [""])[0])
        author_matches = bool(author and item_author and author == item_author)
        if score < (0.92 if author_matches else 0.97):
            continue
        matches.append(
            {
                "key": item.get("key", ""),
                "title": item.get("title", ""),
                "year": item_year,
                "first_author": item_author,
                "score": round(score, 3),
                "reason": "标题、年份和第一作者高度相似" if author_matches else "标题和年份高度相似",
            }
        )
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:3]


def _rag_chat_sources(evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for result in evidence_pack.get("results") or []:
        if not isinstance(result, dict):
            continue
        sources.append(
            {
                "evidence_id": result.get("evidence_id", ""),
                "source_type": result.get("source_type", ""),
                "item_key": result.get("item_key", ""),
                "attachment_key": result.get("attachment_key", ""),
                "doc_id": result.get("doc_id", ""),
                "chunk_id": result.get("chunk_id", ""),
                "title": result.get("title", ""),
                "section_title": result.get("section_title", ""),
                "estimated_page": result.get("estimated_page"),
                "excerpt": result.get("excerpt", ""),
                "citation": result.get("citation", ""),
                "rank": result.get("rank"),
            }
        )
    return sources


# ---------------------------------------------------------------------------
# 单篇文献研读对话存储：聊天记录 + 线程状态 + 图片附件，落盘到 app-data。
# ---------------------------------------------------------------------------

def _reading_chat_dir(library_id: str, item_key: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_]", "_", str(library_id or "library"))
    safe_item = re.sub(r"[^A-Za-z0-9_]", "_", str(item_key or "item"))
    path = app_data_dir() / "libraries" / safe_id / "reading-chat" / safe_item
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reading_chat_messages_path(library_id: str, item_key: str) -> Path:
    return _reading_chat_dir(library_id, item_key) / "messages.json"


def _reading_chat_state_path(library_id: str, item_key: str) -> Path:
    return _reading_chat_dir(library_id, item_key) / "state.json"


def _reading_chat_tasks_path(library_id: str, item_key: str) -> Path:
    return _reading_chat_dir(library_id, item_key) / "tasks.json"


def _reading_chat_assets_dir(library_id: str, item_key: str) -> Path:
    path = _reading_chat_dir(library_id, item_key) / "assets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_reading_chat_messages(library_id: str, item_key: str) -> list[dict[str, Any]]:
    return _read_json_file(_reading_chat_messages_path(library_id, item_key), []) or []


def save_reading_chat_messages(library_id: str, item_key: str, messages: list[dict[str, Any]]) -> None:
    _write_json_file(_reading_chat_messages_path(library_id, item_key), messages)


def append_reading_chat_message(library_id: str, item_key: str, message: dict[str, Any]) -> None:
    messages = load_reading_chat_messages(library_id, item_key)
    messages.append(message)
    save_reading_chat_messages(library_id, item_key, messages)


def load_reading_chat_state(library_id: str, item_key: str) -> dict[str, Any]:
    return _read_json_file(_reading_chat_state_path(library_id, item_key), {}) or {}


def save_reading_chat_state(library_id: str, item_key: str, state: dict[str, Any]) -> None:
    _write_json_file(_reading_chat_state_path(library_id, item_key), state)


def load_reading_chat_tasks(library_id: str, item_key: str) -> list[dict[str, Any]]:
    return _read_json_file(_reading_chat_tasks_path(library_id, item_key), []) or []


def save_reading_chat_tasks(library_id: str, item_key: str, tasks: list[dict[str, Any]]) -> None:
    _write_json_file(_reading_chat_tasks_path(library_id, item_key), tasks[-READING_CHAT_HISTORY_LIMIT:])


def upsert_reading_chat_task(library_id: str, item_key: str, task: dict[str, Any]) -> None:
    tasks = load_reading_chat_tasks(library_id, item_key)
    run_id = task.get("run_id")
    for index, existing in enumerate(tasks):
        if existing.get("run_id") == run_id:
            tasks[index] = {**existing, **task}
            save_reading_chat_tasks(library_id, item_key, tasks)
            return
    tasks.append(task)
    save_reading_chat_tasks(library_id, item_key, tasks)


def append_reading_chat_task_event(library_id: str, item_key: str, run_id: str, message: str, kind: str = "info") -> None:
    tasks = load_reading_chat_tasks(library_id, item_key)
    for task in tasks:
        if task.get("run_id") == run_id:
            events = task.get("events") or []
            events.append({"message": message, "kind": kind, "created_at": now_iso()})
            task["events"] = events[-20:]
            break
    save_reading_chat_tasks(library_id, item_key, tasks)


def reading_chat_task_is_running(library_id: str, item_key: str, run_id: str) -> bool:
    for task in load_reading_chat_tasks(library_id, item_key):
        if task.get("run_id") == run_id and task.get("status") == "running":
            return True
    return False


def latest_reading_chat_task(library_id: str, item_key: str) -> dict[str, Any] | None:
    tasks = load_reading_chat_tasks(library_id, item_key)
    return tasks[-1] if tasks else None


def serialize_reading_chat_messages(library_id: str, item_key: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assets_dir = _reading_chat_assets_dir(library_id, item_key)
    serialized: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        attachments = []
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            copied = dict(attachment)
            filename = str(attachment.get("filename") or "").strip()
            if filename:
                copied["url"] = f"/api/library/{library_id}/items/{item_key}/reading-chat/asset/{filename}"
            attachments.append(copied)
        item["attachments"] = attachments
        serialized.append(item)
    return serialized


def save_reading_chat_uploads(library_id: str, item_key: str, run_id: str, uploads: list[Any]) -> list[dict[str, Any]]:
    allowed = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    target_dir = _reading_chat_assets_dir(library_id, item_key)
    attachments: list[dict[str, Any]] = []
    for index, upload in enumerate(uploads[:6], start=1):
        if not upload or not getattr(upload, "filename", ""):
            continue
        content_type = str(getattr(upload, "content_type", "") or "").lower().split(";", 1)[0]
        ext = allowed.get(content_type)
        if not ext:
            filename_ext = Path(str(upload.filename)).suffix.lower().lstrip(".")
            ext = filename_ext if filename_ext in {"png", "jpg", "jpeg", "webp", "gif"} else ""
        if not ext:
            continue
        if ext == "jpeg":
            ext = "jpg"
        filename = f"{re.sub(r'[^A-Za-z0-9_]', '_', str(run_id))}-{index:02d}.{ext}"
        target = target_dir / filename
        upload.save(target)
        attachments.append({"type": "image", "filename": filename, "size": target.stat().st_size})
    return attachments


def execute_reading_chat_task(
    library_id: str,
    item_key: str,
    run_id: str,
    user_question: str,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    try:
        library = app_store.get_library(library_id)
        if not library:
            raise RuntimeError("文库不存在。")
        repo = ZoteroRepository(library)
        item = next((value for value in repo.items() if value.get("key") == item_key), None)
        if not item:
            raise RuntimeError("当前文献条目不存在。")
        pdf_path = ""
        for attachment in item.get("attachments") or []:
            if attachment.get("kind") == "pdf" and attachment.get("openable") and attachment.get("resolved_path"):
                candidate = Path(attachment["resolved_path"])
                if candidate.exists():
                    pdf_path = str(candidate)
                    break

        state = load_reading_chat_state(library_id, item_key)
        thread_id = state.get("thread_id")
        include_paper_context = not thread_id
        if include_paper_context:
            append_reading_chat_task_event(library_id, item_key, run_id, "正在注入当前文献上下文。")
        else:
            append_reading_chat_task_event(library_id, item_key, run_id, "沿用当前文献研读线程中的上下文。")

        codex_config = api_config_codex_for_library(library_id)
        assets_dir = _reading_chat_assets_dir(library_id, item_key)
        image_paths = [
            str(assets_dir / attachment["filename"])
            for attachment in (attachments or [])
            if attachment.get("type") == "image" and attachment.get("filename")
        ]

        result = run_reading_chat_turn(
            library=library,
            codex_config=codex_config,
            item=item,
            pdf_path=pdf_path,
            thread_id=thread_id,
            user_question=user_question,
            include_paper_context=include_paper_context,
            image_paths=image_paths,
            progress=lambda message: append_reading_chat_task_event(library_id, item_key, run_id, message),
        )
        if not reading_chat_task_is_running(library_id, item_key, run_id):
            append_reading_chat_task_event(library_id, item_key, run_id, "用户已停止本次研读问答，丢弃迟到回复。")
            return
        thread_id = result.get("thread_id") or thread_id
        save_reading_chat_state(
            library_id,
            item_key,
            {
                "thread_id": thread_id,
                "created_at": state.get("created_at") or now_iso(),
                "updated_at": now_iso(),
                "item_key": item_key,
            },
        )
        append_reading_chat_message(
            library_id,
            item_key,
            {
                "role": "assistant",
                "content": result.get("assistant_message") or "文献研读问答已完成，但没有返回内容。",
                "created_at": now_iso(),
                "run_id": run_id,
                "thread_id": thread_id,
                "item_key": item_key,
                "usage": result.get("usage"),
            },
        )
        upsert_reading_chat_task(
            library_id,
            item_key,
            {
                "run_id": run_id,
                "item_key": item_key,
                "status": "success",
                "finished_at": now_iso(),
                "thread_id": thread_id,
            },
        )
    except Exception as exc:
        append_reading_chat_message(
            library_id,
            item_key,
            {
                "role": "assistant",
                "content": f"文献研读问答失败：{exc}",
                "created_at": now_iso(),
                "run_id": run_id,
                "item_key": item_key,
                "error": True,
                "attachments": attachments or [],
            },
        )
        upsert_reading_chat_task(
            library_id,
            item_key,
            {
                "run_id": run_id,
                "item_key": item_key,
                "status": "failed",
                "finished_at": now_iso(),
                "error": str(exc),
            },
        )
    finally:
        with READING_CHAT_LOCK:
            task_key = f"{library_id}:{item_key}"
            if READING_CHAT_TASKS.get(task_key, {}).get("run_id") == run_id:
                READING_CHAT_TASKS.pop(task_key, None)


# ---- 单篇文献矩阵（字段管理 + 运行 + 进度 + 持久化） ----
MATRIX_LOCK = threading.Lock()
MATRIX_TASKS: dict[str, dict[str, Any]] = {}
MATRIX_HISTORY_LIMIT = 30


def _matrix_dir(library_id: str, knowledge_base_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_]", "_", str(library_id or "library"))
    safe_kb = re.sub(r"[^A-Za-z0-9_]", "_", str(knowledge_base_id or "kb"))
    return app_data_dir() / "libraries" / safe_id / "matrix" / safe_kb


def _matrix_fields_path(library_id: str, knowledge_base_id: str) -> Path:
    return _matrix_dir(library_id, knowledge_base_id) / "fields.json"


def _matrix_item_path(library_id: str, knowledge_base_id: str, item_key: str) -> Path:
    safe_item = re.sub(r"[^A-Za-z0-9_]", "_", str(item_key or "item"))
    return _matrix_dir(library_id, knowledge_base_id) / "items" / f"{safe_item}.json"


def _matrix_tasks_path(library_id: str, knowledge_base_id: str) -> Path:
    return _matrix_dir(library_id, knowledge_base_id) / "tasks.json"


def default_matrix_fields() -> list[dict[str, Any]]:
    return [
        {"field_id": "research_background", "name": "研究背景", "rule": "概述论文所解决的问题背景与研究动机，2-4 句中文。", "enabled": True},
        {"field_id": "method_design", "name": "实验设计", "rule": "概括方法或实验设计的核心思路与关键设置。", "enabled": True},
        {"field_id": "key_findings", "name": "关键结论", "rule": "列出 2-4 条最核心的实验结论或发现。", "enabled": True},
        {"field_id": "contributions", "name": "创新点", "rule": "说明论文相对已有工作的主要贡献或创新之处。", "enabled": True},
    ]


def _slugify_field(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", "_", str(name or "")).strip("_").lower()
    return slug or "field"


def normalize_matrix_fields(raw_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    normalized: list[dict[str, Any]] = []
    for entry in raw_fields or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        field_id = str(entry.get("field_id") or "").strip() or _slugify_field(name)
        if field_id in seen:
            seen[field_id] += 1
            field_id = f"{field_id}-{seen[field_id]}"
        else:
            seen[field_id] = 1
        normalized.append(
            {
                "field_id": field_id,
                "name": name,
                "rule": str(entry.get("rule") or "").strip(),
                "enabled": bool(entry.get("enabled", True)),
            }
        )
    if not normalized:
        normalized = default_matrix_fields()
    return normalized


def load_matrix_fields(library_id: str, knowledge_base_id: str) -> list[dict[str, Any]]:
    data = _read_json_file(_matrix_fields_path(library_id, knowledge_base_id), None)
    if not isinstance(data, list) or not data:
        return default_matrix_fields()
    return data


def save_matrix_fields(library_id: str, knowledge_base_id: str, fields: list[dict[str, Any]]) -> None:
    _write_json_file(_matrix_fields_path(library_id, knowledge_base_id), fields)


def load_matrix_item_values(library_id: str, knowledge_base_id: str, item_key: str) -> dict[str, Any]:
    data = _read_json_file(_matrix_item_path(library_id, knowledge_base_id, item_key), {}) or {}
    return data.get("values", {}) if isinstance(data, dict) else {}


def save_matrix_item_values(library_id: str, knowledge_base_id: str, item_key: str, values: dict[str, Any]) -> None:
    _write_json_file(
        _matrix_item_path(library_id, knowledge_base_id, item_key),
        {"item_key": item_key, "values": values, "updated_at": now_iso()},
    )


def load_matrix_tasks(library_id: str, knowledge_base_id: str) -> list[dict[str, Any]]:
    return _read_json_file(_matrix_tasks_path(library_id, knowledge_base_id), []) or []


def save_matrix_tasks(library_id: str, knowledge_base_id: str, tasks: list[dict[str, Any]]) -> None:
    _write_json_file(_matrix_tasks_path(library_id, knowledge_base_id), tasks[-MATRIX_HISTORY_LIMIT:])


def upsert_matrix_task(library_id: str, knowledge_base_id: str, task: dict[str, Any]) -> None:
    tasks = load_matrix_tasks(library_id, knowledge_base_id)
    replaced = False
    for index, existing in enumerate(tasks):
        if isinstance(existing, dict) and existing.get("run_id") == task.get("run_id"):
            tasks[index] = {**existing, **task}
            replaced = True
            break
    if not replaced:
        tasks.append(task)
    save_matrix_tasks(library_id, knowledge_base_id, tasks)


def append_matrix_task_event(library_id: str, knowledge_base_id: str, run_id: str, message: str, kind: str = "info") -> None:
    tasks = load_matrix_tasks(library_id, knowledge_base_id)
    for task in tasks:
        if isinstance(task, dict) and task.get("run_id") == run_id:
            events = task.setdefault("events", [])
            events.append({"kind": kind, "message": message, "created_at": now_iso()})
            task["events"] = events[-40:]
            save_matrix_tasks(library_id, knowledge_base_id, tasks)
            return


def matrix_task_is_running(library_id: str, knowledge_base_id: str, run_id: str) -> bool:
    for task in load_matrix_tasks(library_id, knowledge_base_id):
        if isinstance(task, dict) and task.get("run_id") == run_id:
            return task.get("status") == "running"
    return False


def latest_matrix_task(library_id: str, knowledge_base_id: str) -> dict[str, Any] | None:
    tasks = load_matrix_tasks(library_id, knowledge_base_id)
    return tasks[-1] if tasks else None


def _first_pdf_path(item: dict[str, Any]) -> str:
    for attachment in item.get("attachments") or []:
        if attachment.get("kind") == "pdf" and attachment.get("openable") and attachment.get("resolved_path"):
            candidate = Path(attachment["resolved_path"])
            if candidate.exists():
                return str(candidate)
    return ""


def execute_matrix_task(
    library_id: str,
    knowledge_base_id: str,
    run_id: str,
    item_keys: list[str],
    mode: str,
) -> None:
    try:
        library = app_store.get_library(library_id)
        if not library:
            raise RuntimeError("文库不存在。")
        repo = ZoteroRepository(library)
        items = {value["key"]: value for value in repo.items() if isinstance(value, dict) and value.get("key")}
        fields = [field for field in load_matrix_fields(library_id, knowledge_base_id) if field.get("enabled", True)]
        if not fields:
            raise RuntimeError("当前没有启用任何文献矩阵字段，请先新增或 AI 推荐字段。")
        codex_config = api_config_codex_for_library(library_id)

        total = len(item_keys)
        task = {
            "run_id": run_id,
            "status": "running",
            "knowledge_base_id": knowledge_base_id,
            "selected_item_keys": item_keys,
            "mode": mode,
            "total": total,
            "completed": 0,
            "failed": 0,
            "skipped_no_pdf": 0,
            "skipped_existing": 0,
            "current_item_key": "",
            "current_title": "",
            "started_at": now_iso(),
            "finished_at": "",
            "events": [],
        }
        upsert_matrix_task(library_id, knowledge_base_id, task)

        completed = failed = skipped_no_pdf = skipped_existing = 0
        for index, item_key in enumerate(item_keys, start=1):
            if not matrix_task_is_running(library_id, knowledge_base_id, run_id):
                append_matrix_task_event(library_id, knowledge_base_id, run_id, "用户已停止文献矩阵任务。")
                break
            item = items.get(item_key)
            if not item:
                append_matrix_task_event(library_id, knowledge_base_id, run_id, f"条目 {item_key} 不存在，已跳过。", "warning")
                failed += 1
                task.update(completed=completed, failed=failed, skipped_no_pdf=skipped_no_pdf, skipped_existing=skipped_existing, current_item_key="", current_title="")
                upsert_matrix_task(library_id, knowledge_base_id, task)
                continue
            title = str(item.get("title") or item_key)
            pdf_path = _first_pdf_path(item)
            task.update(current_item_key=item_key, current_title=title)
            upsert_matrix_task(library_id, knowledge_base_id, task)
            if not pdf_path:
                append_matrix_task_event(library_id, knowledge_base_id, run_id, f"[{index}/{total}] {title}：未找到本地 PDF，已跳过。", "warning")
                skipped_no_pdf += 1
                task.update(completed=completed, failed=failed, skipped_no_pdf=skipped_no_pdf, skipped_existing=skipped_existing)
                upsert_matrix_task(library_id, knowledge_base_id, task)
                continue
            existing = load_matrix_item_values(library_id, knowledge_base_id, item_key)
            if mode == "skip_existing" and any(str(v.get("value") or "").strip() for v in existing.values() if isinstance(v, dict)):
                append_matrix_task_event(library_id, knowledge_base_id, run_id, f"[{index}/{total}] {title}：已有结果，跳过。", "info")
                skipped_existing += 1
                task.update(completed=completed, failed=failed, skipped_no_pdf=skipped_no_pdf, skipped_existing=skipped_existing)
                upsert_matrix_task(library_id, knowledge_base_id, task)
                continue
            append_matrix_task_event(library_id, knowledge_base_id, run_id, f"[{index}/{total}] 正在处理：{title}", "info")
            try:
                result = run_reading_matrix_for_item(
                    library=library,
                    codex_config=codex_config,
                    item=item,
                    fields=fields,
                    pdf_path=pdf_path,
                    progress=lambda message: append_matrix_task_event(library_id, knowledge_base_id, run_id, message),
                )
                new_values = result.get("values", {})
                if mode == "overwrite_existing":
                    merged = {**existing, **new_values}
                else:
                    merged = {**new_values, **existing}
                save_matrix_item_values(library_id, knowledge_base_id, item_key, merged)
                completed += 1
                append_matrix_task_event(library_id, knowledge_base_id, run_id, f"[{index}/{total}] {title}：已完成。", "info")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                append_matrix_task_event(library_id, knowledge_base_id, run_id, f"[{index}/{total}] {title} 处理失败：{exc}", "error")
            task.update(completed=completed, failed=failed, skipped_no_pdf=skipped_no_pdf, skipped_existing=skipped_existing)
            upsert_matrix_task(library_id, knowledge_base_id, task)

        status = "failed" if failed and completed == 0 and skipped_no_pdf == 0 and skipped_existing == 0 else "success"
        upsert_matrix_task(
            library_id,
            knowledge_base_id,
            {**task, "status": status, "finished_at": now_iso()},
        )
    except Exception as exc:  # noqa: BLE001
        upsert_matrix_task(
            library_id,
            knowledge_base_id,
            {
                "run_id": run_id,
                "status": "failed",
                "finished_at": now_iso(),
                "error": str(exc),
                "events": [{"kind": "error", "message": str(exc), "created_at": now_iso()}],
            },
        )
    finally:
        with MATRIX_LOCK:
            MATRIX_TASKS.pop(f"{library_id}:{knowledge_base_id}", None)


def create_app() -> Flask:
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")
    static_dir = Path(__file__).resolve().parent / "static"
    app = Flask(__name__, template_folder="templates", static_folder=None)
    app.config.update(
        MAX_CONTENT_LENGTH=_env_int("WEB_LIBRARY_MAX_UPLOAD_BYTES", 8 * 1024 * 1024 * 1024),
        MAX_FORM_MEMORY_SIZE=_env_int("WEB_LIBRARY_MAX_FORM_MEMORY_BYTES", 64 * 1024 * 1024),
        MAX_FORM_PARTS=_env_int("WEB_LIBRARY_MAX_FORM_PARTS", 100_000),
    )
    app_store.ensure_app_store()

    @app.errorhandler(RequestEntityTooLarge)
    def request_entity_too_large(exc):
        return jsonify(
            {
                "ok": False,
                "error": (
                    "上传内容超过当前服务限制。默认支持最大 8GB、最多 100000 个文件；"
                    "如仍然触发，请改用 Docker 目录挂载方式，或设置 WEB_LIBRARY_MAX_UPLOAD_BYTES / WEB_LIBRARY_MAX_FORM_PARTS。"
                ),
            }
        ), 413

    @app.get("/static/<path:filename>", endpoint="static")
    def static_files(filename: str):
        mimetype = "application/javascript" if filename.endswith((".js", ".mjs")) else None
        return send_from_directory(static_dir, filename, mimetype=mimetype)

    @app.after_request
    def fix_static_javascript_mimetype(response):
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
            if request.path.endswith((".js", ".mjs")):
                response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        return response

    def library_or_404(library_id: str) -> dict[str, Any]:
        library = app_store.get_library(library_id)
        if not library:
            raise SourceError("文库不存在。")
        return library

    def annotate_existing_retrieval_matches(repo: ZoteroRepository, candidates: list[dict[str, Any]]) -> None:
        metadata_items = []
        positions = []
        for index, candidate in enumerate(candidates):
            try:
                metadata_items.append(imported_item_from_candidate(candidate))
                positions.append(index)
            except CandidateImportError:
                continue
        if not metadata_items:
            return
        for index, hint in zip(positions, repo.existing_match_hints(metadata_items)):
            matches = hint.get("matches") or []
            if not matches:
                continue
            candidate = candidates[index]
            status = "existing" if len(matches) == 1 else "conflict"
            candidate["existing_matches"] = matches
            candidate["duplicate_hint"] = {
                "status": status,
                "message": "文库已有匹配条目" if status == "existing" else "文库已有多个匹配条目",
                "matches": matches,
            }
            rank_reasons = candidate.setdefault("rank_reasons", [])
            if "文库已有匹配" not in rank_reasons:
                rank_reasons.insert(0, "文库已有匹配")
        existing_items = repo.items()
        for candidate in candidates:
            if candidate.get("duplicate_hint"):
                continue
            matches = weak_similarity_matches(candidate, existing_items)
            if not matches:
                continue
            candidate["weak_similarity_matches"] = matches
            candidate["similarity_hint"] = {
                "status": "similar",
                "message": "文库存在疑似相似条目",
                "matches": matches,
            }
            rank_reasons = candidate.setdefault("rank_reasons", [])
            if "文库疑似相似" not in rank_reasons:
                rank_reasons.insert(0, "文库疑似相似")

    def run_retrieval_search_for_library(
        library_id: str,
        query: str,
        sources: Any,
        limit: int,
        *,
        include_raw: bool = False,
        use_ai_evaluation: bool = True,
        source_limits: dict[str, Any] | None = None,
        search_options: SearchOptions | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        library = library_or_404(library_id)
        registry = retrieval_provider_registry_for_library(library_id)
        result = search_retrieval(
            query,
            sources=sources,
            limit=limit,
            source_limits=source_limits,
            options=search_options,
            include_raw=include_raw,
            registry=registry,
        )
        annotate_existing_retrieval_matches(ZoteroRepository(library), result["candidates"])
        result["ai_evaluation_summary"] = evaluate_retrieval_candidates_with_ai(
            library_id,
            result["query"],
            result["candidates"],
            use_ai_evaluation=use_ai_evaluation,
        )
        stored = app_store.create_retrieval_run(
            library_id,
            result["query"],
            result["sources"],
            result["source_stats"],
            result["candidates"],
        )
        result["run_id"] = stored["run_id"]
        result["candidates"] = stored["candidates"]
        return result

    def retrieval_batch_candidate_key(candidate: dict[str, Any]) -> str:
        item = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        identifiers = candidate.get("identifiers") if isinstance(candidate.get("identifiers"), dict) else {}
        item_identifiers = item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {}
        merged_identifiers = {**item_identifiers, **identifiers}
        for key in ("doi", "pmid", "pmcid", "arxiv", "isbn", "ads_bibcode"):
            value = str(merged_identifiers.get(key) or fields.get(key.upper()) or "").strip().casefold()
            if value:
                return f"{key}:{value}"
        url = str(candidate.get("landing_url") or candidate.get("url") or fields.get("url") or "").strip().casefold()
        if url:
            return f"url:{url}"
        external_id = str(candidate.get("external_id") or "").strip().casefold()
        source = str(candidate.get("source") or "").strip().casefold()
        if external_id and source:
            return f"external:{source}:{external_id}"
        title = re.sub(r"\s+", " ", candidate_field(candidate, "title").casefold()).strip()
        year = str(candidate.get("year") or fields.get("date") or fields.get("year") or "")[:4]
        if title:
            return f"title:{title}:{year}"
        return f"fallback:{source}:{candidate.get('rank') or ''}:{candidate.get('stored_candidate_id') or ''}"

    def retrieval_batch_source_stats(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for item in job.get("items", []):
            source_stats = item.get("source_stats") if isinstance(item.get("source_stats"), dict) else {}
            for source, raw_stat in source_stats.items():
                if not isinstance(raw_stat, dict):
                    continue
                row = stats.setdefault(
                    str(source),
                    {"ok": True, "count": 0, "elapsed_ms": 0, "error": "", "query_count": 0},
                )
                row["query_count"] = safe_int(row.get("query_count")) + 1
                row["count"] = safe_int(row.get("count")) + safe_int(raw_stat.get("count"))
                row["elapsed_ms"] = safe_int(row.get("elapsed_ms")) + safe_int(raw_stat.get("elapsed_ms"))
                if raw_stat.get("ok") is False:
                    row["ok"] = False
                    row["error"] = str(raw_stat.get("error") or raw_stat.get("error_kind") or row.get("error") or "")
        return stats

    def retrieval_batch_candidates_for_display(
        library_id: str,
        job_id: str,
        *,
        use_ai_evaluation: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        library = library_or_404(library_id)
        job = app_store.retrieval_batch_job(library_id, job_id)
        clean_limit = max(1, min(int(limit or 100), 200))
        candidates: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for item in job.get("items", []):
            if item.get("status") != "completed":
                continue
            run_id = str(item.get("run_id") or "").strip()
            if not run_id:
                continue
            try:
                run_report = app_store.retrieval_run_report(library_id, run_id)
            except ValueError:
                continue
            for row in run_report.get("candidates") or []:
                if not isinstance(row, dict):
                    continue
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                candidate = dict(payload or row)
                stored_candidate_id = str(row.get("candidate_id") or candidate.get("candidate_id") or "").strip()
                if stored_candidate_id:
                    candidate["stored_candidate_id"] = stored_candidate_id
                candidate["candidate_id"] = ""
                candidate["batch_job_id"] = str(job.get("job_id") or "")
                candidate["batch_run_id"] = run_id
                candidate["batch_query"] = str(item.get("query") or "")
                candidate["batch_query_index"] = int(item.get("query_index") or 0)
                candidate["batch_queries"] = [str(item.get("query") or "")]
                candidate["batch_hit_count"] = 1
                key = retrieval_batch_candidate_key(candidate)
                existing = by_key.get(key)
                if existing:
                    query = str(item.get("query") or "")
                    if query and query not in existing.setdefault("batch_queries", []):
                        existing["batch_queries"].append(query)
                    existing["batch_hit_count"] = int(existing.get("batch_hit_count") or 1) + 1
                    sources = existing.get("sources") if isinstance(existing.get("sources"), list) else []
                    for source in [candidate.get("source"), *(candidate.get("sources") if isinstance(candidate.get("sources"), list) else [])]:
                        clean_source = str(source or "").strip()
                        if clean_source and clean_source not in sources:
                            sources.append(clean_source)
                    if sources:
                        existing["sources"] = sources
                        existing["source_count"] = len(sources)
                        existing["multi_source"] = len(sources) > 1
                    continue
                by_key[key] = candidate
                candidates.append(candidate)
                if len(candidates) >= clean_limit:
                    break
            if len(candidates) >= clean_limit:
                break
        annotate_existing_retrieval_matches(ZoteroRepository(library), candidates)
        query_text = " / ".join(str(query or "") for query in job.get("queries", []) if str(query or "").strip())
        ai_summary = evaluate_retrieval_candidates_with_ai(
            library_id,
            query_text or str(job.get("job_id") or "batch retrieval"),
            candidates,
            use_ai_evaluation=use_ai_evaluation,
        )
        return {
            "job": job,
            "candidates": candidates,
            "source_stats": retrieval_batch_source_stats(job),
            "ai_evaluation_summary": ai_summary,
        }

    def execute_retrieval_batch_job(library_id: str, job_id: str) -> None:
        try:
            app_store.mark_retrieval_batch_job_running(library_id, job_id)
            job = app_store.retrieval_batch_job(library_id, job_id)
            if job.get("status") in {"canceled", "paused"}:
                app_store.refresh_retrieval_batch_job_progress(library_id, job_id)
                return
            for item in job.get("items", []):
                current_job = app_store.retrieval_batch_job(library_id, job_id)
                if current_job.get("status") in {"canceled", "paused"}:
                    app_store.refresh_retrieval_batch_job_progress(library_id, job_id)
                    return
                if item.get("status") in {"completed", "canceled"}:
                    continue
                job_item_id = str(item.get("job_item_id") or "")
                if not app_store.mark_retrieval_batch_item_running(library_id, job_item_id):
                    app_store.refresh_retrieval_batch_job_progress(library_id, job_id)
                    continue
                try:
                    context = current_job.get("context") if isinstance(current_job.get("context"), dict) else {}
                    source_limits = context.get("source_limits") if isinstance(context.get("source_limits"), dict) else None
                    result = run_retrieval_search_for_library(
                        library_id,
                        str(item.get("query") or ""),
                        current_job.get("sources") or None,
                        int(current_job.get("limit_per_query") or 10),
                        use_ai_evaluation=False,
                        source_limits=source_limits,
                    )
                    app_store.complete_retrieval_batch_item(
                        library_id,
                        job_item_id,
                        status="completed",
                        run_id=result.get("run_id", ""),
                        candidate_count=len(result.get("candidates") or []),
                        source_stats=result.get("source_stats") if isinstance(result.get("source_stats"), dict) else {},
                    )
                except Exception as exc:  # noqa: BLE001 - one failed query should not stop the whole batch
                    app_store.complete_retrieval_batch_item(
                        library_id,
                        job_item_id,
                        status="failed",
                        error=str(exc),
                    )
                app_store.refresh_retrieval_batch_job_progress(library_id, job_id)
            latest_job = app_store.retrieval_batch_job(library_id, job_id)
            if latest_job.get("status") not in {"canceled", "paused"}:
                app_store.mark_retrieval_batch_job_finished(library_id, job_id, "completed")
        except Exception as exc:  # noqa: BLE001 - persist systemic batch failure for the UI
            try:
                if app_store.retrieval_batch_job(library_id, job_id).get("status") in {"canceled", "paused"}:
                    app_store.refresh_retrieval_batch_job_progress(library_id, job_id)
                    return
            except Exception:
                pass
            app_store.mark_retrieval_batch_job_finished(library_id, job_id, "failed", str(exc))
        finally:
            with RETRIEVAL_BATCH_LOCK:
                RUNNING_RETRIEVAL_BATCHES.discard(job_id)

    def start_retrieval_batch_worker(library_id: str, job_id: str) -> None:
        with RETRIEVAL_BATCH_LOCK:
            if job_id in RUNNING_RETRIEVAL_BATCHES:
                return
            RUNNING_RETRIEVAL_BATCHES.add(job_id)
        if os.environ.get("WEB_LIBRARY_RETRIEVAL_BATCH_INLINE", "").strip().lower() in {"1", "true", "yes"}:
            execute_retrieval_batch_job(library_id, job_id)
            return
        thread = threading.Thread(target=execute_retrieval_batch_job, args=(library_id, job_id), daemon=True)
        thread.start()

    def merge_guided_source_stats(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(base) if isinstance(base, dict) else {}
        for source, raw_stat in (update or {}).items():
            if not isinstance(raw_stat, dict):
                continue
            item = merged.setdefault(str(source), {"ok": True, "count": 0, "elapsed_ms": 0, "rate_limit_wait_ms": 0})
            item["count"] = safe_int(item.get("count")) + safe_int(raw_stat.get("count"))
            item["elapsed_ms"] = safe_int(item.get("elapsed_ms")) + safe_int(raw_stat.get("elapsed_ms"))
            item["rate_limit_wait_ms"] = safe_int(item.get("rate_limit_wait_ms")) + safe_int(raw_stat.get("rate_limit_wait_ms"))
            if raw_stat.get("ok") is False:
                item["ok"] = False
                item["error"] = str(raw_stat.get("error") or item.get("error") or "")
                item["error_kind"] = str(raw_stat.get("error_kind") or item.get("error_kind") or "")
                item["action"] = str(raw_stat.get("action") or item.get("action") or "")
            if raw_stat.get("filtering"):
                item["filtering"] = raw_stat.get("filtering")
        return merged

    def guided_search_candidates_for_display(
        library_id: str,
        job_id: str,
        *,
        use_ai_evaluation: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        library = library_or_404(library_id)
        job = app_store.retrieval_guided_job(library_id, job_id)
        clean_limit = max(1, min(int(limit or 200), 500))
        candidates: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for run_id in job.get("run_ids") or []:
            clean_run_id = str(run_id or "").strip()
            if not clean_run_id:
                continue
            try:
                run_report = app_store.retrieval_run_report(library_id, clean_run_id)
            except ValueError:
                continue
            run_payload = run_report.get("run") if isinstance(run_report.get("run"), dict) else {}
            for row in run_report.get("candidates") or []:
                if not isinstance(row, dict):
                    continue
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                candidate = dict(payload or row)
                stored_candidate_id = str(row.get("candidate_id") or candidate.get("candidate_id") or "").strip()
                if stored_candidate_id:
                    candidate["stored_candidate_id"] = stored_candidate_id
                candidate["candidate_id"] = ""
                candidate["guided_job_id"] = str(job.get("job_id") or "")
                candidate["guided_run_id"] = clean_run_id
                candidate["guided_query"] = str(run_payload.get("query") or "")
                key = retrieval_batch_candidate_key(candidate)
                existing = by_key.get(key)
                if existing:
                    query = str(run_payload.get("query") or "")
                    if query and query not in existing.setdefault("guided_queries", []):
                        existing["guided_queries"].append(query)
                    existing["guided_hit_count"] = safe_int(existing.get("guided_hit_count")) + 1
                    sources = existing.get("sources") if isinstance(existing.get("sources"), list) else []
                    for source in [candidate.get("source"), *(candidate.get("sources") if isinstance(candidate.get("sources"), list) else [])]:
                        clean_source = str(source or "").strip()
                        if clean_source and clean_source not in sources:
                            sources.append(clean_source)
                    if sources:
                        existing["sources"] = sources
                        existing["source_count"] = len(sources)
                        existing["multi_source"] = len(sources) > 1
                    continue
                candidate["guided_queries"] = [str(run_payload.get("query") or "")]
                candidate["guided_hit_count"] = 1
                by_key[key] = candidate
                candidates.append(candidate)
                if len(candidates) >= clean_limit:
                    break
            if len(candidates) >= clean_limit:
                break
        candidates.sort(key=lambda candidate: safe_int(candidate.get("quality_score")), reverse=True)
        annotate_existing_retrieval_matches(ZoteroRepository(library), candidates)
        if use_ai_evaluation:
            query_text = " / ".join(str(item.get("query_text") or item.get("query") or "") for item in (job.get("plan") or {}).get("queries", []) if isinstance(item, dict))
            ai_summary = evaluate_retrieval_candidates_with_ai(
                library_id,
                query_text or str(job.get("topic") or "guided search"),
                candidates,
                use_ai_evaluation=False,
            )
        else:
            ai_summary = {"status": "skipped", "score_source": "deterministic_rules", "candidate_count": len(candidates)}
        coverage = guided_search_coverage(job=job, candidates=candidates, auto_expanded=bool((job.get("coverage") or {}).get("auto_expanded")))
        return {
            "job": job,
            "candidates": candidates,
            "source_stats": job.get("source_stats") if isinstance(job.get("source_stats"), dict) else {},
            "coverage": coverage,
            "ai_evaluation_summary": ai_summary,
        }

    def execute_retrieval_guided_job(library_id: str, job_id: str) -> None:
        try:
            job = app_store.retrieval_guided_job(library_id, job_id)
            if job.get("status") in {"canceled", "paused"}:
                return
            progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
            app_store.update_retrieval_guided_job(
                library_id,
                job_id,
                status="running",
                progress={**progress, "stage": "planning"},
                started=True,
            )
            job = app_store.append_retrieval_guided_event(library_id, job_id, "任务开始，正在准备检索计划。")
            job = app_store.retrieval_guided_job(library_id, job_id)
            plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
            job_options = job.get("options") if isinstance(job.get("options"), dict) else {}
            if not plan.get("queries"):
                app_store.append_retrieval_guided_event(
                    library_id,
                    job_id,
                    "正在生成 AI 检索计划。" if job.get("use_ai_planning") else "正在生成规则检索计划。",
                )
                plan = guided_search_plan_for_library(
                    library_id,
                    topic=str(job.get("topic") or ""),
                    mode=str(job.get("mode") or "quality"),
                    sources=[str(source) for source in job.get("sources") or []],
                    material_types=[str(item) for item in job.get("material_types") or []],
                    use_ai_planning=bool(job.get("use_ai_planning")),
                    search_route=str(job_options.get("search_route") or "legacy"),
                    input_text=str(job_options.get("input_text") or job.get("input_text") or job.get("topic") or ""),
                    expansion_level=str(job_options.get("expansion_level") or "balanced"),
                    language_policy=str(job_options.get("language_policy") or "source_adaptive"),
                )
                app_store.append_retrieval_guided_event(
                    library_id,
                    job_id,
                    f"检索计划已生成，共 {safe_int(plan.get('query_count')) or len(plan.get('queries') or [])} 组检索。",
                    kind="success",
                )
            else:
                app_store.append_retrieval_guided_event(
                    library_id,
                    job_id,
                    f"使用已确认的检索计划，共 {safe_int(plan.get('query_count')) or len(plan.get('queries') or [])} 组检索。",
                    kind="success",
                )
            apply_guided_plan_limit(
                plan,
                normalize_guided_limit_per_source(job_options.get("limit_per_source"), str(job.get("mode") or "quality")),
                job_options.get("source_limits") if isinstance(job_options.get("source_limits"), dict) else None,
            )
            job = app_store.retrieval_guided_job(library_id, job_id)
            queries = [item for item in plan.get("queries") or [] if isinstance(item, dict)]
            progress = {
                **(job.get("progress") if isinstance(job.get("progress"), dict) else {}),
                "stage": "retrieving",
                "total_queries": len(queries),
                "completed_queries": safe_int((job.get("progress") or {}).get("completed_queries")),
                "failed_queries": safe_int((job.get("progress") or {}).get("failed_queries")),
                "candidate_count": safe_int((job.get("progress") or {}).get("candidate_count")),
            }
            app_store.update_retrieval_guided_job(library_id, job_id, plan=plan, progress=progress)
            auto_expanded = bool((job.get("coverage") or {}).get("auto_expanded"))
            completed_queries = safe_int(progress.get("completed_queries"))
            failed_queries = safe_int(progress.get("failed_queries"))
            run_ids = [str(run_id) for run_id in job.get("run_ids") or [] if str(run_id or "").strip()]
            source_stats = job.get("source_stats") if isinstance(job.get("source_stats"), dict) else {}
            index = 0
            while index < len(queries):
                current_job = app_store.retrieval_guided_job(library_id, job_id)
                if current_job.get("status") in {"canceled", "paused"}:
                    return
                if index < completed_queries + failed_queries:
                    index += 1
                    continue
                query_item = queries[index]
                query_text = str(query_item.get("query_text") or query_item.get("query") or current_job.get("topic") or "").strip()
                query_sources = [str(source or "").strip().lower() for source in query_item.get("sources") or current_job.get("sources") or [] if str(source or "").strip()]
                options = SearchOptions.from_payload(current_job.get("options") if isinstance(current_job.get("options"), dict) else {})
                strategy = plan.get("strategy") if isinstance(plan.get("strategy"), dict) else guided_strategy(str(current_job.get("mode") or "quality"))
                source_limits = strategy.get("source_limits") if isinstance(strategy.get("source_limits"), dict) else {}
                limit_for_query = guided_limit_for_sources(
                    source_limits,
                    query_sources or [str(source) for source in current_job.get("sources") or []],
                    safe_int(strategy.get("limit_per_source")) or 10,
                )
                progress = {
                    **(current_job.get("progress") if isinstance(current_job.get("progress"), dict) else {}),
                    "stage": "retrieving",
                    "current_query": query_text,
                    "current_query_index": index,
                    "total_queries": len(queries),
                    "completed_queries": completed_queries,
                    "failed_queries": failed_queries,
                    "candidate_count": safe_int((current_job.get("progress") or {}).get("candidate_count")),
                }
                current_job = app_store.update_retrieval_guided_job(library_id, job_id, progress=progress)
                current_job = app_store.append_retrieval_guided_event(
                    library_id,
                    job_id,
                    f"开始检索 {index + 1}/{len(queries)}：{query_text}",
                    data={"query": query_text, "sources": query_sources, "limit": limit_for_query},
                )
                progress = current_job.get("progress") if isinstance(current_job.get("progress"), dict) else progress
                try:
                    result = run_retrieval_search_for_library(
                        library_id,
                        query_text,
                        query_sources or current_job.get("sources") or None,
                        limit_for_query,
                        use_ai_evaluation=False,
                        source_limits=source_limits,
                        search_options=options,
                    )
                    run_id = str(result.get("run_id") or "")
                    if run_id:
                        run_ids.append(run_id)
                    completed_queries += 1
                    source_stats = merge_guided_source_stats(source_stats, result.get("source_stats") if isinstance(result.get("source_stats"), dict) else {})
                    candidate_count = safe_int((current_job.get("progress") or {}).get("candidate_count")) + len(result.get("candidates") or [])
                    display = guided_search_candidates_for_display(library_id, job_id, use_ai_evaluation=False, limit=300) if run_ids[:-1] else {"candidates": []}
                    all_candidates = list(display.get("candidates") or []) + list(result.get("candidates") or [])
                    coverage = guided_search_coverage(job={**current_job, "run_ids": run_ids}, candidates=all_candidates, auto_expanded=auto_expanded)
                    progress = {
                        **progress,
                        "completed_queries": completed_queries,
                        "failed_queries": failed_queries,
                        "candidate_count": candidate_count,
                        "current_query": "",
                    }
                    app_store.update_retrieval_guided_job(
                        library_id,
                        job_id,
                        source_stats=source_stats,
                        run_ids=run_ids,
                        coverage=coverage,
                        progress=progress,
                    )
                    source_counts = []
                    for source_name, stats in (result.get("source_stats") if isinstance(result.get("source_stats"), dict) else {}).items():
                        if isinstance(stats, dict):
                            source_counts.append(f"{source_name} {safe_int(stats.get('count'))}")
                    source_summary = " / ".join(source_counts[:5])
                    app_store.append_retrieval_guided_event(
                        library_id,
                        job_id,
                        f"完成检索 {index + 1}/{len(queries)}：新增 {len(result.get('candidates') or [])} 条候选"
                        + (f"，{source_summary}" if source_summary else ""),
                        kind="success",
                        data={"query": query_text, "candidate_count": len(result.get("candidates") or [])},
                    )
                    current_job = app_store.retrieval_guided_job(library_id, job_id)
                    progress = current_job.get("progress") if isinstance(current_job.get("progress"), dict) else progress
                    if (
                        str(current_job.get("mode") or "") == "coverage"
                        and not auto_expanded
                        and coverage.get("status") != "good"
                        and index == len(queries) - 1
                    ):
                        gap_queries = guided_gap_queries(str(current_job.get("topic") or ""), coverage, [str(source) for source in current_job.get("sources") or []])
                        seen = {str(item.get("query_text") or item.get("query") or "").casefold() for item in queries}
                        for gap in gap_queries:
                            if str(gap.get("query_text") or gap.get("query") or "").casefold() not in seen:
                                queries.append(gap)
                        auto_expanded = True
                        plan["queries"] = queries
                        plan["query_count"] = len(queries)
                        coverage["auto_expanded"] = True
                        coverage["auto_expanded_queries"] = gap_queries
                        progress["total_queries"] = len(queries)
                        app_store.update_retrieval_guided_job(library_id, job_id, plan=plan, coverage=coverage, progress=progress)
                        app_store.append_retrieval_guided_event(
                            library_id,
                            job_id,
                            f"发现覆盖缺口，已自动补检 {len(gap_queries)} 组检索词。",
                            kind="warning",
                        )
                except Exception as exc:  # noqa: BLE001 - keep guided search moving across query failures
                    failed_queries += 1
                    progress = {**progress, "failed_queries": failed_queries, "last_error": str(exc), "current_query": ""}
                    app_store.update_retrieval_guided_job(library_id, job_id, progress=progress, error=str(exc))
                    app_store.append_retrieval_guided_event(
                        library_id,
                        job_id,
                        f"检索失败 {index + 1}/{len(queries)}：{query_text}；{exc}",
                        kind="error",
                    )
                index += 1
            latest = app_store.retrieval_guided_job(library_id, job_id)
            if latest.get("status") not in {"canceled", "paused"}:
                status = "partial" if safe_int((latest.get("progress") or {}).get("failed_queries")) else "completed"
                app_store.update_retrieval_guided_job(
                    library_id,
                    job_id,
                    status=status,
                    progress={**(latest.get("progress") if isinstance(latest.get("progress"), dict) else {}), "stage": status},
                    finished=True,
                )
                final_job = app_store.retrieval_guided_job(library_id, job_id)
                final_progress = final_job.get("progress") if isinstance(final_job.get("progress"), dict) else {}
                app_store.append_retrieval_guided_event(
                    library_id,
                    job_id,
                    f"任务{('部分完成' if status == 'partial' else '完成')}：检索词 {safe_int(final_progress.get('completed_queries'))}/{safe_int(final_progress.get('total_queries'))}，候选 {safe_int(final_progress.get('candidate_count'))} 条。",
                    kind="warning" if status == "partial" else "success",
                )
        except Exception as exc:  # noqa: BLE001 - persist guided job systemic failure
            try:
                existing_job = app_store.retrieval_guided_job(library_id, job_id)
                existing_progress = existing_job.get("progress") if isinstance(existing_job.get("progress"), dict) else {}
                app_store.update_retrieval_guided_job(
                    library_id,
                    job_id,
                    status="failed",
                    error=str(exc),
                    progress={**existing_progress, "stage": "failed", "error": str(exc)},
                    finished=True,
                )
                app_store.append_retrieval_guided_event(library_id, job_id, f"任务失败：{exc}", kind="error")
            except Exception:
                pass
        finally:
            with RETRIEVAL_GUIDED_LOCK:
                RUNNING_RETRIEVAL_GUIDED_JOBS.discard(job_id)

    def start_retrieval_guided_worker(library_id: str, job_id: str) -> None:
        with RETRIEVAL_GUIDED_LOCK:
            if job_id in RUNNING_RETRIEVAL_GUIDED_JOBS:
                return
            RUNNING_RETRIEVAL_GUIDED_JOBS.add(job_id)
        if os.environ.get("WEB_LIBRARY_RETRIEVAL_GUIDED_INLINE", "").strip().lower() in {"1", "true", "yes"}:
            execute_retrieval_guided_job(library_id, job_id)
            return
        thread = threading.Thread(target=execute_retrieval_guided_job, args=(library_id, job_id), daemon=True)
        thread.start()

    def retrieval_agent_job(library_id: str, job_id: str) -> dict[str, Any]:
        job = app_store.retrieval_guided_job(library_id, job_id)
        if str(job.get("search_route") or "") != "agent":
            raise ValueError("该任务不是智能体检索任务。")
        return job

    def retrieval_agent_status_for_library(
        library_id: str,
        *,
        check_runtime: bool = False,
    ) -> dict[str, Any]:
        library = library_or_404(library_id)
        codex_config = api_config_codex_for_library(library_id)
        missing = [
            label
            for key, label in (
                ("model", "Codex 模型"),
                ("base_url", "Codex Base URL"),
                ("api_key", "Codex API Key"),
            )
            if not str(codex_config.get(key) or "").strip()
        ]
        if missing:
            return {
                "ready": False,
                "status": "not_configured",
                "message": f"Codex SDK 未就绪：请先配置{'、'.join(missing)}。",
                "missing": missing,
                "checked": False,
            }
        payload = {
            "ready": True,
            "status": "configured",
            "message": "Codex SDK 配置已就绪。",
            "missing": [],
            "checked": False,
            "model": str(codex_config.get("model") or ""),
            "base_url": str(codex_config.get("base_url") or ""),
        }
        if not check_runtime:
            return payload
        probe = run_codex_connectivity_probe(library=library, codex_config=codex_config)
        return {
            **payload,
            "ready": probe.get("ok") is True,
            "status": "ready" if probe.get("ok") is True else "runtime_error",
            "message": str(probe.get("message") or ""),
            "checked": True,
            "diagnostics": probe.get("diagnostics") if isinstance(probe.get("diagnostics"), dict) else {},
        }

    def retrieval_agent_candidate_title(candidate: dict[str, Any]) -> str:
        item = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        return str(candidate.get("title") or fields.get("title") or "").strip()

    def retrieval_agent_candidate_summary(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": str(
                    candidate.get("stored_candidate_id")
                    or candidate.get("candidate_id")
                    or retrieval_batch_candidate_key(candidate)
                ),
                "title": retrieval_agent_candidate_title(candidate),
                "source": str(candidate.get("source") or ""),
                "year": str(candidate.get("year") or ""),
                "resource_type": str(candidate.get("resource_type") or ""),
            }
            for candidate in candidates[:40]
            if isinstance(candidate, dict)
        ]

    def sync_retrieval_agent_state_with_job(
        library_id: str,
        job: dict[str, Any],
        state: dict[str, Any],
        *,
        candidates_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = normalize_agent_state(state)
        display = candidates_result if isinstance(candidates_result, dict) else {}
        run_ids = [str(item) for item in job.get("run_ids") or [] if str(item or "").strip()]
        coverage = display.get("coverage") if isinstance(display.get("coverage"), dict) else job.get("coverage")
        if run_ids:
            current["coverage_state"] = coverage_state_from_guided(
                coverage,
                run_ids=run_ids,
                source_stats=job.get("source_stats") if isinstance(job.get("source_stats"), dict) else {},
            )
        job_status = str(job.get("status") or "")
        thread_status = str((current.get("thread") or {}).get("thread_status") or "")
        changed = False
        if job_status in {"completed", "partial"} and thread_status == "searching":
            current["thread"]["thread_status"] = "waiting_user"
            for item in current.get("pending_actions") or []:
                if item.get("status") == "approved":
                    item["status"] = "executed"
                    item["updated_at"] = now_iso()
            changed = True
        elif job_status == "failed" and thread_status == "searching":
            current["thread"]["thread_status"] = "failed"
            current["thread"]["last_error"] = str(job.get("error") or "联网检索失败。")
            changed = True
        elif job_status == "canceled" and thread_status == "searching":
            current["thread"]["thread_status"] = "interrupted"
            changed = True
        if changed:
            current["updated_at"] = now_iso()
            retrieval_agent_store.upsert_session(library_id, str(job["job_id"]), current)
        return current

    def retrieval_agent_state_payload(
        library_id: str,
        job_id: str,
        *,
        include_candidates: bool = True,
    ) -> dict[str, Any]:
        job = retrieval_agent_job(library_id, job_id)
        session = retrieval_agent_store.get_session(library_id, job_id)
        state = normalize_agent_state(session.get("state") if session else default_agent_state())
        feedback = retrieval_agent_store.list_feedback(library_id, job_id)
        current_feedback = feedback_summary(feedback)
        if current_feedback != state.get("feedback_summary"):
            state["feedback_summary"] = current_feedback
            state["updated_at"] = now_iso()
            retrieval_agent_store.upsert_session(library_id, job_id, state)
        candidates_result: dict[str, Any] = {"candidates": [], "coverage": job.get("coverage") or {}}
        if include_candidates and job.get("run_ids"):
            candidates_result = guided_search_candidates_for_display(
                library_id,
                job_id,
                use_ai_evaluation=False,
                limit=500,
            )
            job = candidates_result.get("job") if isinstance(candidates_result.get("job"), dict) else job
        state = sync_retrieval_agent_state_with_job(
            library_id,
            job,
            state,
            candidates_result=candidates_result,
        )
        latest_turn = retrieval_agent_store.latest_turn(library_id, job_id)
        if latest_turn and latest_turn.get("status") == "queued":
            start_retrieval_agent_turn_worker(library_id, job_id, str(latest_turn["turn_id"]))
        elif latest_turn and latest_turn.get("status") == "running":
            with RETRIEVAL_AGENT_LOCK:
                active_locally = str(latest_turn["turn_id"]) in RUNNING_RETRIEVAL_AGENT_TURNS
            if not active_locally:
                retrieval_agent_store.update_turn(
                    library_id,
                    job_id,
                    str(latest_turn["turn_id"]),
                    status="queued",
                    result=latest_turn.get("result") if isinstance(latest_turn.get("result"), dict) else {},
                )
                start_retrieval_agent_turn_worker(library_id, job_id, str(latest_turn["turn_id"]))
        memory = retrieval_agent_store.list_memory(library_id)
        state["memory_policy"]["library_enabled"] = any(item.get("status") == "enabled" for item in memory)
        return {
            "job": job,
            "agent_state": state,
            "messages": retrieval_agent_store.list_messages(library_id, job_id),
            "latest_turn": retrieval_agent_store.latest_turn(library_id, job_id),
            "memory": memory,
            "feedback": feedback,
            "candidates": candidates_result.get("candidates") if include_candidates else [],
            "coverage": candidates_result.get("coverage") or job.get("coverage") or {},
            "source_stats": job.get("source_stats") if isinstance(job.get("source_stats"), dict) else {},
        }

    def execute_retrieval_agent_turn(library_id: str, job_id: str, turn_id: str) -> None:
        try:
            turn = retrieval_agent_store.get_turn(library_id, job_id, turn_id)
            if not turn or turn.get("status") != "queued":
                return
            job = retrieval_agent_job(library_id, job_id)
            session = retrieval_agent_store.get_session(library_id, job_id)
            state = normalize_agent_state(session.get("state") if session else default_agent_state())
            state = mark_turn_running(state, turn_id)
            retrieval_agent_store.upsert_session(library_id, job_id, state)
            retrieval_agent_store.update_turn(library_id, job_id, turn_id, status="running")
            app_store.append_retrieval_guided_event(
                library_id,
                job_id,
                "智能体正在理解本轮要求。",
                kind="agent",
                data={"turn_id": turn_id},
            )
            status = retrieval_agent_status_for_library(library_id)
            if not status.get("ready"):
                raise RuntimeError(str(status.get("message") or "Codex SDK 未就绪。"))
            display = (
                guided_search_candidates_for_display(library_id, job_id, use_ai_evaluation=False, limit=100)
                if job.get("run_ids")
                else {"candidates": []}
            )
            feedback = retrieval_agent_store.list_feedback(library_id, job_id)
            state["feedback_summary"] = feedback_summary(feedback)
            enabled_memory = retrieval_agent_store.list_memory(library_id, status="enabled")
            messages = retrieval_agent_store.list_messages(library_id, job_id)
            prompt = build_agent_prompt(
                state=state,
                messages=messages,
                enabled_library_memory=enabled_memory,
                selected_sources=[str(item) for item in job.get("sources") or []],
                candidate_summary=retrieval_agent_candidate_summary(list(display.get("candidates") or [])),
            )
            result = run_codex_prompt(
                library=library_or_404(library_id),
                codex_config=api_config_codex_for_library(library_id),
                prompt=prompt,
                include_agentic_rag_skill=False,
                ephemeral=True,
                read_only=True,
            )
            decision = enforce_clarification_before_search(
                normalize_agent_decision(str(result.get("assistant_text") or "")),
                state=state,
                messages=messages,
            )
            current_turn = retrieval_agent_store.get_turn(library_id, job_id, turn_id)
            if not current_turn or current_turn.get("status") != "running":
                return
            next_state = apply_decision_to_state(state, decision, turn_id=turn_id)
            existing_memories = retrieval_agent_store.list_memory(library_id)
            existing_memory_keys = {
                json.dumps(item.get("content") or {}, ensure_ascii=False, sort_keys=True)
                for item in existing_memories
            }
            memory_ids: list[str] = []
            for suggestion in decision.get("memory_suggestions") or []:
                content = suggestion.get("content") if isinstance(suggestion.get("content"), dict) else {}
                memory_key = json.dumps(content, ensure_ascii=False, sort_keys=True)
                if not content or memory_key in existing_memory_keys:
                    continue
                memory = retrieval_agent_store.create_memory_suggestion(
                    library_id,
                    kind=str(suggestion.get("kind") or "preference"),
                    content=content,
                    source_job_id=job_id,
                )
                memory_ids.append(str(memory.get("memory_id") or ""))
                existing_memory_keys.add(memory_key)
            assistant_message = retrieval_agent_store.add_message(
                library_id,
                job_id,
                "assistant",
                str(decision.get("assistant_message") or ""),
                metadata={
                    "action": decision.get("action"),
                    "turn_id": turn_id,
                    "codex_turn_id": result.get("turn_id"),
                    "memory_suggestion_ids": memory_ids,
                },
            )
            retrieval_agent_store.upsert_session(library_id, job_id, next_state)
            retrieval_agent_store.update_turn(
                library_id,
                job_id,
                turn_id,
                status="completed",
                result={
                    "decision": decision,
                    "assistant_message_id": assistant_message.get("message_id"),
                    "codex_turn_id": result.get("turn_id"),
                    "usage": result.get("usage"),
                },
            )
            app_store.append_retrieval_guided_event(
                library_id,
                job_id,
                "智能体已更新当前理解。"
                + ("请确认检索计划后开始联网。" if decision.get("action") == "PROPOSE_SEARCH" else ""),
                kind="agent",
                data={"turn_id": turn_id, "action": decision.get("action")},
            )
        except Exception as exc:  # noqa: BLE001 - persist agent failures for UI recovery
            current_turn = retrieval_agent_store.get_turn(library_id, job_id, turn_id)
            if current_turn and current_turn.get("status") == "interrupted":
                return
            try:
                session = retrieval_agent_store.get_session(library_id, job_id)
                state = mark_turn_failed(
                    session.get("state") if session else default_agent_state(),
                    turn_id,
                    str(exc),
                )
                retrieval_agent_store.upsert_session(library_id, job_id, state)
                retrieval_agent_store.update_turn(
                    library_id,
                    job_id,
                    turn_id,
                    status="failed",
                    error=str(exc),
                )
                retrieval_agent_store.add_message(
                    library_id,
                    job_id,
                    "system",
                    f"智能体处理失败：{exc}",
                    metadata={"turn_id": turn_id, "error": True},
                )
                app_store.append_retrieval_guided_event(
                    library_id,
                    job_id,
                    f"智能体处理失败：{exc}",
                    kind="error",
                    data={"turn_id": turn_id},
                )
            except Exception:
                pass
        finally:
            with RETRIEVAL_AGENT_LOCK:
                RUNNING_RETRIEVAL_AGENT_TURNS.discard(turn_id)

    def start_retrieval_agent_turn_worker(library_id: str, job_id: str, turn_id: str) -> None:
        with RETRIEVAL_AGENT_LOCK:
            if turn_id in RUNNING_RETRIEVAL_AGENT_TURNS:
                return
            RUNNING_RETRIEVAL_AGENT_TURNS.add(turn_id)
        if os.environ.get("WEB_LIBRARY_RETRIEVAL_AGENT_INLINE", "").strip().lower() in {"1", "true", "yes"}:
            execute_retrieval_agent_turn(library_id, job_id, turn_id)
            return
        thread = threading.Thread(
            target=execute_retrieval_agent_turn,
            args=(library_id, job_id, turn_id),
            daemon=True,
        )
        thread.start()

    def retrieval_agent_plan_to_guided_plan(
        library_id: str,
        job: dict[str, Any],
        action: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        preview = action.get("plan_preview") if isinstance(action.get("plan_preview"), dict) else {}
        intents = preview.get("query_intents") if isinstance(preview.get("query_intents"), list) else []
        selected_sources = [str(item).strip().lower() for item in job.get("sources") or [] if str(item).strip()]
        registry = retrieval_provider_registry_for_library(library_id)
        budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
        query_limit = max(3, min(safe_int(budget.get("max_queries_per_round")) or 18, 40))
        existing_plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
        existing_queries = [
            item for item in existing_plan.get("queries") or [] if isinstance(item, dict)
        ]
        seen = {
            str(item.get("query_text") or item.get("query") or "").strip().casefold()
            for item in existing_queries
        }
        new_queries: list[dict[str, Any]] = []
        new_groups: list[dict[str, Any]] = []
        for intent in intents:
            if not isinstance(intent, dict):
                continue
            material = normalized_material_type(intent.get("material_type")) or "paper"
            supported = default_guided_sources_for_materials(registry, [material])
            sources = [source for source in selected_sources if source in supported]
            if not sources:
                sources = list(selected_sources)
            group_queries: list[str] = []
            for keyword in intent.get("keywords") or []:
                query = re.sub(r"\s+", " ", str(keyword or "")).strip()
                key = query.casefold()
                if not query or key in seen:
                    continue
                seen.add(key)
                group_queries.append(query)
                new_queries.append(
                    {
                        "query": query,
                        "query_text": query,
                        "intent": str(intent.get("purpose") or material),
                        "reason": "agent_decision",
                        "resource_type": material,
                        "sources": sources,
                    }
                )
                if len(new_queries) >= query_limit:
                    break
            if group_queries:
                new_groups.append(
                    {
                        "resource_type": material,
                        "language": "zh" if any("\u4e00" <= char <= "\u9fff" for char in "".join(group_queries)) else "en",
                        "queries": group_queries,
                        "sources": sources,
                        "reason": "agent_decision",
                        "planning_status": "ai",
                        "planning_message": str(intent.get("purpose") or ""),
                    }
                )
            if len(new_queries) >= query_limit:
                break
        if not new_queries:
            raise ValueError("确认的智能体检索计划没有新的有效检索词。")
        strategy = existing_plan.get("strategy") if isinstance(existing_plan.get("strategy"), dict) else guided_strategy(str(job.get("mode") or "quality"))
        plan = {
            **existing_plan,
            "input_text": str(job.get("input_text") or job.get("topic") or ""),
            "normalized_topic": str(
                (state.get("intent_model") or {}).get("normalized_topic")
                or (state.get("intent_model") or {}).get("topic")
                or job.get("topic")
                or ""
            ),
            "queries": [*existing_queries, *new_queries],
            "query_groups": [
                *[item for item in existing_plan.get("query_groups") or [] if isinstance(item, dict)],
                *new_groups,
            ],
            "query_count": len(existing_queries) + len(new_queries),
            "strategy": strategy,
            "planner_version": "agent-v2",
            "search_route": "agent",
            "ai_enhancement": {
                "status": "applied",
                "provider": "codex",
                "message": "Codex 形成语义检索意图，后端按资料类型解析真实数据源。",
            },
        }
        options = dict(job.get("options") if isinstance(job.get("options"), dict) else {})
        intent_model = state.get("intent_model") if isinstance(state.get("intent_model"), dict) else {}
        time_range = intent_model.get("time_range") if isinstance(intent_model.get("time_range"), dict) else {}
        for key in ("start_year", "end_year"):
            if time_range.get(key):
                options[key] = time_range[key]
        options["material_types"] = [
            str(item) for item in intent_model.get("material_types") or job.get("material_types") or []
        ]
        limit_per_source = safe_int(options.get("limit_per_source")) or 8
        source_limits = options.get("source_limits") if isinstance(options.get("source_limits"), dict) else {}
        apply_guided_plan_limit(plan, limit_per_source, source_limits)
        return plan, options

    def execute_retrieval_search_job(library_id: str, job_id: str) -> None:
        try:
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_SEARCH_JOBS.get(job_id)
                if not job:
                    return
                if job.get("stop_requested"):
                    job["status"] = "canceled"
                    job["finished_at"] = now_iso()
                    job["updated_at"] = job["finished_at"]
                    return
                job["status"] = "running"
                job["started_at"] = now_iso()
                job["updated_at"] = job["started_at"]
                job_payload = retrieval_job_snapshot(job) or {}
            result = run_retrieval_search_for_library(
                library_id,
                str(job_payload.get("query") or ""),
                job_payload.get("sources") or None,
                safe_int(job_payload.get("limit")) or 10,
                include_raw=bool(job_payload.get("include_raw")),
                use_ai_evaluation=bool(job_payload.get("use_ai_evaluation")),
            )
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_SEARCH_JOBS.get(job_id)
                if not job:
                    return
                if job.get("stop_requested"):
                    job["status"] = "canceled"
                    job["message"] = "快速检索已取消。"
                else:
                    job["status"] = "completed"
                    job["result"] = result
                    job["run_id"] = result.get("run_id", "")
                    job["candidate_count"] = len(result.get("candidates") or [])
                    job["message"] = f"检索到 {job['candidate_count']} 条候选。"
                job["finished_at"] = now_iso()
                job["updated_at"] = job["finished_at"]
        except Exception as exc:  # noqa: BLE001 - surface background failures to polling UI
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_SEARCH_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    job["message"] = "快速检索失败。"
                    job["finished_at"] = now_iso()
                    job["updated_at"] = job["finished_at"]
        finally:
            with RETRIEVAL_BACKGROUND_LOCK:
                RUNNING_RETRIEVAL_SEARCH_JOBS.discard(job_id)

    def start_retrieval_search_worker(library_id: str, job_id: str) -> None:
        with RETRIEVAL_BACKGROUND_LOCK:
            if job_id in RUNNING_RETRIEVAL_SEARCH_JOBS:
                return
            RUNNING_RETRIEVAL_SEARCH_JOBS.add(job_id)
        if retrieval_background_inline_enabled("search"):
            execute_retrieval_search_job(library_id, job_id)
            return
        thread = threading.Thread(target=execute_retrieval_search_job, args=(library_id, job_id), daemon=True)
        thread.start()

    def execute_retrieval_query_plan_job(library_id: str, job_id: str) -> None:
        try:
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_QUERY_PLAN_JOBS.get(job_id)
                if not job:
                    return
                if job.get("stop_requested"):
                    job["status"] = "canceled"
                    job["finished_at"] = now_iso()
                    job["updated_at"] = job["finished_at"]
                    return
                job["status"] = "running"
                job["started_at"] = now_iso()
                job["updated_at"] = job["started_at"]
                job_payload = retrieval_job_snapshot(job) or {}
            plan = retrieval_query_plan_for_library(
                library_id,
                seed_query=str(job_payload.get("seed_query") or "robot"),
                sample_size=safe_int(job_payload.get("sample_size")) or 5,
                limit=safe_int(job_payload.get("limit")) or 5,
                use_ai=bool(job_payload.get("use_ai")),
                selected_sources=job_payload.get("selected_sources") or [],
            )
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_QUERY_PLAN_JOBS.get(job_id)
                if not job:
                    return
                if job.get("stop_requested"):
                    job["status"] = "canceled"
                    job["message"] = "AI 检索计划已取消。"
                else:
                    job["status"] = "completed"
                    job["plan"] = plan
                    job["message"] = str(plan.get("message") or "AI 检索计划已生成。")
                    job["query_count"] = safe_int(plan.get("query_count"))
                job["finished_at"] = now_iso()
                job["updated_at"] = job["finished_at"]
        except Exception as exc:  # noqa: BLE001 - surface background failures to polling UI
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_QUERY_PLAN_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    job["message"] = "AI 检索计划生成失败。"
                    job["finished_at"] = now_iso()
                    job["updated_at"] = job["finished_at"]
        finally:
            with RETRIEVAL_BACKGROUND_LOCK:
                RUNNING_RETRIEVAL_QUERY_PLAN_JOBS.discard(job_id)

    def start_retrieval_query_plan_worker(library_id: str, job_id: str) -> None:
        with RETRIEVAL_BACKGROUND_LOCK:
            if job_id in RUNNING_RETRIEVAL_QUERY_PLAN_JOBS:
                return
            RUNNING_RETRIEVAL_QUERY_PLAN_JOBS.add(job_id)
        if retrieval_background_inline_enabled("query_plan"):
            execute_retrieval_query_plan_job(library_id, job_id)
            return
        thread = threading.Thread(target=execute_retrieval_query_plan_job, args=(library_id, job_id), daemon=True)
        thread.start()

    def execute_retrieval_ai_scoring_job(library_id: str, job_id: str) -> None:
        try:
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                if not job:
                    return
                if job.get("stop_requested"):
                    job["status"] = "canceled"
                    job["finished_at"] = now_iso()
                    job["updated_at"] = job["finished_at"]
                    job["summary"] = retrieval_ai_scoring_job_summary(job)
                    return
                job["status"] = "running"
                job["started_at"] = now_iso()
                job["updated_at"] = job["started_at"]
                queue_keys = [str(key) for key in job.get("queue_keys") or []]
                query = str(job.get("query") or "")
                job["summary"] = retrieval_ai_scoring_job_summary(job)
            for client_key in queue_keys:
                with RETRIEVAL_BACKGROUND_LOCK:
                    job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                    if not job:
                        return
                    if job.get("stop_requested"):
                        job["status"] = "canceled"
                        job["message"] = "AI 推荐排序已停止。"
                        job["finished_at"] = now_iso()
                        job["updated_at"] = job["finished_at"]
                        job["summary"] = retrieval_ai_scoring_job_summary(job)
                        return
                    candidates = job.get("candidates") if isinstance(job.get("candidates"), list) else []
                    index = next((idx for idx, item in enumerate(candidates) if str(item.get("client_key") or "") == client_key), -1)
                    if index < 0:
                        continue
                    original = copy.deepcopy(candidates[index])
                    title = candidate_field(original, "title") or str(original.get("title") or "未命名候选")
                    candidates[index] = {
                        **original,
                        "ai_evaluation": {
                            "status": "evaluating",
                            "score_source": "pending_ai",
                            "score_framework": AI_CANDIDATE_SCORE_FRAMEWORK,
                            "reason": "AI 正在评分...",
                            "auto_select": False,
                        },
                    }
                    job["current_candidate_key"] = client_key
                    job["current_candidate_title"] = title
                    job["message"] = f"AI 正在评分：{title}"
                    job["updated_at"] = now_iso()
                    job["summary"] = retrieval_ai_scoring_job_summary(job)
                updated = copy.deepcopy(original)
                candidate_summary: dict[str, Any] = {}
                try:
                    evaluated_candidates = [updated]
                    candidate_summary = evaluate_retrieval_candidates_with_ai(
                        library_id,
                        query,
                        evaluated_candidates,
                        use_ai_evaluation=True,
                    )
                    updated = evaluated_candidates[0] if evaluated_candidates else updated
                except Exception as exc:  # noqa: BLE001 - keep the queue moving when one candidate fails
                    message = ai_evaluation_error_message(exc)
                    updated["ai_evaluation"] = deterministic_candidate_evaluation(
                        updated,
                        query,
                        status="fallback",
                        reason=f"AI 评分失败，已使用规则兜底：{message}",
                    )
                    candidate_summary = {"status": "error", "error": message, "score_source": "deterministic_rules"}
                updated["client_key"] = client_key
                with RETRIEVAL_BACKGROUND_LOCK:
                    job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                    if not job:
                        return
                    candidates = job.get("candidates") if isinstance(job.get("candidates"), list) else []
                    index = next((idx for idx, item in enumerate(candidates) if str(item.get("client_key") or "") == client_key), -1)
                    if index >= 0:
                        candidates[index] = updated
                    if retrieval_candidate_has_ai_model_evaluation(updated):
                        job["ai_completed_count"] = safe_int(job.get("ai_completed_count")) + 1
                    else:
                        job["failed_count"] = safe_int(job.get("failed_count")) + 1
                        job["error"] = str(candidate_summary.get("error") or "该候选 AI 评分失败，已使用规则兜底。")
                    job["completed_count"] = safe_int(job.get("completed_count")) + 1
                    job["candidates"] = sort_candidates_by_ai_evaluation(candidates)
                    job["message"] = f"AI 已评分 {job['completed_count']}/{job.get('total_count') or len(candidates)} 条。"
                    job["updated_at"] = now_iso()
                    job["summary"] = retrieval_ai_scoring_job_summary(job)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                if not job:
                    return
                if job.get("stop_requested"):
                    job["status"] = "canceled"
                    job["message"] = "AI 推荐排序已停止。"
                elif safe_int(job.get("failed_count")):
                    job["status"] = "partial"
                    job["message"] = "AI 推荐排序部分完成，失败项已使用规则兜底。"
                else:
                    job["status"] = "completed"
                    job["message"] = "AI 推荐排序已完成。"
                job["finished_at"] = now_iso()
                job["updated_at"] = job["finished_at"]
                job["summary"] = retrieval_ai_scoring_job_summary(job)
        except Exception as exc:  # noqa: BLE001 - surface systemic background failure for the UI
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    job["message"] = "AI 推荐排序失败。"
                    job["finished_at"] = now_iso()
                    job["updated_at"] = job["finished_at"]
                    job["summary"] = retrieval_ai_scoring_job_summary(job)
        finally:
            with RETRIEVAL_BACKGROUND_LOCK:
                RUNNING_RETRIEVAL_AI_SCORING_JOBS.discard(job_id)

    def start_retrieval_ai_scoring_worker(library_id: str, job_id: str) -> None:
        with RETRIEVAL_BACKGROUND_LOCK:
            if job_id in RUNNING_RETRIEVAL_AI_SCORING_JOBS:
                return
            RUNNING_RETRIEVAL_AI_SCORING_JOBS.add(job_id)
        if retrieval_background_inline_enabled("ai_scoring"):
            execute_retrieval_ai_scoring_job(library_id, job_id)
            return
        thread = threading.Thread(target=execute_retrieval_ai_scoring_job, args=(library_id, job_id), daemon=True)
        thread.start()

    @app.get("/")
    def index():
        libraries = app_store.list_libraries()
        default_source = default_service_source_path()
        return render_template("index.html", libraries=libraries, default_source=default_source)

    @app.get("/features")
    def features_index_page():
        libraries = app_store.list_libraries()
        return render_template("features.html", library=None, libraries=libraries)

    @app.get("/library/<library_id>/features")
    def features_page(library_id: str):
        library = library_or_404(library_id)
        libraries = app_store.list_libraries()
        return render_template("features.html", library=library, libraries=libraries)

    @app.get("/library/<library_id>")
    def library_page(library_id: str):
        library = library_or_404(library_id)
        libraries = app_store.list_libraries()
        return render_template("library.html", library=library, libraries=libraries)

    @app.get("/library/<library_id>/knowledge")
    def knowledge_page(library_id: str):
        library = library_or_404(library_id)
        libraries = app_store.list_libraries()
        return render_template("knowledge.html", library=library, libraries=libraries)

    @app.get("/library/<library_id>/reader")
    def reader_page(library_id: str):
        library = library_or_404(library_id)
        libraries = app_store.list_libraries()
        item_key = str(request.args.get("item_key") or "").strip()
        attachment_key = str(request.args.get("attachment_key") or "").strip()
        return render_template(
            "reader.html",
            library=library,
            libraries=libraries,
            item_key=item_key,
            attachment_key=attachment_key,
        )

    @app.get("/library/<library_id>/api-config")
    def api_config_page(library_id: str):
        library = library_or_404(library_id)
        libraries = app_store.list_libraries()
        return render_template("api_config.html", library=library, libraries=libraries)

    from . import writing as writing
    from .codex_agent import writing as writing_agent

    @app.get("/library/<library_id>/writing")
    def writing_page(library_id: str):
        library = library_or_404(library_id)
        libraries = app_store.list_libraries()
        writing.ensure_writing_files(library)
        requested_stage = request.args.get("stage")
        state = writing.load_writing_state(library)
        if requested_stage:
            state["stage"] = writing.normalize_writing_stage(requested_stage)
            writing.save_writing_state(library, state)
        papers = writing.paper_list(library)
        selected_keys = set(state.get("selected_paper_keys") or [])
        try:
            knowledge_bases = rag_list_knowledge_bases(library)
        except Exception:  # noqa: BLE001
            knowledge_bases = []
        from .rag.store import knowledge_base_item_keys as _kb_item_keys
        papers_by_kb: dict[str, list[str]] = {}
        for kb in knowledge_bases:
            kb_id = str(kb.get("knowledge_base_id") or kb.get("id") or "")
            try:
                kb_keys = _kb_item_keys(library, kb_id)
            except Exception:  # noqa: BLE001
                kb_keys = []
            papers_by_kb[kb_id] = kb_keys
        return render_template(
            "writing.html",
            library=library,
            libraries=libraries,
            writing_stages=writing.WRITING_STAGES,
            writing_stage_labels=writing.WRITING_STAGE_LABELS,
            active_stage=state.get("stage") or "topic",
            writing_state=state,
            papers=papers,
            selected_paper_keys=selected_keys,
            knowledge_bases=knowledge_bases,
            papers_by_kb=papers_by_kb,
            matrix_by_paper=writing.matrix_by_paper(library),
            writing_mapping=writing.writing_mapping_payload(library),
            outline_text=writing.load_outline(library),
            survey_text=writing.load_survey(library),
            writing_chat_messages=writing.load_writing_chat(library),
            active_writing_task=writing_agent.get_task(library_id),
        )

    @app.post("/library/<library_id>/writing/api/stage")
    def writing_api_stage(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        state = writing.load_writing_state(library)
        if payload.get("stage") in writing.WRITING_STAGES:
            state["stage"] = payload["stage"]
        writing.save_writing_state(library, state)
        return jsonify({"ok": True, "state": state})

    @app.post("/library/<library_id>/writing/api/selection")
    def writing_api_selection(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        paper_keys = payload.get("paper_keys") if isinstance(payload.get("paper_keys"), list) else []
        valid = {str(p.get("key")) for p in writing.paper_list(library)}
        selected = [str(item) for item in paper_keys if str(item) in valid]
        state = writing.load_writing_state(library)
        state["selected_paper_keys"] = selected
        state["updated_at"] = writing.now_iso()
        writing.save_writing_state(library, state)
        writing.refresh_writing_csv(library)
        return jsonify({"ok": True, "state": writing.load_writing_state(library), "matrix_by_paper": writing.matrix_by_paper(library)})

    @app.post("/library/<library_id>/writing/api/outline")
    def writing_api_outline(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        if payload.get("text") is not None:
            writing.save_outline(library, str(payload["text"]))
        mapping = writing.writing_mapping_payload(library)
        return jsonify({"ok": True, "outline": writing.load_outline(library), "mapping": mapping})

    @app.post("/library/<library_id>/writing/api/mappings")
    def writing_api_mappings(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        mappings = payload.get("mappings") if isinstance(payload.get("mappings"), list) else []
        sections = writing.parse_outline_sections(writing.load_outline(library))
        section_by_id = {s["section_id"]: s for s in sections}
        papers = {str(p.get("key")): p for p in writing.paper_list(library)}
        normalized = []
        for item in mappings:
            if not isinstance(item, dict):
                continue
            section = section_by_id.get(str(item.get("section_id") or ""))
            if not section:
                continue
            row = writing.normalize_section_mapping(library, section, item, paper_lookup=papers)
            if row:
                normalized.append(row)
        saved = writing.save_mappings(
            library,
            {
                "sections": sections,
                "papers": [{"paper_id": str(p.get("key")), "title": str(p.get("title") or "")} for p in writing.paper_list(library)],
                "mappings": normalized,
            },
        )
        return jsonify({"ok": True, "mapping": saved})

    @app.post("/library/<library_id>/writing/api/survey")
    def writing_api_survey(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        writing.save_survey(library, str(payload.get("text") or ""))
        return jsonify({"ok": True, "survey": writing.load_survey(library)})

    @app.post("/library/<library_id>/writing/api/topic")
    def writing_api_topic(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            return jsonify({"ok": False, "error": "请选择或输入综述主题"}), 400
        state = writing.load_writing_state(library)
        state["topic"] = topic
        state["updated_at"] = writing.now_iso()
        writing.save_writing_state(library, state)
        writing.append_writing_chat_message(
            library,
            {
                "role": "divider",
                "content": f"已选择主题：{topic}",
                "created_at": writing.now_iso(),
            },
        )
        return jsonify({"ok": True, "topic": topic, "state": writing.load_writing_state(library), "messages": writing.load_writing_chat(library)})

    @app.post("/library/<library_id>/writing/api/run")
    def writing_api_run(library_id: str):
        import datetime as _dt
        from uuid import uuid4 as _uuid4
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        stage = writing.normalize_writing_stage(str(payload.get("stage") or ""))
        user_question = str(payload.get("user_question") or "").strip()
        if not user_question:
            return jsonify({"ok": False, "error": "请输入综述写作问题"}), 400
        task = writing_agent.get_task(library_id)
        if task.get("status") == "running":
            return jsonify({"ok": False, "error": "已有综述写作任务正在运行"}), 409
        state = writing.load_writing_state(library)
        state["stage"] = stage
        writing.save_writing_state(library, state)
        run_id = f"write-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{_uuid4().hex[:6]}"
        user_message = {
            "role": "user",
            "content": user_question,
            "created_at": writing.now_iso(),
            "run_id": run_id,
            "stage": stage,
        }
        writing.append_writing_chat_message(library, user_message)
        writing_agent.start_writing_task(library=library, library_id=library_id, run_id=run_id, user_question=user_question, stage=stage)
        return jsonify({"ok": True, "run_id": run_id, "user_message": user_message, "messages": writing.load_writing_chat(library)})

    @app.get("/library/<library_id>/writing/api/status")
    def writing_api_status(library_id: str):
        library = library_or_404(library_id)
        task = writing_agent.get_task(library_id)
        latest = task if task.get("status") == "running" else None
        outline_path = writing._writing_dir(library) / "outline.md"
        response = jsonify({
            "running": bool(latest),
            "latest": latest,
            "messages": writing.load_writing_chat(library),
            "outline": writing.load_outline(library),
            "draft": writing.load_survey(library),
            "mapping": writing.writing_mapping_payload(library),
            "_debug_outline_path": str(outline_path),
            "_debug_outline_exists": outline_path.exists(),
            "_debug_outline_len": outline_path.stat().st_size if outline_path.exists() else 0,
        })
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.post("/library/<library_id>/writing/api/stop")
    def writing_api_stop(library_id: str):
        library = library_or_404(library_id)
        task = writing_agent.get_task(library_id)
        if task.get("status") == "running":
            run_id = task.get("run_id", "")
            stage = task.get("stage") or "topic"
            writing_agent.stop_writing_task(library_id, run_id)
            writing.append_writing_chat_message(
                library,
                {
                    "role": "assistant",
                    "content": "已停止本次综述写作任务。当前对话记忆会保留，下一次可继续；如需清空记忆，请点击“重置”。",
                    "created_at": writing.now_iso(),
                    "run_id": run_id,
                    "stage": stage,
                    "stopped": True,
                },
            )
        return jsonify({"ok": True, "messages": writing.load_writing_chat(library)})

    @app.post("/library/<library_id>/writing/api/reset")
    def writing_api_reset(library_id: str):
        """完全重置写作：清空主题、大纲、正文、映射、对话，回到初始模板。"""
        library = library_or_404(library_id)
        task = writing_agent.get_task(library_id)
        if task.get("status") == "running":
            return jsonify({"ok": False, "error": "当前有综述写作任务正在运行，请完成后再重置"}), 409
        # 重置写作状态（清空 topic + selected_paper_keys + 哈希）
        state = writing.load_writing_state(library)
        writing.save_writing_state(library, {
            "stage": "topic",
            "selected_paper_keys": [],
            "topic": "",
            "csv_hash": "",
            "outline_hash": "",
            "draft_hash": "",
            "created_at": writing.now_iso(),
            "updated_at": writing.now_iso(),
        })
        # 重写大纲、综述、映射为默认模板
        writing.save_outline(library, writing.default_writing_outline())
        writing.save_survey(library, "# 综述草稿\n\n请在右侧对话中让光牍生成或修改综述正文。\n")
        writing.save_mappings(library, {"sections": [], "papers": [], "mappings": []})
        # 清空聊天
        writing._write_json(writing._writing_dir(library) / "writing_chat.json", [])
        writing.save_writing_chat_state(library, {})
        # 重新生成 CSV
        writing.refresh_writing_csv(library)
        divider = {"role": "divider", "content": "新的对话", "created_at": writing.now_iso()}
        writing.append_writing_chat_message(library, divider)
        return jsonify({
            "ok": True,
            "divider": divider,
            "messages": writing.load_writing_chat(library),
            "state": writing.load_writing_state(library),
        })

    @app.post("/library/<library_id>/writing/api/compact")
    def writing_api_compact(library_id: str):
        library = library_or_404(library_id)
        chat_state = writing.load_writing_chat_state(library)
        thread_id = chat_state.get("thread_id")
        if not thread_id:
            return jsonify({"ok": False, "error": "当前还没有可压缩的综述写作线程"}), 400
        divider = {
            "role": "divider",
            "content": "记忆已压缩，可以继续沿用当前综述写作对话",
            "created_at": writing.now_iso(),
            "thread_id": thread_id,
            "compact": True,
        }
        writing.append_writing_chat_message(library, divider)
        return jsonify({"ok": True, "messages": writing.load_writing_chat(library)})

    @app.get("/library/<library_id>/writing/api/export/markdown")
    def writing_api_export_markdown(library_id: str):
        library = library_or_404(library_id)
        return Response(writing.build_markdown_export(library), mimetype="text/markdown; charset=utf-8")

    @app.get("/library/<library_id>/writing/api/export/csv")
    def writing_api_export_csv(library_id: str):
        library = library_or_404(library_id)
        return Response(writing.build_csv_export(library), mimetype="text/csv; charset=utf-8")




    @app.post("/api/sources/read-only")
    def api_read_only_source():
        payload = request.get_json(silent=True) or request.form
        try:
            record = create_read_only_source(str(payload.get("path") or ""), name=str(payload.get("name") or "").strip() or None)
            return jsonify({"ok": True, "library": record})
        except (SourceError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/sources/local-copy")
    def api_local_copy_source():
        payload = request.get_json(silent=True) or request.form
        try:
            record = create_local_copy(str(payload.get("path") or ""), name=str(payload.get("name") or "").strip() or None)
            return jsonify({"ok": True, "library": record})
        except (SourceError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/sources/upload-folder")
    def api_upload_folder_source():
        try:
            uploads = request.files.getlist("files")
            record = create_local_copy_from_uploads(uploads, name=str(request.form.get("name") or "").strip() or None)
            return jsonify({"ok": True, "library": record})
        except (SourceError, OSError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/server-paths/roots")
    def api_server_path_roots():
        return jsonify({"ok": True, "roots": server_path_roots()})

    @app.get("/api/server-paths/list")
    def api_server_path_list():
        try:
            return jsonify({"ok": True, **list_server_directory(str(request.args.get("path") or ""))})
        except (SourceError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/sources/<library_id>")
    def api_delete_source(library_id: str):
        try:
            library = library_or_404(library_id)
            if library.get("mode") == "local_copy" and app_store.unsynced_count(library_id) and not request.args.get("confirm"):
                return jsonify({"ok": False, "requires_confirmation": True, "error": "本地副本有未同步更改，确认后才会删除。"}), 409
            deleted = delete_source(library_id)
            return jsonify({"ok": True, "library": deleted})
        except (SourceError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/state")
    def api_library_state(library_id: str):
        try:
            repo = ZoteroRepository(library_or_404(library_id))
            state = repo.state()
            return jsonify({"ok": True, **state})
        except (SourceError, OSError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/api-config")
    def api_get_library_api_config(library_id: str):
        try:
            library_or_404(library_id)
            include_secrets = truthy_query_flag(request.args.get("include_secrets"))
            return jsonify({"ok": True, "config": library_api_config_response(library_id, include_secrets=include_secrets)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/api-config")
    def api_save_library_api_config(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            existing = api_config_for_library(library_id)
            config = normalized_library_api_config(payload, existing)
            app_store.set_preference(library_id, API_CONFIG_PREFERENCE_KEY, config)
            return jsonify({"ok": True, "config": library_api_config_response(library_id, include_secrets=False)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/api-config/mineru")
    def api_save_library_mineru_config(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            existing = api_config_for_library(library_id)
            config = normalized_mineru_api_config(payload, existing)
            app_store.set_preference(library_id, API_CONFIG_PREFERENCE_KEY, config)
            return jsonify({"ok": True, "config": library_api_config_response(library_id, include_secrets=False)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/api-config/check")
    def api_check_library_api_config(library_id: str):
        payload = request.get_json(silent=True) or {}
        service = str(payload.get("service") or "").strip().lower()
        try:
            library_or_404(library_id)
            if service == "model":
                with use_ai_pixel_config(api_config_model_for_library(library_id)):
                    return jsonify({"ok": True, "service": service, "check": retrieval_model_health_check()})
            source_map = {"github": "github", "gitlab": "gitlab", "huggingface": "huggingface", "zenodo": "zenodo", "brave": "brave"}
            source_name = source_map.get(service)
            if not source_name:
                return jsonify({"ok": False, "error": "Unknown API config service."}), 400
            provider = retrieval_provider_registry_for_library(library_id)[source_name]
            candidates = provider.search(str(payload.get("query") or "robot"), limit=1)
            return jsonify(
                {
                    "ok": True,
                    "service": service,
                    "check": {
                        "ok": True,
                        "configured": bool(getattr(provider, "is_configured", lambda: False)()),
                        "count": len(candidates),
                        "message": f"{service} responded.",
                    },
                }
            )
        except (SourceError, RetrievalError, ValueError, OSError) as exc:
            return jsonify(
                {
                    "ok": True,
                    "service": service,
                    "check": {
                        "ok": False,
                        "error": str(exc),
                        "message": f"{service or 'service'} check failed.",
                    },
                }
            )

    @app.post("/api/library/<library_id>/preferences/columns")
    def api_columns(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        columns = payload.get("columns")
        if not isinstance(columns, list):
            return jsonify({"ok": False, "error": "columns must be a list"}), 400
        app_store.set_preference(library_id, "columns", [str(item) for item in columns if str(item)])
        return jsonify({"ok": True, "columns": app_store.column_preference(library_id)})

    @app.post("/api/library/<library_id>/preferences/column-widths")
    def api_column_widths(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        widths = payload.get("widths")
        if not isinstance(widths, dict):
            return jsonify({"ok": False, "error": "widths must be an object"}), 400
        app_store.set_preference(library_id, "column_widths", widths)
        return jsonify({"ok": True, "widths": app_store.column_width_preference(library_id)})

    @app.post("/api/library/<library_id>/preferences/plain-tags")
    def api_plain_tags_preference(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        collapsed = bool(payload.get("collapsed"))
        app_store.set_preference(library_id, "plain_tags_collapsed", collapsed)
        return jsonify({"ok": True, "collapsed": collapsed})

    @app.post("/api/library/<library_id>/collections")
    def api_create_collection(library_id: str):
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        parent_key = str(payload.get("parent_key") or "").strip() or None
        if not name:
            return jsonify({"ok": False, "error": "文件夹名称不能为空。"}), 400
        try:
            collection = ZoteroRepository(library_or_404(library_id)).create_collection(name, parent_key)
            return jsonify({"ok": True, "collection": collection})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/collections/<collection_key>")
    def api_rename_collection(library_id: str, collection_key: str):
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        try:
            repo = ZoteroRepository(library_or_404(library_id))
            if name:
                repo.rename_collection(collection_key, name)
            if "parent_key" in payload:
                parent_key = str(payload.get("parent_key") or "").strip() or None
                repo.reparent_collection(collection_key, parent_key)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/collections/<collection_key>")
    def api_delete_collection(library_id: str, collection_key: str):
        try:
            result = ZoteroRepository(library_or_404(library_id)).delete_collection(collection_key)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/delete")
    def api_delete_items(library_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys")
        mode = str(payload.get("mode") or "trash").strip()
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        try:
            result = ZoteroRepository(library_or_404(library_id)).delete_items([str(key) for key in item_keys], mode)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/move")
    def api_move_items(library_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys")
        target_collection_key = str(payload.get("target_collection_key") or "").strip()
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        if not target_collection_key:
            return jsonify({"ok": False, "error": "请选择目标文件夹。"}), 400
        try:
            result = ZoteroRepository(library_or_404(library_id)).move_items([str(key) for key in item_keys], target_collection_key)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/items/<item_key>/field")
    def api_update_item_field(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        field = str(payload.get("field") or "").strip()
        value = str(payload.get("value") or "")
        if not field:
            return jsonify({"ok": False, "error": "字段名不能为空。"}), 400
        try:
            ZoteroRepository(library_or_404(library_id)).update_item_field(item_key, field, value)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/items/<item_key>/structured-field")
    def api_update_structured_field(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        field = str(payload.get("field") or "").strip()
        value = str(payload.get("value") or "")
        if field not in {"remark", "title_zh", "abstract_zh"}:
            return jsonify({"ok": False, "error": "未知结构化字段。"}), 400
        try:
            ZoteroRepository(library_or_404(library_id)).update_structured_field(item_key, field, value)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/<item_key>/tags")
    def api_add_tag(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        tag = str(payload.get("tag") or "").strip()
        if not tag:
            return jsonify({"ok": False, "error": "标签不能为空。"}), 400
        try:
            ZoteroRepository(library_or_404(library_id)).add_tag(item_key, tag)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/items/<item_key>/tags")
    def api_remove_tag(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        tag = str(payload.get("tag") or "").strip()
        if not tag:
            return jsonify({"ok": False, "error": "标签不能为空。"}), 400
        try:
            ZoteroRepository(library_or_404(library_id)).remove_tag(item_key, tag)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/items/<item_key>/rating")
    def api_set_rating(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        try:
            value = int(payload.get("rating") or 0)
            ZoteroRepository(library_or_404(library_id)).set_rating(item_key, value)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/items/<item_key>/reading-status")
    def api_set_reading_status(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status") or "").strip()
        try:
            ZoteroRepository(library_or_404(library_id)).set_reading_status(item_key, status)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/<item_key>/collections")
    def api_item_collection(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        collection_key = str(payload.get("collection_key") or "").strip()
        enabled = bool(payload.get("enabled"))
        if not collection_key:
            return jsonify({"ok": False, "error": "collection_key is required"}), 400
        try:
            ZoteroRepository(library_or_404(library_id)).set_collection_membership(item_key, collection_key, enabled)
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/import-identifier")
    def api_import_identifier(library_id: str):
        payload = request.get_json(silent=True) or {}
        identifier = str(payload.get("identifier") or "").strip()
        collection_key = str(payload.get("collection_key") or "").strip() or None
        if not identifier:
            return jsonify({"ok": False, "error": "标识符不能为空。"}), 400
        try:
            repo = ZoteroRepository(library_or_404(library_id))
            summary = repo.import_metadata_items([resolve_identifier(identifier)], collection_key)
            return jsonify({"ok": True, **summary})
        except (SourceError, ValueError, MetadataImportError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/import-text")
    def api_import_text(library_id: str):
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or "")
        fmt = str(payload.get("format") or "auto").strip() or "auto"
        collection_key = str(payload.get("collection_key") or "").strip() or None
        if not text.strip():
            return jsonify({"ok": False, "error": "导入文本不能为空。"}), 400
        try:
            repo = ZoteroRepository(library_or_404(library_id))
            summary = repo.import_metadata_items(parse_import_text(text, fmt), collection_key)
            return jsonify({"ok": True, **summary})
        except (SourceError, ValueError, MetadataImportError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    def guided_search_request_from_payload(library_id: str, payload: dict[str, Any], *, default_route: str = "legacy") -> dict[str, Any]:
        route_was_provided = "search_route" in payload or "route" in payload
        search_route = normalize_retrieval_search_route(
            payload.get("search_route") or payload.get("route"),
            default="natural_language" if route_was_provided else default_route,
        )
        empty_agent_task = search_route == "agent" and payload.get("empty_task") is True
        topic = str(payload.get("topic") or payload.get("query") or payload.get("input_text") or "").strip()
        if not topic:
            if empty_agent_task:
                topic = "待确认研究需求"
            else:
                raise ValueError("检索主题不能为空。")
        library_or_404(library_id)
        mode = normalize_guided_search_mode(payload.get("mode"))
        time_range = normalize_guided_time_range(payload.get("time_range"), mode)
        material_types = normalize_guided_material_types(payload.get("material_types"))
        expansion_level = normalize_retrieval_expansion_level(payload.get("expansion_level"))
        language_policy = normalize_retrieval_language_policy(payload.get("language_policy"))
        registry = retrieval_provider_registry_for_library(library_id)
        source_names = [str(source or "").strip().lower() for source in payload.get("sources") or [] if str(source or "").strip()]
        unknown = [source for source in source_names if source not in registry]
        if unknown:
            raise ValueError(f"未知数据源：{', '.join(unknown)}")
        if not source_names:
            source_names = default_guided_sources_for_materials(registry, material_types)
        limit_per_source = normalize_guided_limit_per_source(payload.get("limit_per_source"), mode)
        source_limits = normalize_retrieval_source_limits(payload.get("source_limits"), source_names, fallback=limit_per_source)
        options = guided_search_options(
            mode,
            time_range,
            material_types,
            limit_per_source=limit_per_source,
            source_limits=source_limits,
        )
        options.update(
            {
                "search_route": search_route,
                "input_text": "" if empty_agent_task else topic,
                "empty_task": empty_agent_task,
                "planner_version": "v4" if search_route in {"keyword", "natural_language", "agent"} else "legacy",
                "expansion_level": expansion_level,
                "language_policy": language_policy,
            }
        )
        if search_route == "natural_language":
            with use_ai_pixel_config(api_config_model_for_library(library_id)):
                model_status = retrieval_model_status()
            if not model_status.get("configured"):
                raise ValueError("模型未配置，无法进行自然语言检索；请先配置模型，或切换为主题词检索。")
        return {
            "topic": topic,
            "mode": mode,
            "time_range": time_range,
            "material_types": material_types,
            "search_route": search_route,
            "expansion_level": expansion_level,
            "language_policy": language_policy,
            "source_names": source_names,
            "options": options,
            "limit_per_source": limit_per_source,
            "source_limits": source_limits,
            "empty_agent_task": empty_agent_task,
        }

    @app.post("/api/library/<library_id>/retrieval/guided-search-plan")
    def api_retrieval_guided_search_plan(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            parsed = guided_search_request_from_payload(library_id, payload, default_route="natural_language")
            if parsed["search_route"] == "agent":
                raise ValueError("智能体检索通过对话形成计划，请在智能体工作台发送消息。")
            plan = guided_search_plan_for_library(
                library_id,
                topic=parsed["topic"],
                mode=parsed["mode"],
                sources=parsed["source_names"],
                material_types=parsed["material_types"],
                use_ai_planning=parsed["search_route"] != "keyword",
                search_route=parsed["search_route"],
                input_text=parsed["topic"],
                expansion_level=parsed["expansion_level"],
                language_policy=parsed["language_policy"],
            )
            apply_guided_plan_limit(plan, parsed["limit_per_source"], parsed["source_limits"])
            return jsonify({"ok": True, "plan": plan})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs")
    def api_create_retrieval_guided_search_job(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            parsed = guided_search_request_from_payload(library_id, payload, default_route="legacy")
            use_ai_planning = payload.get("use_ai_planning") is not False and parsed["search_route"] != "keyword"
            job = app_store.create_retrieval_guided_job(
                library_id,
                topic=parsed["topic"],
                mode=parsed["mode"],
                time_range=parsed["time_range"],
                material_types=parsed["material_types"],
                sources=parsed["source_names"],
                options=parsed["options"],
                use_ai_planning=use_ai_planning,
            )
            plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
            if plan and isinstance(plan.get("queries"), list) and plan.get("queries"):
                apply_guided_plan_limit(plan, parsed["limit_per_source"], parsed["source_limits"])
                job = app_store.update_retrieval_guided_job(library_id, str(job.get("job_id") or ""), plan=plan)
            if parsed["search_route"] == "agent":
                state = default_agent_state()
                state["intent_model"] = merge_intent_model(
                    state.get("intent_model"),
                    {
                        "topic": "" if parsed["empty_agent_task"] else parsed["topic"],
                        "material_types": parsed["material_types"],
                        "time_range": parsed["time_range"],
                        "preferred_sources": parsed["source_names"],
                        "confidence": 0.0 if parsed["empty_agent_task"] else 0.15,
                    },
                )
                state["thread"]["thread_status"] = "waiting_user"
                state["updated_at"] = now_iso()
                retrieval_agent_store.upsert_session(
                    library_id,
                    str(job.get("job_id") or ""),
                    state,
                )
                job = app_store.update_retrieval_guided_job(
                    library_id,
                    str(job.get("job_id") or ""),
                    status="draft",
                    progress={
                        **(job.get("progress") if isinstance(job.get("progress"), dict) else {}),
                        "stage": "conversation",
                    },
                )
                app_store.append_retrieval_guided_event(
                    library_id,
                    str(job.get("job_id") or ""),
                    "智能体会话已创建，等待理解用户需求。",
                    kind="agent",
                )
            else:
                start_retrieval_guided_worker(library_id, str(job.get("job_id") or ""))
            return jsonify({"ok": True, "job": app_store.retrieval_guided_job(library_id, str(job.get("job_id") or ""))})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/guided-search-jobs/latest")
    def api_latest_retrieval_guided_search_job(library_id: str):
        try:
            library_or_404(library_id)
            search_route = str(request.args.get("search_route") or "").strip()
            if search_route:
                job = next(
                    (
                        item
                        for item in app_store.recent_retrieval_guided_jobs(library_id, limit=100)
                        if str(
                            item.get("search_route")
                            or (
                                item.get("options")
                                if isinstance(item.get("options"), dict)
                                else {}
                            ).get("search_route")
                            or ""
                        )
                        == search_route
                    ),
                    None,
                )
            else:
                job = app_store.latest_retrieval_guided_job(library_id)
            if job and job.get("status") in {"queued", "running"}:
                start_retrieval_guided_worker(library_id, str(job.get("job_id") or ""))
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>")
    def api_retrieval_guided_search_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.retrieval_guided_job(library_id, job_id)
            if job.get("status") in {"queued", "running"}:
                start_retrieval_guided_worker(library_id, str(job.get("job_id") or ""))
                job = app_store.retrieval_guided_job(library_id, job_id)
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.get("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/candidates")
    def api_retrieval_guided_search_candidates(library_id: str, job_id: str):
        try:
            limit = int(request.args.get("limit") or 200)
            use_ai_evaluation = request.args.get("use_ai_evaluation", "0").strip().lower() in {"1", "true", "yes"}
            result = guided_search_candidates_for_display(
                library_id,
                job_id,
                use_ai_evaluation=use_ai_evaluation,
                limit=limit,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/agent/status")
    def api_retrieval_agent_status(library_id: str):
        try:
            check_runtime = request.args.get("check", "").strip().lower() in {"1", "true", "yes"}
            return jsonify(
                {
                    "ok": True,
                    **retrieval_agent_status_for_library(
                        library_id,
                        check_runtime=check_runtime,
                    ),
                }
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/agent/state")
    def api_retrieval_agent_state(library_id: str, job_id: str):
        try:
            return jsonify(
                {
                    "ok": True,
                    **retrieval_agent_state_payload(
                        library_id,
                        job_id,
                        include_candidates=True,
                    ),
                }
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/agent-turns")
    def api_retrieval_agent_turn(library_id: str, job_id: str):
        payload = request.get_json(silent=True) or {}
        content = str(payload.get("message") or payload.get("content") or "").strip()
        if not content:
            return jsonify({"ok": False, "error": "请输入要告诉智能体的内容。"}), 400
        try:
            job = retrieval_agent_job(library_id, job_id)
            status = retrieval_agent_status_for_library(library_id)
            if not status.get("ready"):
                return jsonify({"ok": False, "error": status.get("message"), "agent_status": status}), 503
            active_turn = retrieval_agent_store.latest_turn(library_id, job_id)
            if active_turn and active_turn.get("status") in {"queued", "running"}:
                raise ValueError("智能体正在处理上一条消息，请稍候。")
            existing_messages = retrieval_agent_store.list_messages(library_id, job_id)
            job_options = job.get("options") if isinstance(job.get("options"), dict) else {}
            if not existing_messages and job_options.get("empty_task") is True:
                job = app_store.update_retrieval_guided_job(
                    library_id,
                    job_id,
                    topic=content,
                    options={
                        **job_options,
                        "input_text": content,
                        "empty_task": False,
                    },
                )
            message = retrieval_agent_store.add_message(
                library_id,
                job_id,
                "user",
                content,
                visibility="session_only",
            )
            turn = retrieval_agent_store.create_turn(
                library_id,
                job_id,
                str(message.get("message_id") or ""),
            )
            session = retrieval_agent_store.get_session(library_id, job_id)
            state = mark_turn_running(
                session.get("state") if session else default_agent_state(),
                str(turn["turn_id"]),
            )
            retrieval_agent_store.upsert_session(library_id, job_id, state)
            start_retrieval_agent_turn_worker(library_id, job_id, str(turn["turn_id"]))
            return (
                jsonify(
                    {
                        "ok": True,
                        "turn": retrieval_agent_store.get_turn(
                            library_id,
                            job_id,
                            str(turn["turn_id"]),
                        ),
                        "message": message,
                    }
                ),
                202,
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/agent/intent")
    def api_retrieval_agent_intent(library_id: str, job_id: str):
        payload = request.get_json(silent=True) or {}
        patch = payload.get("intent_patch") if isinstance(payload.get("intent_patch"), dict) else payload
        try:
            retrieval_agent_job(library_id, job_id)
            session = retrieval_agent_store.get_session(library_id, job_id)
            state = normalize_agent_state(session.get("state") if session else default_agent_state())
            state["intent_model"] = merge_intent_model(state.get("intent_model"), patch)
            state["thread"]["thread_status"] = "waiting_user"
            state["updated_at"] = now_iso()
            retrieval_agent_store.upsert_session(library_id, job_id, state)
            retrieval_agent_store.add_message(
                library_id,
                job_id,
                "system",
                "用户手动更新了当前理解。",
                metadata={"intent_patch": patch},
            )
            app_store.append_retrieval_guided_event(
                library_id,
                job_id,
                "用户手动更新了智能体当前理解。",
                kind="agent",
            )
            return jsonify({"ok": True, **retrieval_agent_state_payload(library_id, job_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/agent-approvals")
    def api_retrieval_agent_approval(library_id: str, job_id: str):
        payload = request.get_json(silent=True) or {}
        action_id = str(payload.get("action_id") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        if not action_id or decision not in {"approve", "reject"}:
            return jsonify({"ok": False, "error": "请提供 action_id 和 approve/reject。"}), 400
        try:
            job = retrieval_agent_job(library_id, job_id)
            session = retrieval_agent_store.get_session(library_id, job_id)
            state = normalize_agent_state(session.get("state") if session else default_agent_state())
            if decision == "reject":
                state, _ = update_pending_action(state, action_id, "rejected")
                retrieval_agent_store.upsert_session(library_id, job_id, state)
                retrieval_agent_store.add_message(
                    library_id,
                    job_id,
                    "system",
                    "用户暂不执行这轮联网检索计划。",
                    metadata={"action_id": action_id},
                )
                return jsonify({"ok": True, **retrieval_agent_state_payload(library_id, job_id)})
            if str(job.get("status") or "") in {"queued", "running"}:
                raise ValueError("当前联网检索仍在执行；可以继续聊天，待本轮完成后再确认补检。")
            budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
            if safe_int(budget.get("search_rounds_used")) >= safe_int(budget.get("max_search_rounds")):
                raise ValueError("已达到当前智能体任务的最大检索轮数。")
            state, action = update_pending_action(state, action_id, "approved")
            plan, options = retrieval_agent_plan_to_guided_plan(library_id, job, action, state)
            budget["search_rounds_used"] = safe_int(budget.get("search_rounds_used")) + 1
            state["budget"] = budget
            state["thread"]["thread_status"] = "searching"
            state["updated_at"] = now_iso()
            progress = {
                **(job.get("progress") if isinstance(job.get("progress"), dict) else {}),
                "stage": "queued",
                "total_queries": len(plan.get("queries") or []),
                "current_query": "",
            }
            app_store.update_retrieval_guided_job(
                library_id,
                job_id,
                status="queued",
                options=options,
                plan=plan,
                progress=progress,
                error="",
            )
            retrieval_agent_store.upsert_session(library_id, job_id, state)
            app_store.append_retrieval_guided_event(
                library_id,
                job_id,
                f"用户已确认第 {budget['search_rounds_used']} 轮联网检索。",
                kind="agent",
                data={"action_id": action_id},
            )
            start_retrieval_guided_worker(library_id, job_id)
            return jsonify({"ok": True, **retrieval_agent_state_payload(library_id, job_id)})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/agent/interrupt")
    def api_retrieval_agent_interrupt(library_id: str, job_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            job = retrieval_agent_job(library_id, job_id)
            turn = retrieval_agent_store.interrupt_active_turn(library_id, job_id)
            session = retrieval_agent_store.get_session(library_id, job_id)
            state = normalize_agent_state(session.get("state") if session else default_agent_state())
            state["thread"]["thread_status"] = "interrupted"
            state["thread"]["active_turn_id"] = ""
            state["thread"]["last_error"] = "用户中断了智能体处理。"
            state["updated_at"] = now_iso()
            retrieval_agent_store.upsert_session(library_id, job_id, state)
            if payload.get("scope") == "all" and str(job.get("status") or "") in {"queued", "running"}:
                app_store.cancel_retrieval_guided_job(library_id, job_id, "用户中断了智能体联网检索。")
            return jsonify({"ok": True, "turn": turn, **retrieval_agent_state_payload(library_id, job_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/candidate-feedback")
    def api_retrieval_agent_candidate_feedback(library_id: str, job_id: str):
        payload = request.get_json(silent=True) or {}
        candidate_id = str(payload.get("candidate_id") or "").strip()
        feedback_type = str(payload.get("feedback_type") or "").strip()
        try:
            job = retrieval_agent_job(library_id, job_id)
            candidates_result = guided_search_candidates_for_display(
                library_id,
                job_id,
                use_ai_evaluation=False,
                limit=500,
            )
            candidate_ids = {
                str(
                    candidate.get("stored_candidate_id")
                    or candidate.get("candidate_id")
                    or retrieval_batch_candidate_key(candidate)
                )
                for candidate in candidates_result.get("candidates") or []
                if isinstance(candidate, dict)
            }
            if candidate_id not in candidate_ids:
                raise ValueError("候选不存在或不属于当前智能体任务。")
            feedback = retrieval_agent_store.add_feedback(
                library_id,
                job_id,
                candidate_id,
                feedback_type,
                note=str(payload.get("note") or ""),
            )
            session = retrieval_agent_store.get_session(library_id, job_id)
            state = normalize_agent_state(session.get("state") if session else default_agent_state())
            state["feedback_summary"] = feedback_summary(
                retrieval_agent_store.list_feedback(library_id, job_id)
            )
            if feedback_type == "too_old":
                criteria = list((state.get("intent_model") or {}).get("quality_criteria") or [])
                if "优先较新的资料" not in criteria:
                    criteria.append("优先较新的资料")
                state["intent_model"] = merge_intent_model(
                    state.get("intent_model"),
                    {"quality_criteria": criteria},
                )
            elif feedback_type == "useful_code":
                criteria = list((state.get("intent_model") or {}).get("quality_criteria") or [])
                if "优先可复现代码" not in criteria:
                    criteria.append("优先可复现代码")
                state["intent_model"] = merge_intent_model(
                    state.get("intent_model"),
                    {"quality_criteria": criteria},
                )
            state["updated_at"] = now_iso()
            retrieval_agent_store.upsert_session(library_id, job_id, state)
            app_store.append_retrieval_guided_event(
                library_id,
                job_id,
                f"记录候选反馈：{feedback_type}。",
                kind="feedback",
                data={"candidate_id": candidate_id, "feedback_type": feedback_type},
            )
            return jsonify({"ok": True, "feedback": feedback, **retrieval_agent_state_payload(library_id, job_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/agent-memory")
    def api_retrieval_agent_memory(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "memory": retrieval_agent_store.list_memory(library_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/retrieval/agent-memory/<memory_id>")
    def api_update_retrieval_agent_memory(library_id: str, memory_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            status = payload.get("status")
            if "enabled" in payload:
                status = "enabled" if payload.get("enabled") is True else "disabled"
            memory = retrieval_agent_store.update_memory(
                library_id,
                memory_id,
                status=str(status) if status is not None else None,
                content=payload.get("content") if isinstance(payload.get("content"), dict) else None,
            )
            return jsonify({"ok": True, "memory": memory})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/retrieval/agent-memory/<memory_id>")
    def api_delete_retrieval_agent_memory(library_id: str, memory_id: str):
        try:
            library_or_404(library_id)
            deleted = retrieval_agent_store.delete_memory(library_id, memory_id)
            if not deleted:
                raise ValueError("记忆不存在。")
            return jsonify({"ok": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/cancel")
    def api_cancel_retrieval_guided_search_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.cancel_retrieval_guided_job(library_id, job_id, "引导式检索已取消。")
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/pause")
    def api_pause_retrieval_guided_search_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.pause_retrieval_guided_job(library_id, job_id, "引导式检索已暂停。")
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/guided-search-jobs/<job_id>/resume")
    def api_resume_retrieval_guided_search_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.resume_retrieval_guided_job(library_id, job_id)
            start_retrieval_guided_worker(library_id, str(job.get("job_id") or ""))
            return jsonify({"ok": True, "job": app_store.retrieval_guided_job(library_id, job_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/search")
    def api_retrieval_search(library_id: str):
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query") or "").strip()
        if not query:
            return jsonify({"ok": False, "error": "检索词不能为空。"}), 400
        try:
            library = library_or_404(library_id)
            limit = int(payload.get("limit") or 10)
            result = run_retrieval_search_for_library(
                library_id,
                query,
                payload.get("sources"),
                limit,
                include_raw=bool(payload.get("include_raw")),
                use_ai_evaluation=payload.get("use_ai_evaluation") is not False,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/search/jobs")
    def api_create_retrieval_search_job(library_id: str):
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query") or "").strip()
        if not query:
            return jsonify({"ok": False, "error": "检索词不能为空。"}), 400
        try:
            library_or_404(library_id)
            limit = int(payload.get("limit") or 10)
            sources = payload.get("sources")
            source_names = [str(source or "").strip().lower() for source in sources or [] if str(source or "").strip()]
            job_id = retrieval_background_job_id("search")
            now = now_iso()
            job = {
                "job_id": job_id,
                "library_id": library_id,
                "type": "search",
                "query": query,
                "sources": source_names,
                "limit": limit,
                "include_raw": bool(payload.get("include_raw")),
                "use_ai_evaluation": payload.get("use_ai_evaluation") is not False,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
                "message": "快速检索已进入后台队列。",
                "error": "",
                "stop_requested": False,
                "candidate_count": 0,
                "run_id": "",
                "result": None,
            }
            with RETRIEVAL_BACKGROUND_LOCK:
                RETRIEVAL_SEARCH_JOBS[job_id] = job
                trim_retrieval_background_jobs(RETRIEVAL_SEARCH_JOBS, library_id)
            start_retrieval_search_worker(library_id, job_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                snapshot = retrieval_job_snapshot(RETRIEVAL_SEARCH_JOBS.get(job_id))
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/search/jobs/latest")
    def api_latest_retrieval_search_job(library_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = retrieval_job_snapshot(latest_retrieval_background_job(RETRIEVAL_SEARCH_JOBS, library_id))
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/search/jobs/<job_id>")
    def api_retrieval_search_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_SEARCH_JOBS.get(job_id)
                if not job or job.get("library_id") != library_id:
                    raise ValueError("快速检索任务不存在。")
                snapshot = retrieval_job_snapshot(job)
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/library/<library_id>/retrieval/candidates/evaluate")
    def api_retrieval_candidates_evaluate(library_id: str):
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query") or "").strip()
        candidates = payload.get("candidates")
        if not query:
            return jsonify({"ok": False, "error": "检索词不能为空。"}), 400
        if not isinstance(candidates, list) or not candidates:
            return jsonify({"ok": False, "error": "没有可评分的候选。"}), 400
        try:
            library_or_404(library_id)
            candidate_payloads = [dict(candidate) for candidate in candidates if isinstance(candidate, dict)]
            if not candidate_payloads:
                return jsonify({"ok": False, "error": "没有可评分的候选。"}), 400
            requested_limit = safe_int(payload.get("max_candidates")) or AI_CANDIDATE_MANUAL_EVALUATION_LIMIT
            max_candidates = max(1, min(requested_limit, AI_CANDIDATE_MANUAL_EVALUATION_LIMIT))
            ai_candidate_payloads = candidate_payloads[:max_candidates]
            rule_candidate_payloads = candidate_payloads[max_candidates:]
            summary = evaluate_retrieval_candidates_with_ai(
                library_id,
                query,
                ai_candidate_payloads,
                use_ai_evaluation=True,
            )
            for candidate in rule_candidate_payloads:
                candidate["ai_evaluation"] = deterministic_candidate_evaluation(
                    candidate,
                    query,
                    status="skipped",
                    reason=f"AI 推荐排序已精评前 {len(ai_candidate_payloads)} 条；该候选保留规则评分。",
                )
            candidate_payloads = sort_candidates_by_ai_evaluation(ai_candidate_payloads + rule_candidate_payloads)
            summary["candidate_count"] = len(candidate_payloads)
            summary["ai_candidate_limit"] = max_candidates
            summary["ai_evaluated_candidate_count"] = len(ai_candidate_payloads)
            summary["skipped_candidate_count"] = len(rule_candidate_payloads)
            if rule_candidate_payloads and summary.get("score_source") == "ai_model":
                summary["status"] = "partial"
                summary["score_source"] = "mixed_ai_rules"
                summary["partial_reason"] = "candidate_limit"
            summary["decision_counts"] = ai_evaluation_decision_counts(candidate_payloads)
            summary["auto_selected_count"] = sum(
                1 for candidate in candidate_payloads if candidate.get("ai_evaluation", {}).get("auto_select")
            )
            return jsonify({"ok": True, "candidates": candidate_payloads, "ai_evaluation_summary": summary})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/ai-scoring-jobs")
    def api_create_retrieval_ai_scoring_job(library_id: str):
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query") or "").strip()
        candidates = payload.get("candidates")
        if not query:
            return jsonify({"ok": False, "error": "检索词不能为空。"}), 400
        if not isinstance(candidates, list) or not candidates:
            return jsonify({"ok": False, "error": "没有可评分的候选。"}), 400
        try:
            library_or_404(library_id)
            with use_ai_pixel_config(api_config_model_for_library(library_id)):
                model_status = retrieval_model_status()
            if not model_status.get("configured"):
                raise ValueError("模型未配置，无法启动 AI 推荐排序。")
            candidate_payloads = [copy.deepcopy(candidate) for candidate in candidates if isinstance(candidate, dict)]
            if not candidate_payloads:
                raise ValueError("没有可评分的候选。")
            candidate_payloads.sort(key=retrieval_candidate_rule_confidence_value, reverse=True)
            for index, candidate in enumerate(candidate_payloads, start=1):
                candidate["client_key"] = retrieval_candidate_job_key(candidate, index)
                candidate["rank"] = index
            job_id = retrieval_background_job_id("ai-score")
            job = {
                "job_id": job_id,
                "library_id": library_id,
                "type": "ai_scoring",
                "query": query,
                "status": "queued",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "started_at": "",
                "finished_at": "",
                "message": "AI 推荐排序已进入后台队列。",
                "error": "",
                "stop_requested": False,
                "model_status": model_status,
                "candidates": candidate_payloads,
                "queue_keys": [str(candidate.get("client_key") or "") for candidate in candidate_payloads],
                "total_count": len(candidate_payloads),
                "completed_count": 0,
                "ai_completed_count": 0,
                "failed_count": 0,
                "current_candidate_key": "",
                "current_candidate_title": "",
            }
            job["summary"] = retrieval_ai_scoring_job_summary(job)
            with RETRIEVAL_BACKGROUND_LOCK:
                RETRIEVAL_AI_SCORING_JOBS[job_id] = job
                trim_retrieval_background_jobs(RETRIEVAL_AI_SCORING_JOBS, library_id)
            start_retrieval_ai_scoring_worker(library_id, job_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                snapshot = retrieval_job_snapshot(RETRIEVAL_AI_SCORING_JOBS.get(job_id))
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/ai-scoring-jobs/latest")
    def api_latest_retrieval_ai_scoring_job(library_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = retrieval_job_snapshot(latest_retrieval_background_job(RETRIEVAL_AI_SCORING_JOBS, library_id))
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/ai-scoring-jobs/<job_id>")
    def api_retrieval_ai_scoring_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                if not job or job.get("library_id") != library_id:
                    raise ValueError("AI 推荐排序任务不存在。")
                snapshot = retrieval_job_snapshot(job)
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/library/<library_id>/retrieval/ai-scoring-jobs/<job_id>/cancel")
    def api_cancel_retrieval_ai_scoring_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_AI_SCORING_JOBS.get(job_id)
                if not job or job.get("library_id") != library_id:
                    raise ValueError("AI 推荐排序任务不存在。")
                status = str(job.get("status") or "")
                if status in {"queued", "running", "canceling"}:
                    job["stop_requested"] = True
                    job["status"] = "canceling"
                    job["message"] = "正在停止 AI 推荐排序..."
                    job["updated_at"] = now_iso()
                    job["summary"] = retrieval_ai_scoring_job_summary(job)
                snapshot = retrieval_job_snapshot(job)
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/library/<library_id>/retrieval/import")
    def api_retrieval_import(library_id: str):
        payload = request.get_json(silent=True) or {}
        collection_key = str(payload.get("collection_key") or "").strip() or None
        run_id = str(payload.get("run_id") or "").strip()
        try:
            repo = ZoteroRepository(library_or_404(library_id))
            candidate_ids = payload.get("candidate_ids")
            if isinstance(candidate_ids, list) and candidate_ids:
                candidates = app_store.retrieval_candidates_for_import(library_id, run_id, [str(candidate_id) for candidate_id in candidate_ids])
            else:
                candidates = payload.get("candidates")
            metadata_items = imported_items_from_candidates(candidates)
            summary = repo.import_metadata_items(metadata_items, collection_key)
            results = summary.get("results", [])
            app_store.record_import_provenance(library_id, run_id, candidates, results)
            evidence = retrieval_import_evidence(library_id, run_id, candidates, results)
            return jsonify({"ok": True, **summary, "import_evidence": evidence})
        except (SourceError, CandidateImportError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/runs")
    def api_retrieval_runs(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "runs": app_store.recent_retrieval_runs(library_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/batches")
    def api_create_retrieval_batch(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            queries = normalize_retrieval_batch_queries(payload.get("queries") or payload.get("query_text") or payload.get("text") or "")
            limit = max(1, min(int(payload.get("limit") or payload.get("limit_per_query") or 10), 50))
            sources = payload.get("sources")
            registry = retrieval_provider_registry_for_library(library_id)
            source_names = [str(source or "").strip().lower() for source in sources or [] if str(source or "").strip()]
            unknown = [source for source in source_names if source not in registry]
            if unknown:
                raise ValueError(f"unknown retrieval sources: {', '.join(unknown)}")
            if not source_names:
                source_names = list(registry)
            source_limits = normalize_retrieval_source_limits(payload.get("source_limits"), source_names, fallback=limit)
            context = retrieval_batch_context_for_library(library_id)
            if source_limits:
                context["source_limits"] = source_limits
            job = app_store.create_retrieval_batch_job(
                library_id,
                queries,
                source_names,
                limit,
                context=context,
            )
            start_retrieval_batch_worker(library_id, job["job_id"])
            return jsonify({"ok": True, "job": app_store.retrieval_batch_job(library_id, job["job_id"])})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/batches")
    def api_retrieval_batches(library_id: str):
        try:
            library_or_404(library_id)
            limit = int(request.args.get("limit") or 20)
            return jsonify({"ok": True, "jobs": app_store.recent_retrieval_batch_jobs(library_id, limit=limit)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/batches/<job_id>")
    def api_retrieval_batch(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "job": app_store.retrieval_batch_job(library_id, job_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/batches/<job_id>/candidates")
    def api_retrieval_batch_candidates(library_id: str, job_id: str):
        try:
            limit = int(request.args.get("limit") or 100)
            use_ai_evaluation = request.args.get("use_ai_evaluation", "1").strip().lower() not in {"0", "false", "no"}
            result = retrieval_batch_candidates_for_display(
                library_id,
                job_id,
                use_ai_evaluation=use_ai_evaluation,
                limit=limit,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/batches/<job_id>/report")
    def api_retrieval_batch_report(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            scope = normalize_retrieval_batch_report_scope(str(request.args.get("scope") or "queries"))
            report = retrieval_batch_report(app_store.retrieval_batch_job(library_id, job_id))
            content = render_retrieval_batch_report(report, fmt, scope)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Disposition": f"attachment; filename={retrieval_batch_report_filename(job_id, fmt, scope)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/batches/<job_id>/cancel")
    def api_cancel_retrieval_batch(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.cancel_retrieval_batch_job(library_id, job_id, "Batch retrieval canceled by cjh.")
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/batches/<job_id>/pause")
    def api_pause_retrieval_batch(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.pause_retrieval_batch_job(library_id, job_id, "Batch retrieval paused by cjh.")
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/batches/<job_id>/resume")
    def api_resume_retrieval_batch(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.resume_retrieval_batch_job(library_id, job_id)
            start_retrieval_batch_worker(library_id, job["job_id"])
            return jsonify({"ok": True, "job": app_store.retrieval_batch_job(library_id, job["job_id"])})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/batches/<job_id>/retry-failed")
    def api_retry_failed_retrieval_batch(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            job = app_store.retry_failed_retrieval_batch_job(library_id, job_id)
            start_retrieval_batch_worker(library_id, job["job_id"])
            return jsonify({"ok": True, "job": app_store.retrieval_batch_job(library_id, job["job_id"])})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/summary")
    def api_retrieval_summary(library_id: str):
        try:
            library_or_404(library_id)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            return jsonify({"ok": True, "summary": app_store.retrieval_run_summary(library_id, limit=limit)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/summary/report")
    def api_retrieval_summary_report(library_id: str):
        try:
            library_or_404(library_id)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            summary = app_store.retrieval_run_summary(library_id, limit=limit)
            content = render_retrieval_summary_report(summary, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_summary_report_filename(fmt)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/tuning")
    def api_retrieval_tuning(library_id: str):
        try:
            library_or_404(library_id)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            summary = app_store.retrieval_run_summary(library_id, limit=limit)
            sources = retrieval_source_statuses(registry=retrieval_provider_registry_for_library(library_id))
            return jsonify({"ok": True, "tuning": retrieval_tuning_report(summary, sources)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/tuning/report")
    def api_retrieval_tuning_report(library_id: str):
        try:
            library_or_404(library_id)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            summary = app_store.retrieval_run_summary(library_id, limit=limit)
            sources = retrieval_source_statuses(registry=retrieval_provider_registry_for_library(library_id))
            report = retrieval_tuning_report(summary, sources)
            content = render_retrieval_tuning_report(report, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_tuning_report_filename(fmt)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/config-bundle")
    def api_retrieval_config_bundle(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "bundle": retrieval_config_bundle_for_library(library_id, redact=True)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/config-bundle/download")
    def api_retrieval_config_bundle_download(library_id: str):
        try:
            library_or_404(library_id)
            bundle = retrieval_config_bundle_for_library(library_id, redact=True)
            content = json.dumps(bundle, ensure_ascii=False, indent=2)
            return Response(
                content,
                mimetype="application/json",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_config_bundle_filename()}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/config-bundle")
    def api_import_retrieval_config_bundle(library_id: str):
        payload = request.get_json(silent=True) or {}
        if request.args.get("dry_run") is not None and "dry_run" not in payload:
            payload = {**payload, "dry_run": request.args.get("dry_run")}
        if request.args.get("allow_redacted") is not None and "allow_redacted" not in payload:
            payload = {**payload, "allow_redacted": request.args.get("allow_redacted")}
        try:
            library_or_404(library_id)
            result = apply_retrieval_config_bundle(library_id, payload)
            return jsonify({"ok": True, **result})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/rehearsal/setup")
    def api_setup_retrieval_rehearsal(library_id: str):
        payload = request.get_json(silent=True) or {}
        replace_value = payload.get("replace_existing")
        if request.args.get("replace_existing") is not None and "replace_existing" not in payload:
            replace_value = request.args.get("replace_existing")
        try:
            library_or_404(library_id)
            result = setup_retrieval_rehearsal_for_library(
                library_id,
                replace_existing=truthy_query_flag(replace_value),
            )
            if not result.get("applied"):
                return jsonify({"ok": False, **result}), 409
            return jsonify({"ok": True, **result})
        except (SourceError, RetrievalError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/rehearsal/validate")
    def api_validate_retrieval_rehearsal(library_id: str):
        payload = request.get_json(silent=True) or {}
        replace_value = payload.get("replace_existing")
        if request.args.get("replace_existing") is not None and "replace_existing" not in payload:
            replace_value = request.args.get("replace_existing")
        sample_value = payload.get("sample_size", request.args.get("sample_size"))
        limit_value = payload.get("limit", request.args.get("limit"))
        summary_limit_value = payload.get("summary_limit", request.args.get("summary_limit"))
        try:
            library_or_404(library_id)
            sample_size = bounded_retrieval_sample_size(sample_value, default=2)
            batch_limit = max(1, min(int(limit_value or 5), 50))
            summary_limit = bounded_retrieval_summary_limit(summary_limit_value, default=100)
            setup = setup_retrieval_rehearsal_for_library(
                library_id,
                replace_existing=truthy_query_flag(replace_value),
            )
            if not setup.get("applied"):
                return jsonify({"ok": False, **setup}), 409
            kit = setup.get("kit") if isinstance(setup.get("kit"), dict) else {}
            seed_queries = normalize_retrieval_batch_queries(kit.get("queries") or [])
            query = seed_queries[0]
            plan_sample_size = max(sample_size, 5)
            query_plan = retrieval_query_plan_for_library(
                library_id,
                seed_query=query,
                sample_size=plan_sample_size,
                limit=5,
            )
            planned_queries = normalize_retrieval_batch_queries(
                [
                    item.get("query")
                    for item in query_plan.get("queries") or []
                    if isinstance(item, dict)
                ]
            )
            queries = planned_queries or seed_queries
            source_names = list(RETRIEVAL_REHEARSAL_SOURCES)
            readiness = retrieval_readiness_report_for_library(
                library_id,
                query=query,
                sample_size=sample_size,
                include_health=False,
            )
            job = app_store.create_retrieval_batch_job(
                library_id,
                queries,
                source_names,
                batch_limit,
                context=retrieval_batch_context_for_library(library_id),
            )
            start_retrieval_batch_worker(library_id, job["job_id"])
            job = app_store.retrieval_batch_job(library_id, job["job_id"])
            onboarding = retrieval_onboarding_report_for_library(
                library_id,
                query=query,
                sample_size=plan_sample_size,
                include_health=False,
                limit=summary_limit,
            )
            artifacts = retrieval_rehearsal_validation_artifacts(
                job["job_id"],
                query=query,
                sample_size=plan_sample_size,
                limit=summary_limit,
            )
            validation_summary = retrieval_rehearsal_validation_evidence(
                setup,
                readiness,
                job,
                onboarding,
                artifacts,
                queries=queries,
                sources=source_names,
            )
            completed = safe_int(job.get("completed_queries"))
            total = safe_int(job.get("total_queries"))
            return jsonify(
                {
                    "ok": True,
                    "setup": setup,
                    "kit": kit,
                    "seed_queries": seed_queries,
                    "query_plan": query_plan,
                    "queries": queries,
                    "sources": source_names,
                    "sample_size": sample_size,
                    "query_plan_sample_size": plan_sample_size,
                    "batch_limit": batch_limit,
                    "summary_limit": summary_limit,
                    "readiness": readiness,
                    "job": job,
                    "onboarding": onboarding,
                    "artifacts": artifacts,
                    "validation_summary": validation_summary,
                    "validation_gates": validation_summary["gates"],
                    "message": (
                        f"Rehearsal validation started: READY {readiness.get('status')}; "
                        f"batch {job.get('status')} {completed}/{total}; "
                        f"ONB {onboarding.get('status')}."
                    ),
                    "next_steps": [
                        "Open Batch report and Source CSV for per-query evidence.",
                        "Download ONB report or ONB ZIP for handoff evidence.",
                    ],
                }
            )
        except (SourceError, RetrievalError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/field-map/targets")
    def api_retrieval_field_map_targets(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "targets": retrieval_field_map_targets()})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/model-status")
    def api_retrieval_model_status(library_id: str):
        try:
            library_or_404(library_id)
            with use_ai_pixel_config(api_config_model_for_library(library_id)):
                model = retrieval_model_status()
                check = str(request.args.get("check") or "").strip().casefold() in {"1", "true", "yes", "on"}
                if check:
                    model["health"] = retrieval_model_health_check()
            return jsonify({"ok": True, "model": model})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/field-map/suggest")
    def api_retrieval_field_map_suggest(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            with use_ai_pixel_config(api_config_model_for_library(library_id)):
                suggestion = retrieval_field_map_suggestion_from_payload(payload)
            return jsonify({"ok": True, **suggestion})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/field-map/report")
    def api_retrieval_field_map_report(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            with use_ai_pixel_config(api_config_model_for_library(library_id)):
                suggestion = dict(retrieval_field_map_suggestion_from_payload(payload))
            suggestion.setdefault("schema", "web-library.retrieval-field-map-report/v1")
            suggestion.setdefault("generated_at", now_iso())
            content = render_retrieval_field_map_report(suggestion, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_field_map_report_filename(fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/source-intake")
    def api_retrieval_source_intake(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            return jsonify(
                {
                    "ok": True,
                    "intake": retrieval_source_intake_for_library(library_id, payload),
                }
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/source-intake/report")
    def api_retrieval_source_intake_report(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            intake = retrieval_source_intake_for_library(library_id, payload)
            content = render_retrieval_source_intake_report(intake, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_source_intake_report_filename(fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/local-files")
    def api_retrieval_local_files(library_id: str):
        try:
            library_or_404(library_id)
            config, source = local_retrieval_config_for_library(library_id)
            paths = [str(path).strip() for path in config.get("paths") or [] if str(path).strip()]
            field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
            return jsonify(
                {
                    "ok": True,
                    "paths": paths,
                    "field_map": field_map,
                    "source": source,
                    "status": local_retrieval_path_status(paths, field_map),
                }
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/local-files")
    def api_update_retrieval_local_files(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            config = normalize_local_retrieval_config_payload(payload)
            paths = [str(path).strip() for path in config.get("paths") or [] if str(path).strip()]
            field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
            if paths:
                configured_local_file_paths(paths)
            stored_config = app_store.set_retrieval_local_config(library_id, {"paths": paths, "field_map": field_map})
            stored_paths = stored_config["paths"]
            stored_field_map = stored_config["field_map"]
            return jsonify(
                {
                    "ok": True,
                    "paths": stored_paths,
                    "field_map": stored_field_map,
                    "source": "preference",
                    "status": local_retrieval_path_status(stored_paths, stored_field_map),
                }
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/local-files/preview")
    def api_retrieval_local_file_preview(library_id: str):
        try:
            library_or_404(library_id)
            config, source = local_retrieval_config_for_library(library_id)
            paths = [str(path).strip() for path in config.get("paths") or [] if str(path).strip()]
            field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
            sample_size = max(1, min(int(request.args.get("sample_size") or 2), 5))
            preview = preview_local_file_mappings(paths, sample_size=sample_size, field_map=field_map)
            return jsonify({"ok": True, "paths": paths, "field_map": field_map, "source": source, "preview": preview})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/local-files/field-map/suggest")
    def api_retrieval_local_file_field_map_suggest(library_id: str):
        try:
            library_or_404(library_id)
            config, source = local_retrieval_config_for_library(library_id)
            paths = [str(path).strip() for path in config.get("paths") or [] if str(path).strip()]
            field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = suggest_local_file_field_map(
                paths,
                sample_size=sample_size,
                field_map=field_map,
                replace_existing=replace_existing,
            )
            return jsonify(
                {
                    "ok": True,
                    "paths": paths,
                    "field_map": field_map,
                    "source": source,
                    "suggestion": field_map_suggestion_response_for_source(source, suggestion),
                }
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/local-files/field-map/report")
    def api_retrieval_local_file_field_map_report(library_id: str):
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            config, source = local_retrieval_config_for_library(library_id)
            paths = [str(path).strip() for path in config.get("paths") or [] if str(path).strip()]
            field_map = config.get("field_map") if isinstance(config.get("field_map"), dict) else {}
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = field_map_suggestion_response_for_source(
                source,
                suggest_local_file_field_map(
                    paths,
                    sample_size=sample_size,
                    field_map=field_map,
                    replace_existing=replace_existing,
                ),
            )
            suggestion = {
                "schema": "web-library.retrieval-field-map-report/v1",
                "generated_at": now_iso(),
                "source_config_source": source,
                "sample_size": sample_size,
                **suggestion,
            }
            content = render_retrieval_field_map_report(suggestion, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_source_field_map_report_filename('local-files', fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/http-json")
    def api_retrieval_http_json(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = http_json_config_for_library(library_id)
            summary = http_json_config_summary(raw_config)
            config_text = ""
            if source == "preference" and summary["configured"]:
                config_text = json.dumps(http_json_config(raw_config), ensure_ascii=False, indent=2)
            return jsonify(
                {
                    "ok": True,
                    "source": source,
                    "config": config_text,
                    "summary": summary,
                }
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/http-json")
    def api_update_retrieval_http_json(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            config = normalize_http_json_config_payload(payload)
            stored_config = app_store.set_retrieval_http_json_config(library_id, config)
            summary = http_json_config_summary(stored_config)
            config_text = json.dumps(http_json_config(stored_config), ensure_ascii=False, indent=2) if summary["configured"] else ""
            return jsonify(
                {
                    "ok": True,
                    "source": "preference",
                    "config": config_text,
                    "summary": summary,
                }
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/http-json/templates")
    def api_retrieval_http_json_templates(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "templates": http_json_config_templates()})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/http-json/preview")
    def api_retrieval_http_json_preview(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = http_json_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = max(1, min(int(request.args.get("sample_size") or 2), 5))
            preview = preview_http_json_mappings(raw_config, query=query, sample_size=sample_size)
            return jsonify({"ok": True, "source": source, "preview": preview})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/http-json/field-map/suggest")
    def api_retrieval_http_json_field_map_suggest(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = http_json_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = suggest_http_json_field_map(
                raw_config,
                query=query,
                sample_size=sample_size,
                replace_existing=replace_existing,
            )
            return jsonify({"ok": True, "source": source, "suggestion": field_map_suggestion_response_for_source(source, suggestion)})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/http-json/field-map/report")
    def api_retrieval_http_json_field_map_report(library_id: str):
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            raw_config, source = http_json_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = field_map_suggestion_response_for_source(
                source,
                suggest_http_json_field_map(
                    raw_config,
                    query=query,
                    sample_size=sample_size,
                    replace_existing=replace_existing,
                ),
            )
            suggestion = {
                "schema": "web-library.retrieval-field-map-report/v1",
                "generated_at": now_iso(),
                "source_config_source": source,
                "query": query,
                "sample_size": sample_size,
                **suggestion,
            }
            content = render_retrieval_field_map_report(suggestion, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_source_field_map_report_filename('http-json', fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sqlite")
    def api_retrieval_sqlite(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = sqlite_config_for_library(library_id)
            summary = sqlite_config_summary(raw_config)
            config_text = ""
            if source == "preference" and summary["configured"]:
                config_text = json.dumps(sqlite_config(raw_config), ensure_ascii=False, indent=2)
            return jsonify({"ok": True, "source": source, "config": config_text, "summary": summary})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/sqlite")
    def api_update_retrieval_sqlite(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            config = normalize_sqlite_config_payload(payload)
            stored_config = app_store.set_retrieval_sqlite_config(library_id, config)
            summary = sqlite_config_summary(stored_config)
            config_text = json.dumps(sqlite_config(stored_config), ensure_ascii=False, indent=2) if summary["configured"] else ""
            return jsonify({"ok": True, "source": "preference", "config": config_text, "summary": summary})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sqlite/templates")
    def api_retrieval_sqlite_templates(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "templates": sqlite_config_templates()})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sqlite/preview")
    def api_retrieval_sqlite_preview(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = sqlite_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = max(1, min(int(request.args.get("sample_size") or 2), 5))
            preview = preview_sqlite_mappings(raw_config, query=query, sample_size=sample_size)
            return jsonify({"ok": True, "source": source, "preview": preview})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sqlite/field-map/suggest")
    def api_retrieval_sqlite_field_map_suggest(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = sqlite_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = suggest_sqlite_field_map(
                raw_config,
                query=query,
                sample_size=sample_size,
                replace_existing=replace_existing,
            )
            return jsonify({"ok": True, "source": source, "suggestion": field_map_suggestion_response_for_source(source, suggestion)})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sqlite/field-map/report")
    def api_retrieval_sqlite_field_map_report(library_id: str):
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            raw_config, source = sqlite_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = field_map_suggestion_response_for_source(
                source,
                suggest_sqlite_field_map(
                    raw_config,
                    query=query,
                    sample_size=sample_size,
                    replace_existing=replace_existing,
                ),
            )
            suggestion = {
                "schema": "web-library.retrieval-field-map-report/v1",
                "generated_at": now_iso(),
                "source_config_source": source,
                "query": query,
                "sample_size": sample_size,
                **suggestion,
            }
            content = render_retrieval_field_map_report(suggestion, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_source_field_map_report_filename('sqlite', fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/manifest")
    def api_retrieval_manifest(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = manifest_config_for_library(library_id)
            summary = manifest_config_summary(raw_config)
            config_text = ""
            if source == "preference" and summary["configured"]:
                config_text = json.dumps(manifest_config(raw_config), ensure_ascii=False, indent=2)
            return jsonify({"ok": True, "source": source, "config": config_text, "summary": summary})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/manifest")
    def api_update_retrieval_manifest(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            config = normalize_manifest_config_payload(payload)
            stored_config = app_store.set_retrieval_manifest_config(library_id, config)
            summary = manifest_config_summary(stored_config)
            config_text = json.dumps(manifest_config(stored_config), ensure_ascii=False, indent=2) if summary["configured"] else ""
            return jsonify({"ok": True, "source": "preference", "config": config_text, "summary": summary})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/manifest/templates")
    def api_retrieval_manifest_templates(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "templates": manifest_config_templates()})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/manifest/preview")
    def api_retrieval_manifest_preview(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = manifest_config_for_library(library_id)
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = max(1, min(int(request.args.get("sample_size") or 2), 5))
            preview = preview_manifest_mappings(raw_config, query=query, sample_size=sample_size)
            return jsonify({"ok": True, "source": source, "preview": preview})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/manifest/field-map/suggest")
    def api_retrieval_manifest_field_map_suggest(library_id: str):
        try:
            library_or_404(library_id)
            raw_config, source = manifest_config_for_library(library_id)
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = suggest_manifest_field_map(
                raw_config,
                sample_size=sample_size,
                replace_existing=replace_existing,
            )
            return jsonify({"ok": True, "source": source, "suggestion": field_map_suggestion_response_for_source(source, suggestion)})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/manifest/field-map/report")
    def api_retrieval_manifest_field_map_report(library_id: str):
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            raw_config, source = manifest_config_for_library(library_id)
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=3)
            replace_existing = replace_existing_field_map_default(request.args.get("replace_existing"))
            suggestion = field_map_suggestion_response_for_source(
                source,
                suggest_manifest_field_map(
                    raw_config,
                    sample_size=sample_size,
                    replace_existing=replace_existing,
                ),
            )
            suggestion = {
                "schema": "web-library.retrieval-field-map-report/v1",
                "generated_at": now_iso(),
                "source_config_source": source,
                "sample_size": sample_size,
                **suggestion,
            }
            content = render_retrieval_field_map_report(suggestion, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_source_field_map_report_filename('manifest', fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/custom-sources")
    def api_retrieval_custom_sources(library_id: str):
        try:
            library_or_404(library_id)
            return jsonify({"ok": True, "sources": app_store.list_retrieval_custom_sources(library_id)})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/custom-sources")
    def api_create_retrieval_custom_source(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            normalized = normalize_custom_source_payload(payload)
            source = app_store.upsert_retrieval_custom_source(library_id, normalized)
            return jsonify({"ok": True, "source": source})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/custom-sources/<source_id>")
    def api_retrieval_custom_source(library_id: str, source_id: str):
        try:
            library_or_404(library_id)
            source = app_store.get_retrieval_custom_source(library_id, source_id)
            if not source:
                return jsonify({"ok": False, "error": "custom source does not exist"}), 404
            return jsonify({"ok": True, "source": source})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/retrieval/custom-sources/<source_id>")
    def api_update_retrieval_custom_source(library_id: str, source_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            existing = app_store.get_retrieval_custom_source(library_id, source_id)
            if not existing:
                return jsonify({"ok": False, "error": "custom source does not exist"}), 404
            normalized = normalize_custom_source_payload({**payload, "source_id": source_id}, existing=existing)
            source = app_store.upsert_retrieval_custom_source(library_id, normalized)
            return jsonify({"ok": True, "source": source})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/retrieval/custom-sources/<source_id>")
    def api_delete_retrieval_custom_source(library_id: str, source_id: str):
        try:
            library_or_404(library_id)
            deleted = app_store.delete_retrieval_custom_source(library_id, source_id)
            if not deleted:
                return jsonify({"ok": False, "error": "custom source does not exist"}), 404
            return jsonify({"ok": True, "deleted": True})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/custom-sources/<source_id>/preview")
    def api_preview_retrieval_custom_source(library_id: str, source_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            source = app_store.get_retrieval_custom_source(library_id, source_id)
            if not source:
                return jsonify({"ok": False, "error": "custom source does not exist"}), 404
            query = str(payload.get("query") or request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(payload.get("sample_size") or request.args.get("sample_size"), default=2)
            result = custom_source_check_result(source, query=query, limit=sample_size)
            return jsonify({"ok": True, "preview": result})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/custom-sources/<source_id>/check")
    def api_check_retrieval_custom_source(library_id: str, source_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            source = app_store.get_retrieval_custom_source(library_id, source_id)
            if not source:
                return jsonify({"ok": False, "error": "custom source does not exist"}), 404
            query = str(payload.get("query") or request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(payload.get("sample_size") or request.args.get("sample_size"), default=2)
            result = custom_source_check_result(source, query=query, limit=sample_size)
            stored = app_store.update_retrieval_custom_source_status(library_id, source_id, result, checked=True)
            return jsonify({"ok": True, "check": result, "source": stored})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sources")
    def api_retrieval_sources(library_id: str):
        try:
            library_or_404(library_id)
            include_health = str(request.args.get("check") or "").strip().lower() in {"1", "true", "yes", "health"}
            return jsonify(
                {
                    "ok": True,
                    "sources": retrieval_source_statuses(
                        registry=retrieval_provider_registry_for_library(library_id),
                        include_health=include_health,
                    ),
                    "custom_sources": app_store.list_retrieval_custom_sources(library_id),
                }
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/sources/report")
    def api_retrieval_sources_report(library_id: str):
        try:
            library_or_404(library_id)
            include_health = str(request.args.get("check") or "").strip().lower() in {"1", "true", "yes", "health"}
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            sources = retrieval_source_statuses(
                registry=retrieval_provider_registry_for_library(library_id),
                include_health=include_health,
            )
            report = retrieval_source_setup_report(sources, include_health=include_health)
            content = render_retrieval_source_setup_report(report, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_source_setup_report_filename(fmt)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/readiness")
    def api_retrieval_readiness(library_id: str):
        try:
            library_or_404(library_id)
            include_health = truthy_query_flag(request.args.get("check"))
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=2)
            return jsonify(
                {
                    "ok": True,
                    "readiness": retrieval_readiness_report_for_library(
                        library_id,
                        query=query,
                        sample_size=sample_size,
                        include_health=include_health,
                    ),
                }
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/readiness/report")
    def api_retrieval_readiness_report(library_id: str):
        try:
            library_or_404(library_id)
            include_health = truthy_query_flag(request.args.get("check"))
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=2)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            report = retrieval_readiness_report_for_library(
                library_id,
                query=query,
                sample_size=sample_size,
                include_health=include_health,
            )
            content = render_retrieval_readiness_report(report, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_readiness_report_filename(fmt)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/retrieval/query-plan/jobs")
    def api_create_retrieval_query_plan_job(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            library_or_404(library_id)
            seed_query = str(payload.get("seed_query") or payload.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(payload.get("sample_size"), default=5)
            limit = max(1, min(int(payload.get("limit") or 5), 10))
            raw_use_ai = payload.get("use_ai")
            use_ai = bool(raw_use_ai) if isinstance(raw_use_ai, bool) else truthy_query_flag(raw_use_ai)
            selected_sources = payload.get("sources") or payload.get("source") or []
            if not isinstance(selected_sources, list):
                selected_sources = [selected_sources]
            selected_sources = [str(source or "").strip() for source in selected_sources if str(source or "").strip()]
            job_id = retrieval_background_job_id("query-plan")
            now = now_iso()
            job = {
                "job_id": job_id,
                "library_id": library_id,
                "type": "query_plan",
                "seed_query": seed_query,
                "sample_size": sample_size,
                "limit": limit,
                "use_ai": use_ai,
                "selected_sources": selected_sources,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
                "message": "AI 检索计划已进入后台队列。",
                "error": "",
                "stop_requested": False,
                "plan": None,
                "query_count": 0,
            }
            with RETRIEVAL_BACKGROUND_LOCK:
                RETRIEVAL_QUERY_PLAN_JOBS[job_id] = job
                trim_retrieval_background_jobs(RETRIEVAL_QUERY_PLAN_JOBS, library_id)
            start_retrieval_query_plan_worker(library_id, job_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                snapshot = retrieval_job_snapshot(RETRIEVAL_QUERY_PLAN_JOBS.get(job_id))
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/query-plan/jobs/latest")
    def api_latest_retrieval_query_plan_job(library_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = retrieval_job_snapshot(latest_retrieval_background_job(RETRIEVAL_QUERY_PLAN_JOBS, library_id))
            return jsonify({"ok": True, "job": job})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/query-plan/jobs/<job_id>")
    def api_retrieval_query_plan_job(library_id: str, job_id: str):
        try:
            library_or_404(library_id)
            with RETRIEVAL_BACKGROUND_LOCK:
                job = RETRIEVAL_QUERY_PLAN_JOBS.get(job_id)
                if not job or job.get("library_id") != library_id:
                    raise ValueError("AI 检索计划任务不存在。")
                snapshot = retrieval_job_snapshot(job)
            return jsonify({"ok": True, "job": snapshot})
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.get("/api/library/<library_id>/retrieval/query-plan")
    def api_retrieval_query_plan(library_id: str):
        try:
            library_or_404(library_id)
            seed_query = str(request.args.get("seed_query") or request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=5)
            limit = max(1, min(int(request.args.get("limit") or 5), 10))
            use_ai = truthy_query_flag(request.args.get("use_ai"))
            selected_sources = request.args.getlist("sources") or request.args.getlist("source")
            return jsonify(
                {
                    "ok": True,
                    "plan": retrieval_query_plan_for_library(
                        library_id,
                        seed_query=seed_query,
                        sample_size=sample_size,
                        limit=limit,
                        use_ai=use_ai,
                        selected_sources=selected_sources,
                    ),
                }
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/query-plan/report")
    def api_retrieval_query_plan_report(library_id: str):
        try:
            library_or_404(library_id)
            seed_query = str(request.args.get("seed_query") or request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=5)
            limit = max(1, min(int(request.args.get("limit") or 5), 10))
            use_ai = truthy_query_flag(request.args.get("use_ai"))
            selected_sources = request.args.getlist("sources") or request.args.getlist("source")
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            plan = retrieval_query_plan_for_library(
                library_id,
                seed_query=seed_query,
                sample_size=sample_size,
                limit=limit,
                use_ai=use_ai,
                selected_sources=selected_sources,
            )
            content = render_retrieval_query_plan_report(plan, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_query_plan_report_filename(fmt)}",
                },
            )
        except (SourceError, RetrievalError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/onboarding")
    def api_retrieval_onboarding(library_id: str):
        try:
            library_or_404(library_id)
            include_health = truthy_query_flag(request.args.get("check"))
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=2)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            use_ai = truthy_query_flag(request.args.get("use_ai"))
            required_queries = request_required_queries()
            return jsonify(
                {
                    "ok": True,
                    "onboarding": retrieval_onboarding_report_for_library(
                        library_id,
                        query=query,
                        sample_size=sample_size,
                        include_health=include_health,
                        limit=limit,
                        use_ai=use_ai,
                        required_queries=required_queries,
                    ),
                }
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/onboarding/report")
    def api_retrieval_onboarding_report(library_id: str):
        try:
            library_or_404(library_id)
            include_health = truthy_query_flag(request.args.get("check"))
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=2)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            use_ai = truthy_query_flag(request.args.get("use_ai"))
            required_queries = request_required_queries()
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            report = retrieval_onboarding_report_for_library(
                library_id,
                query=query,
                sample_size=sample_size,
                include_health=include_health,
                limit=limit,
                use_ai=use_ai,
                required_queries=required_queries,
            )
            content = render_retrieval_onboarding_report(report, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_onboarding_report_filename(fmt)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/onboarding/package")
    def api_retrieval_onboarding_package(library_id: str):
        try:
            library_or_404(library_id)
            include_health = truthy_query_flag(request.args.get("check"))
            query = str(request.args.get("query") or "robot").strip() or "robot"
            sample_size = bounded_retrieval_sample_size(request.args.get("sample_size"), default=2)
            limit = bounded_retrieval_summary_limit(request.args.get("limit"))
            use_ai = truthy_query_flag(request.args.get("use_ai"))
            required_queries = request_required_queries()
            content = retrieval_onboarding_package_for_library(
                library_id,
                query=query,
                sample_size=sample_size,
                include_health=include_health,
                limit=limit,
                use_ai=use_ai,
                required_queries=required_queries,
            )
            return send_file(
                io.BytesIO(content),
                mimetype="application/zip",
                as_attachment=True,
                download_name=retrieval_onboarding_package_filename(),
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/retrieval/runs/<run_id>/report")
    def api_retrieval_run_report(library_id: str, run_id: str):
        try:
            library_or_404(library_id)
            fmt = normalize_retrieval_report_format(str(request.args.get("format") or "markdown"))
            report = app_store.retrieval_run_report(library_id, run_id)
            content = render_retrieval_report(report, fmt)
            mimetype = retrieval_report_mimetype(fmt)
            return Response(
                content,
                mimetype=mimetype,
                headers={
                    "Content-Type": f"{mimetype}; charset=utf-8",
                    "Content-Disposition": f"attachment; filename={retrieval_report_filename(run_id, fmt)}",
                },
            )
        except (SourceError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/export-citations")
    def api_export_citations(library_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys") or []
        fmt = str(payload.get("format") or "").strip()
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        try:
            repo = ZoteroRepository(library_or_404(library_id))
            content, meta = export_citations(repo.items(), [str(key) for key in item_keys], fmt)
            return Response(
                content,
                mimetype=meta["mime"].split(";")[0],
                headers={
                    "Content-Type": meta["mime"],
                    "Content-Disposition": f"attachment; filename={export_filename(fmt)}",
                },
            )
        except (SourceError, ValueError, CitationExportError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/items/<item_key>/pdf-attachments")
    def api_item_pdf_attachments(library_id: str, item_key: str):
        try:
            attachments = ZoteroRepository(library_or_404(library_id)).pdf_attachments_for_item(item_key)
            return jsonify({"ok": True, "attachments": attachments})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/parse-pdfs")
    def api_parse_selected_pdfs(library_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys") or []
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        try:
            summary = mineru_parse_selected_pdfs(library_or_404(library_id), [str(key) for key in item_keys])
            return jsonify({"ok": True, **summary})
        except (SourceError, ValueError, RuntimeError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/knowledge-bases")
    def api_rag_knowledge_bases(library_id: str):
        try:
            return jsonify({"ok": True, "knowledge_bases": rag_list_knowledge_bases(library_or_404(library_id))})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/knowledge-bases")
    def api_rag_create_knowledge_base(library_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys") or []
        if item_keys is None:
            item_keys = []
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        try:
            knowledge_base = rag_create_knowledge_base(
                library_or_404(library_id),
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                item_keys=item_keys,
                base_mode=str(payload.get("base_mode") or "manual"),
                scope=scope,
            )
            return jsonify({"ok": True, "knowledge_base": knowledge_base})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/knowledge-bases/<knowledge_base_id>")
    def api_rag_knowledge_base(library_id: str, knowledge_base_id: str):
        try:
            return jsonify({"ok": True, "knowledge_base": rag_knowledge_base(library_or_404(library_id), knowledge_base_id)})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/rag/knowledge-bases/<knowledge_base_id>")
    def api_rag_delete_knowledge_base(library_id: str, knowledge_base_id: str):
        try:
            return jsonify({"ok": True, **rag_delete_knowledge_base(library_or_404(library_id), knowledge_base_id)})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/knowledge-bases/<knowledge_base_id>/items")
    def api_rag_add_knowledge_base_items(library_id: str, knowledge_base_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys") or []
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        try:
            knowledge_base = rag_add_knowledge_base_items(
                library_or_404(library_id),
                knowledge_base_id,
                item_keys,
                source=str(payload.get("source") or "manual"),
            )
            return jsonify({"ok": True, "knowledge_base": knowledge_base})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/rag/knowledge-bases/<knowledge_base_id>/items")
    def api_rag_remove_knowledge_base_items(library_id: str, knowledge_base_id: str):
        payload = request.get_json(silent=True) or {}
        item_keys = payload.get("item_keys") or []
        if not isinstance(item_keys, list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        try:
            knowledge_base = rag_remove_knowledge_base_items(library_or_404(library_id), knowledge_base_id, item_keys)
            return jsonify({"ok": True, "knowledge_base": knowledge_base})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/index")
    def api_rag_index_library(library_id: str):
        try:
            status = rag_index_library(library_or_404(library_id))
            return jsonify({"ok": True, "status": status})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/index/mineru")
    def api_rag_index_mineru(library_id: str):
        try:
            status = rag_index_mineru_results(library_or_404(library_id))
            return jsonify({"ok": True, "status": status})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/index/status")
    def api_rag_index_status(library_id: str):
        try:
            return jsonify({"ok": True, "status": rag_index_status(library_or_404(library_id))})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/embeddings/config")
    def api_rag_embeddings_config(library_id: str):
        try:
            library = library_or_404(library_id)
            include_secrets = request.args.get("include_secrets") == "1"
            return jsonify({"ok": True, "config": library_embedding_config_response(library, include_secrets=include_secrets)})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/embeddings/config")
    def api_rag_save_embeddings_config(library_id: str):
        payload = request.get_json(silent=True) or {}
        raw = payload.get("embedding") if isinstance(payload.get("embedding"), dict) else payload
        try:
            library = library_or_404(library_id)
            existing = rag_embedding_config(library)
            raw_api_key = clean_secret(raw.get("api_key"))
            api_key = clean_secret(existing.get("api_key")) if raw_api_key == API_CONFIG_SECRET_KEEP_VALUE else raw_api_key
            raw_dim = raw.get("dim")
            dim = int(raw_dim) if str(raw_dim or "").strip() else None
            config = rag_save_embedding_config(
                library,
                enabled=bool(raw.get("enabled", False)),
                provider=clean_secret(raw.get("provider")) or "openai",
                model=clean_secret(raw.get("model")) or "text-embedding-3-small",
                dim=dim,
                vector_store_type=clean_secret(raw.get("vector_store_type")) or "sqlite_blob",
                api_key=api_key,
                base_url=clean_secret(raw.get("base_url")) or CODEX_DEFAULT_BASE_URL,
                batch_size=int(raw.get("batch_size") or 64),
            )
            return jsonify({"ok": True, "config": library_embedding_config_response(library, include_secrets=False), "raw_config": {key: value for key, value in config.items() if key != "api_key"}})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/embeddings/status")
    def api_rag_embeddings_status(library_id: str):
        try:
            return jsonify({"ok": True, "status": rag_embedding_status(library_or_404(library_id))})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/embeddings/index")
    def api_rag_embeddings_index(library_id: str):
        payload = request.get_json(silent=True) or {}
        if "item_keys" in payload and not isinstance(payload.get("item_keys"), list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        try:
            result = rag_embed_missing_chunks(
                library_or_404(library_id),
                knowledge_base_id=str(payload.get("knowledge_base_id") or ""),
                item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
                batch_size=int(payload.get("batch_size") or 64),
                force=bool(payload.get("force", False)),
            )
            return jsonify({"ok": bool(result.get("ok", True)), **result})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/tools/retrieve")
    def api_rag_retrieve(library_id: str):
        payload = request.get_json(silent=True) or {}
        if "item_keys" in payload and not isinstance(payload.get("item_keys"), list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        if "filters" in payload and not isinstance(payload.get("filters"), dict):
            return jsonify({"ok": False, "error": "filters must be an object"}), 400
        try:
            result = rag_retrieve(
                library_or_404(library_id),
                str(payload.get("query") or ""),
                knowledge_base_id=str(payload.get("knowledge_base_id") or ""),
                item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
                mode=str(payload.get("mode") or "auto"),
                top_k=int(payload.get("top_k") or 8),
                include_context=bool(payload.get("include_context", True)),
                context_window=int(payload.get("context_window") or 1),
                filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/agent/check")
    def api_rag_agent_check(library_id: str):
        try:
            library_or_404(library_id)
            model_config = api_config_model_for_library(library_id)
            missing = missing_model_config_fields(model_config)
            return jsonify(
                {
                    "ok": True,
                    "configured": not missing,
                    "model": model_config.get("model", ""),
                    "base_url": normalize_openai_base_url(model_config.get("base_url")),
                    "missing": missing,
                }
            )
        except SourceError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/chat/history")
    def api_rag_chat_history(library_id: str):
        conversation_id = str(request.args.get("conversation_id") or "").strip()
        knowledge_base_id = str(request.args.get("knowledge_base_id") or "").strip()
        if not conversation_id and not knowledge_base_id:
            return jsonify({"ok": False, "error": "conversation_id 和 knowledge_base_id 至少需要一个。"}), 400
        try:
            conversation = load_conversation(
                library_or_404(library_id),
                conversation_id=conversation_id,
                knowledge_base_id=knowledge_base_id,
            )
            return jsonify({"ok": True, **conversation})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/rag/chat/runs/<run_id>")
    def api_rag_chat_run(library_id: str, run_id: str):
        try:
            library = library_or_404(library_id)
            run = load_agent_run(library, run_id)
            if not run or str(run.get("library_id") or "") != library_id:
                return jsonify({"ok": False, "error": "Agent 运行记录不存在。"}), 404
            after_sequence = max(0, int(request.args.get("after_sequence") or 0))
            if after_sequence:
                run["events"] = load_agent_events(library, run_id, after_sequence=after_sequence)
            return jsonify({"ok": True, **run})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/chat/runs/<run_id>/cancel")
    def api_rag_chat_run_cancel(library_id: str, run_id: str):
        try:
            result = cancel_agent_chat_job(library_or_404(library_id), run_id)
            return jsonify(result)
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/chat/runs/<run_id>/restart")
    def api_rag_chat_run_restart(library_id: str, run_id: str):
        try:
            library = library_or_404(library_id)
            model_config = api_config_model_for_library(library_id)
            missing = missing_model_config_fields(model_config)
            if missing:
                return jsonify({"ok": False, "error": f"模型 API 配置不完整，请先配置：{', '.join(missing)}。"}), 400
            result = restart_agent_chat_job(
                library=library,
                model_config=model_config,
                run_id=run_id,
            )
            return jsonify(result), 202
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/chat")
    def api_rag_chat(library_id: str):
        payload = request.get_json(silent=True) or {}
        if "item_keys" in payload and not isinstance(payload.get("item_keys"), list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        question = str(payload.get("question") or payload.get("query") or "").strip()
        if not question:
            return jsonify({"ok": False, "error": "question 不能为空。"}), 400
        conversation_id = str(payload.get("conversation_id") or "").strip()
        knowledge_base_id = str(payload.get("knowledge_base_id") or "").strip()
        if not conversation_id and not knowledge_base_id:
            return jsonify({"ok": False, "error": "新会话必须先选择知识库。"}), 400
        try:
            library = library_or_404(library_id)
            model_config = api_config_model_for_library(library_id)
            missing = missing_model_config_fields(model_config)
            if missing:
                return jsonify({"ok": False, "error": f"模型 API 配置不完整，请先配置：{', '.join(missing)}。"}), 400
            if str(payload.get("response_mode") or "").strip().lower() == "async":
                result = start_agentic_chat_job(
                    library=library,
                    model_config=model_config,
                    conversation_id=conversation_id,
                    question=question,
                    knowledge_base_id=knowledge_base_id,
                    item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
                )
                return jsonify(result), 202
            result = rag_run_agentic_chat(
                library=library,
                model_config=model_config,
                conversation_id=conversation_id,
                question=question,
                knowledge_base_id=knowledge_base_id,
                item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
            )
            return jsonify(result)
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 400

    # ---- 单篇文献研读对话（异步任务 + 轮询 + 停止 + 持久化） ----

    @app.post("/api/library/<library_id>/items/<item_key>/reading-chat/run")
    def api_reading_chat_run(library_id: str, item_key: str):
        library_or_404(library_id)
        task_key = f"{library_id}:{item_key}"
        with READING_CHAT_LOCK:
            if READING_CHAT_TASKS.get(task_key, {}).get("status") == "running":
                return jsonify({"ok": False, "error": "当前文献已有研读问答正在运行。"}), 409
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            user_question = str(payload.get("user_question") or payload.get("question") or "").strip()
            attachments = []
        else:
            payload = {}
            user_question = str(request.form.get("user_question") or "").strip()
            attachments = save_reading_chat_uploads(library_id, item_key, f"run-{now_iso()}", request.files.getlist("images"))
        if not user_question and not attachments:
            return jsonify({"ok": False, "error": "请输入研读问题，或先截图/粘贴一张图片。"}), 400
        run_id = f"readqa-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        user_message = {
            "role": "user",
            "content": user_question or "请根据我附加的图片回答。",
            "created_at": now_iso(),
            "run_id": run_id,
            "item_key": item_key,
            "attachments": attachments,
        }
        append_reading_chat_message(library_id, item_key, user_message)
        upsert_reading_chat_task(
            library_id,
            item_key,
            {
                "run_id": run_id,
                "item_key": item_key,
                "user_question": user_message["content"],
                "status": "running",
                "started_at": now_iso(),
                "attachment_count": len(attachments),
                "events": [],
            },
        )
        thread = threading.Thread(
            target=execute_reading_chat_task,
            args=(library_id, item_key, run_id, user_message["content"], attachments),
            daemon=True,
        )
        with READING_CHAT_LOCK:
            READING_CHAT_TASKS[task_key] = {"run_id": run_id, "status": "running", "thread": thread}
        thread.start()
        return jsonify(
            {
                "ok": True,
                "run_id": run_id,
                "user_message": serialize_reading_chat_messages(library_id, item_key, [user_message])[0],
                "messages": serialize_reading_chat_messages(library_id, item_key, load_reading_chat_messages(library_id, item_key)),
            }
        )

    @app.get("/api/library/<library_id>/items/<item_key>/reading-chat/status")
    def api_reading_chat_status(library_id: str, item_key: str):
        library_or_404(library_id)
        latest = latest_reading_chat_task(library_id, item_key)
        messages = load_reading_chat_messages(library_id, item_key)
        return jsonify(
            {
                "running": bool(latest and latest.get("status") == "running"),
                "latest": latest,
                "messages": serialize_reading_chat_messages(library_id, item_key, messages),
                "message_count": len(messages),
            }
        )

    @app.post("/api/library/<library_id>/items/<item_key>/reading-chat/stop")
    def api_reading_chat_stop(library_id: str, item_key: str):
        library_or_404(library_id)
        latest = latest_reading_chat_task(library_id, item_key)
        if latest and latest.get("status") == "running":
            run_id = latest.get("run_id", "")
            append_reading_chat_task_event(library_id, item_key, run_id, "用户已停止本次文献研读问答。", kind="warning")
            upsert_reading_chat_task(
                library_id,
                item_key,
                {
                    "run_id": run_id,
                    "item_key": item_key,
                    "status": "stopped",
                    "finished_at": now_iso(),
                },
            )
            append_reading_chat_message(
                library_id,
                item_key,
                {
                    "role": "assistant",
                    "content": "已停止本次文献研读问答。当前文献对话记忆会保留，下一次可以继续提问；如果想清空记忆，请点击“重置”。",
                    "created_at": now_iso(),
                    "run_id": run_id,
                    "item_key": item_key,
                    "stopped": True,
                },
            )
        return jsonify(
            {
                "ok": True,
                "messages": serialize_reading_chat_messages(library_id, item_key, load_reading_chat_messages(library_id, item_key)),
                "latest": latest,
            }
        )

    @app.post("/api/library/<library_id>/items/<item_key>/reading-chat/reset")
    def api_reading_chat_reset(library_id: str, item_key: str):
        library_or_404(library_id)
        task_key = f"{library_id}:{item_key}"
        with READING_CHAT_LOCK:
            if READING_CHAT_TASKS.get(task_key, {}).get("status") == "running":
                return jsonify({"ok": False, "error": "当前有研读问答正在运行，请完成后再重置。"}), 409
        divider = {
            "role": "divider",
            "content": "新的对话",
            "created_at": now_iso(),
        }
        save_reading_chat_state(library_id, item_key, {})
        append_reading_chat_message(library_id, item_key, divider)
        return jsonify(
            {
                "ok": True,
                "messages": serialize_reading_chat_messages(library_id, item_key, load_reading_chat_messages(library_id, item_key)),
            }
        )

    @app.get("/api/library/<library_id>/items/<item_key>/reading-chat/asset/<filename>")
    def api_reading_chat_asset(library_id: str, item_key: str, filename: str):
        library_or_404(library_id)
        safe_name = Path(filename).name
        if safe_name != filename:
            return jsonify({"ok": False, "error": "invalid filename"}), 400
        asset_path = _reading_chat_assets_dir(library_id, item_key) / safe_name
        if not asset_path.is_file():
            return jsonify({"ok": False, "error": "asset not found"}), 404
        return send_file(asset_path)

    # ---- 单篇文献矩阵（字段管理 + 运行 + 进度 + 持久化） ----

    def _matrix_item_summaries(library_id: str, library: dict[str, Any], knowledge_base_id: str) -> list[dict[str, Any]]:
        if not knowledge_base_id:
            return []
        try:
            kb = rag_knowledge_base(library, knowledge_base_id)
        except (SourceError, ValueError, OSError, sqlite3.Error):
            return []
        kb_items = kb.get("items") if isinstance(kb.get("items"), list) else []
        repo_items = {it["key"]: it for it in ZoteroRepository(library).items() if isinstance(it, dict) and it.get("key")}
        summaries = []
        for kb_item in kb_items:
            key = str(kb_item.get("item_key") or "")
            repo_item = repo_items.get(key, {})
            summaries.append(
                {
                    "key": key,
                    "title": kb_item.get("title") or repo_item.get("title", ""),
                    "creators_display": repo_item.get("creators_display", ""),
                    "year": kb_item.get("year") or repo_item.get("year", ""),
                    "venue": kb_item.get("venue") or repo_item.get("venue", ""),
                    "has_pdf": bool(_first_pdf_path(repo_item)),
                    "values": load_matrix_item_values(library_id, knowledge_base_id, key),
                }
            )
        return summaries

    @app.get("/api/library/<library_id>/matrix")
    def api_matrix_state(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        knowledge_base_id = str(request.args.get("knowledge_base_id") or payload.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            return jsonify({"ok": True, "knowledge_base_id": "", "fields": [], "items": [], "running": False, "latest": None})
        latest = latest_matrix_task(library_id, knowledge_base_id)
        return jsonify(
            {
                "ok": True,
                "knowledge_base_id": knowledge_base_id,
                "fields": load_matrix_fields(library_id, knowledge_base_id),
                "items": _matrix_item_summaries(library_id, library, knowledge_base_id),
                "running": bool(latest and latest.get("status") == "running"),
                "latest": latest,
            }
        )

    @app.post("/api/library/<library_id>/matrix/fields")
    def api_matrix_save_fields(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        knowledge_base_id = str(request.args.get("knowledge_base_id") or payload.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            return jsonify({"ok": False, "error": "缺少 knowledge_base_id"}), 400
        raw = payload.get("fields")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "error": "fields 必须是数组"}), 400
        fields = normalize_matrix_fields(raw)
        save_matrix_fields(library_id, knowledge_base_id, fields)
        return jsonify({"ok": True, "fields": fields})

    @app.post("/api/library/<library_id>/matrix/recommend-fields")
    def api_matrix_recommend_fields(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        knowledge_base_id = str(request.args.get("knowledge_base_id") or payload.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            return jsonify({"ok": False, "error": "缺少 knowledge_base_id"}), 400
        item_keys = payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else []
        try:
            kb = rag_knowledge_base(library, knowledge_base_id)
            kb_items = kb.get("items") if isinstance(kb.get("items"), list) else []
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": f"知识库不存在：{exc}"}), 400
        kb_keys = {str(it.get("item_key") or "") for it in kb_items}
        selected_keys = [k for k in (item_keys or []) if k in kb_keys] if item_keys else list(kb_keys)
        if not selected_keys:
            return jsonify({"ok": False, "error": "当前知识库没有可推荐的论文。"}), 400
        repo_items = {it["key"]: it for it in ZoteroRepository(library).items() if isinstance(it, dict) and it.get("key")}
        selected = [repo_items.get(k, {"key": k, "title": "", "creators": [], "fields": {}}) for k in selected_keys]
        try:
            codex_config = api_config_codex_for_library(library_id)
            recommended = recommend_matrix_fields(
                library=library,
                codex_config=codex_config,
                items=selected,
                existing_fields=load_matrix_fields(library_id, knowledge_base_id),
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"AI 推荐字段失败：{exc}"}), 500
        return jsonify({"ok": True, "fields": recommended, "source_count": len(selected_keys)})

    @app.post("/api/library/<library_id>/matrix/run")
    def api_matrix_run(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        knowledge_base_id = str(request.args.get("knowledge_base_id") or payload.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            return jsonify({"ok": False, "error": "缺少 knowledge_base_id"}), 400
        task_key = f"{library_id}:{knowledge_base_id}"
        with MATRIX_LOCK:
            if MATRIX_TASKS.get(task_key, {}).get("status") == "running":
                return jsonify({"ok": False, "error": "已有文献矩阵任务正在运行。"}), 409
        item_keys = [str(k) for k in (payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else []) if str(k).strip()]
        if not item_keys:
            return jsonify({"ok": False, "error": "请至少选择一篇文献。"}), 400
        mode = str(payload.get("mode") or "skip_existing")
        if mode not in ("skip_existing", "overwrite_existing"):
            mode = "skip_existing"
        run_id = f"matrix-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        upsert_matrix_task(
            library_id,
            knowledge_base_id,
            {
                "run_id": run_id,
                "status": "running",
                "knowledge_base_id": knowledge_base_id,
                "selected_item_keys": item_keys,
                "mode": mode,
                "total": len(item_keys),
                "completed": 0,
                "failed": 0,
                "skipped_no_pdf": 0,
                "skipped_existing": 0,
                "current_item_key": "",
                "current_title": "",
                "started_at": now_iso(),
                "finished_at": "",
                "events": [],
            },
        )
        thread = threading.Thread(
            target=execute_matrix_task,
            args=(library_id, knowledge_base_id, run_id, item_keys, mode),
            daemon=True,
        )
        with MATRIX_LOCK:
            MATRIX_TASKS[task_key] = {"run_id": run_id, "status": "running", "thread": thread}
        thread.start()
        return jsonify({"ok": True, "run_id": run_id})

    @app.get("/api/library/<library_id>/matrix/status")
    def api_matrix_status(library_id: str):
        library = library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        knowledge_base_id = str(request.args.get("knowledge_base_id") or payload.get("knowledge_base_id") or "").strip()
        latest = latest_matrix_task(library_id, knowledge_base_id) if knowledge_base_id else None
        return jsonify(
            {
                "ok": True,
                "running": bool(latest and latest.get("status") == "running"),
                "latest": latest,
                "fields": load_matrix_fields(library_id, knowledge_base_id) if knowledge_base_id else [],
                "items": _matrix_item_summaries(library_id, library, knowledge_base_id) if knowledge_base_id else [],
            }
        )

    @app.post("/api/library/<library_id>/matrix/stop")
    def api_matrix_stop(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        knowledge_base_id = str(request.args.get("knowledge_base_id") or payload.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            return jsonify({"ok": False, "error": "缺少 knowledge_base_id"}), 400
        latest = latest_matrix_task(library_id, knowledge_base_id)
        if not latest or latest.get("status") != "running":
            return jsonify({"ok": False, "error": "当前没有正在运行的文献矩阵任务。"}), 400
        upsert_matrix_task(library_id, knowledge_base_id, {**latest, "status": "stopped", "finished_at": now_iso()})
        append_matrix_task_event(library_id, knowledge_base_id, latest.get("run_id"), "用户已停止文献矩阵任务。")
        with MATRIX_LOCK:
            MATRIX_TASKS.pop(f"{library_id}:{knowledge_base_id}", None)
        return jsonify({"ok": True, "latest": latest_matrix_task(library_id, knowledge_base_id)})

    @app.post("/api/library/<library_id>/rag/tools/keyword_search")
    def api_rag_keyword_search(library_id: str):
        payload = request.get_json(silent=True) or {}
        if "item_keys" in payload and not isinstance(payload.get("item_keys"), list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        if "filters" in payload and not isinstance(payload.get("filters"), dict):
            return jsonify({"ok": False, "error": "filters must be an object"}), 400
        try:
            result = rag_keyword_search(
                library_or_404(library_id),
                str(payload.get("query") or ""),
                top_k=int(payload.get("top_k") or 10),
                chunk_type=str(payload.get("chunk_type") or ""),
                knowledge_base_id=str(payload.get("knowledge_base_id") or ""),
                item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
                filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/tools/metadata_search")
    def api_rag_metadata_search(library_id: str):
        payload = request.get_json(silent=True) or {}
        if "item_keys" in payload and not isinstance(payload.get("item_keys"), list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        if "filters" in payload and not isinstance(payload.get("filters"), dict):
            return jsonify({"ok": False, "error": "filters must be an object"}), 400
        try:
            result = rag_metadata_search(
                library_or_404(library_id),
                str(payload.get("query") or ""),
                top_k=int(payload.get("top_k") or 10),
                knowledge_base_id=str(payload.get("knowledge_base_id") or ""),
                item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
                filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/tools/semantic_search")
    def api_rag_semantic_search(library_id: str):
        payload = request.get_json(silent=True) or {}
        if "item_keys" in payload and not isinstance(payload.get("item_keys"), list):
            return jsonify({"ok": False, "error": "item_keys must be a list"}), 400
        if "filters" in payload and not isinstance(payload.get("filters"), dict):
            return jsonify({"ok": False, "error": "filters must be an object"}), 400
        try:
            result = rag_semantic_search(
                library_or_404(library_id),
                str(payload.get("query") or ""),
                top_k=int(payload.get("top_k") or 10),
                chunk_type=str(payload.get("chunk_type") or ""),
                knowledge_base_id=str(payload.get("knowledge_base_id") or ""),
                item_keys=payload.get("item_keys") if isinstance(payload.get("item_keys"), list) else None,
                filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/rag/tools/chunk_read")
    def api_rag_chunk_read(library_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            result = rag_chunk_read(
                library_or_404(library_id),
                str(payload.get("chunk_id") or ""),
                doc_id=str(payload.get("doc_id") or ""),
                window_size=int(payload.get("window_size") or 2),
            )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/semantic-rules")
    def api_semantic_rules(library_id: str):
        library_or_404(library_id)
        return jsonify({"ok": True, "rules": app_store.list_semantic_rules(library_id)})

    @app.post("/api/library/<library_id>/semantic-rules")
    def api_add_semantic_rule(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        bucket = str(payload.get("bucket") or "").strip()
        pattern = str(payload.get("pattern") or "").strip()
        label = str(payload.get("label") or "").strip()
        if bucket not in {"rating", "nested", "venue_rank", "reading_status", "plain"}:
            return jsonify({"ok": False, "error": "未知语义桶。"}), 400
        if not pattern:
            return jsonify({"ok": False, "error": "pattern 不能为空。"}), 400
        rule = app_store.add_semantic_rule(library_id, bucket, pattern, label)
        return jsonify({"ok": True, "rule": rule})

    @app.get("/api/library/<library_id>/tag-shortcuts")
    def api_tag_shortcuts(library_id: str):
        library_or_404(library_id)
        return jsonify({"ok": True, "shortcuts": app_store.list_tag_shortcuts(library_id)})

    @app.post("/api/library/<library_id>/tag-shortcuts")
    def api_add_tag_shortcut(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        tag = str(payload.get("tag") or "").strip()
        if not tag:
            return jsonify({"ok": False, "error": "标签不能为空。"}), 400
        normalized_tag = normalize_hash_tag(tag)
        shortcut = app_store.upsert_tag_shortcut(library_id, normalized_tag, stable_tag_color(normalized_tag))
        return jsonify({"ok": True, "shortcut": shortcut})

    @app.delete("/api/library/<library_id>/tag-shortcuts")
    def api_delete_tag_shortcut(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        tag = str(payload.get("tag") or "").strip()
        if not tag:
            return jsonify({"ok": False, "error": "标签不能为空。"}), 400
        app_store.delete_tag_shortcut(library_id, tag)
        return jsonify({"ok": True})

    @app.post("/api/library/<library_id>/items/<item_key>/attachments/file")
    def api_add_file_attachment(library_id: str, item_key: str):
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"ok": False, "error": "请选择要上传的文件。"}), 400
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                filename = Path(upload.filename).name
                temp_path = Path(tmp_dir) / filename
                upload.save(temp_path)
                result = ZoteroRepository(library_or_404(library_id)).add_file_attachment(
                    item_key,
                    temp_path,
                    filename,
                    upload.mimetype or None,
                )
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/items/<item_key>/attachments/url")
    def api_add_url_attachment(library_id: str, item_key: str):
        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "网址不能为空。"}), 400
        try:
            result = ZoteroRepository(library_or_404(library_id)).add_url_attachment(item_key, url, title)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.patch("/api/library/<library_id>/attachments/<attachment_key>")
    def api_rename_attachment(library_id: str, attachment_key: str):
        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title") or "").strip()
        if not title:
            return jsonify({"ok": False, "error": "附件名称不能为空。"}), 400
        try:
            result = ZoteroRepository(library_or_404(library_id)).rename_attachment(attachment_key, title)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/library/<library_id>/attachments/<attachment_key>")
    def api_delete_attachment(library_id: str, attachment_key: str):
        payload = request.get_json(silent=True) or {}
        keys = payload.get("attachment_keys")
        attachment_keys = [str(key) for key in keys] if isinstance(keys, list) else [attachment_key]
        try:
            result = ZoteroRepository(library_or_404(library_id)).delete_attachments(attachment_keys)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/attachments/<attachment_key>/annotations")
    def api_attachment_annotations(library_id: str, attachment_key: str):
        try:
            annotations = ZoteroRepository(library_or_404(library_id)).annotations_for_attachment(attachment_key)
            return jsonify({"ok": True, "annotations": annotations})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/attachments/<attachment_key>/annotations")
    def api_create_attachment_annotation(library_id: str, attachment_key: str):
        payload = request.get_json(silent=True) or {}
        try:
            annotation = ZoteroRepository(library_or_404(library_id)).create_pdf_annotation(attachment_key, payload)
            return jsonify({"ok": True, "annotation": annotation})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/library/<library_id>/attachments/<attachment_key>/annotations/clear")
    def api_clear_attachment_annotations(library_id: str, attachment_key: str):
        payload = request.get_json(silent=True) or {}
        try:
            result = ZoteroRepository(library_or_404(library_id)).clear_pdf_annotations(attachment_key, payload)
            return jsonify({"ok": True, **result})
        except (SourceError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/library/<library_id>/sync/payloads")
    def api_sync_payloads(library_id: str):
        library_or_404(library_id)
        return jsonify({"ok": True, "payloads": prepare_sync_payloads(library_id)})

    @app.post("/api/library/<library_id>/sync/conflicts")
    def api_mark_conflicts(library_id: str):
        library_or_404(library_id)
        payload = request.get_json(silent=True) or {}
        keys = {str(key) for key in payload.get("changed_keys") or []}
        return jsonify({"ok": True, "conflicted": mark_conflicts_for_changed_keys(library_id, keys)})

    @app.get("/api/library/<library_id>/attachments/<attachment_key>")
    def api_open_attachment(library_id: str, attachment_key: str):
        repo = ZoteroRepository(library_or_404(library_id))
        for item in repo.items():
            for attachment in item.get("attachments", []):
                if attachment.get("key") == attachment_key and attachment.get("openable") and attachment.get("resolved_path"):
                    path = Path(attachment["resolved_path"])
                    if path.exists():
                        return send_file(path, as_attachment=False)
        return jsonify({"ok": False, "error": "附件文件缺失或不可直接打开。"}), 404

    return app


app = create_app()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    host = os.environ.get("WEB_LIBRARY_HOST", "127.0.0.1")
    port = _env_int("WEB_LIBRARY_PORT", 8686)
    debug = _env_bool("WEB_LIBRARY_DEBUG", True)
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
