from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox, SkillInput, TextInput
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    ItemCompletedNotification,
    ReasoningSummary,
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStatus,
)

from zotero_web_library.agentic_rag_skill import agentic_rag_skill_path as shared_agentic_rag_skill_path
from zotero_web_library.paths import app_data_dir


MINIMAL_CONNECTIVITY_PROMPT = "请只回答“正常”两个字。"
DEFAULT_WIRE_API = "responses"
DEFAULT_MODEL_PROVIDER = "web_library_openai"
PROVIDER_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")


class CodexConfigError(RuntimeError):
    pass


class CodexTurnError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass(slots=True)
class CodexTurnDiagnostics:
    turn_id: str
    status: str
    error: str
    final_response: str
    delta_text: str
    items: list[ThreadItem]
    usage: ThreadTokenUsage | None
    diagnostics: dict[str, Any]

    def to_api_payload(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "turn_status": self.status,
            "assistant_text": self.final_response,
            "usage": model_to_json(self.usage),
            "diagnostics": self.diagnostics,
        }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def agentic_rag_skill_path() -> Path:
    return shared_agentic_rag_skill_path()


def model_to_json(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    return value


def safe_provider_name(value: str) -> str:
    provider = PROVIDER_SAFE_RE.sub("_", str(value or "").strip()).strip("_")
    return provider or DEFAULT_MODEL_PROVIDER


def build_runtime_config(library: dict[str, Any], codex_config: dict[str, Any]) -> dict[str, Any]:
    model = str(codex_config.get("model") or "").strip()
    api_key = str(codex_config.get("api_key") or "").strip()
    base_url = str(codex_config.get("base_url") or "").strip()
    if not model:
        raise CodexConfigError("请先在 API 配置页填写 Codex 模型。")
    if not api_key:
        raise CodexConfigError("请先在 API 配置页填写 Codex API Key。")
    if not base_url:
        raise CodexConfigError("请先在 API 配置页填写 Codex Base URL。")

    library_id = str(library.get("library_id") or "library")
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/") + "/",
        "model": model,
        "model_provider": safe_provider_name(f"web_library_{library_id}"),
        "wire_api": str(codex_config.get("wire_api") or DEFAULT_WIRE_API),
        "reasoning_effort": str(codex_config.get("reasoning_effort_default") or codex_config.get("reasoning_effort") or "medium"),
        "disable_response_storage": bool(codex_config.get("disable_response_storage", True)),
    }


def build_config_overrides(config: dict[str, Any], *, reasoning_effort: str | None = None) -> tuple[str, ...]:
    provider = safe_provider_name(str(config.get("model_provider") or DEFAULT_MODEL_PROVIDER))
    base_url = str(config["base_url"]).rstrip("/") + "/"
    effort = reasoning_effort or str(config.get("reasoning_effort") or "medium")
    return (
        f'model_provider="{provider}"',
        f'model="{config["model"]}"',
        f'model_reasoning_effort="{effort}"',
        f'disable_response_storage={str(bool(config.get("disable_response_storage", True))).lower()}',
        f'model_providers.{provider}.name="{provider}"',
        f'model_providers.{provider}.wire_api="{config.get("wire_api") or DEFAULT_WIRE_API}"',
        f"model_providers.{provider}.requires_openai_auth=true",
        f'model_providers.{provider}.base_url="{base_url}"',
    )


def item_root(item: ThreadItem) -> Any:
    return item.root if hasattr(item, "root") else item


def item_summary(item: ThreadItem) -> dict[str, Any]:
    root = item_root(item)
    item_type = str(getattr(root, "type", type(root).__name__))
    summary: dict[str, Any] = {
        "type": item_type,
        "id": str(getattr(root, "id", "")),
    }
    if isinstance(root, AgentMessageThreadItem):
        summary["phase"] = root.phase.value if root.phase else ""
        summary["text_preview"] = str(root.text or "")[:500]
    elif isinstance(root, CommandExecutionThreadItem):
        summary["status"] = root.status.value
    return summary


def final_response_from_items(items: list[ThreadItem]) -> str:
    last_unknown_phase = ""
    for item in reversed(items):
        root = item_root(item)
        if not isinstance(root, AgentMessageThreadItem):
            continue
        phase = root.phase.value if root.phase else ""
        if phase == "final_answer":
            return root.text or ""
        if not phase and not last_unknown_phase:
            last_unknown_phase = root.text or ""
    return last_unknown_phase


def collect_codex_turn_with_diagnostics(
    stream: Iterator[Any],
    *,
    turn_id: str,
    on_event: Callable[[Any], None] | None = None,
) -> CodexTurnDiagnostics:
    completed: TurnCompletedNotification | None = None
    items: list[ThreadItem] = []
    usage: ThreadTokenUsage | None = None
    deltas: list[str] = []

    for event in stream:
        if on_event:
            on_event(event)
        payload = event.payload
        if isinstance(payload, AgentMessageDeltaNotification) and payload.turn_id == turn_id:
            deltas.append(payload.delta)
            continue
        if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn_id:
            items.append(payload.item)
            continue
        if isinstance(payload, ThreadTokenUsageUpdatedNotification) and payload.turn_id == turn_id:
            usage = payload.token_usage
            continue
        if isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn_id:
            completed = payload

    if completed is None:
        diagnostics = {
            "turn_id": turn_id,
            "item_count": len(items),
            "delta_preview": "".join(deltas).strip()[:1000],
            "items": [item_summary(item) for item in items[-8:]],
        }
        raise CodexTurnError("没有收到 Codex turn completed 事件。", diagnostics)

    turn = completed.turn
    status = turn.status.value if hasattr(turn.status, "value") else str(turn.status)
    error = turn.error.message if turn.error is not None and turn.error.message else ""
    delta_text = "".join(deltas).strip()
    final_response = (final_response_from_items(items) or delta_text).strip()
    diagnostics = {
        "turn_id": turn_id,
        "status": status,
        "error": error,
        "item_count": len(items),
        "agent_delta_chars": len(delta_text),
        "items": [item_summary(item) for item in items[-8:]],
    }
    if turn.status == TurnStatus.failed:
        raise CodexTurnError(error or f"Codex turn failed: {status}", diagnostics)
    return CodexTurnDiagnostics(
        turn_id=turn_id,
        status=status,
        error=error,
        final_response=final_response,
        delta_text=delta_text,
        items=items,
        usage=usage,
        diagnostics=diagnostics,
    )


def run_thread_turn_with_diagnostics(
    thread: Any,
    turn_input: list[Any],
    *,
    approval_mode: ApprovalMode = ApprovalMode.deny_all,
    sandbox: Sandbox = Sandbox.workspace_write,
    summary: ReasoningSummary | None = None,
    on_event: Callable[[Any], None] | None = None,
) -> CodexTurnDiagnostics:
    turn = thread.turn(
        turn_input,
        approval_mode=approval_mode,
        sandbox=sandbox,
        summary=summary,
    )
    stream = turn.stream()
    try:
        return collect_codex_turn_with_diagnostics(stream, turn_id=turn.id, on_event=on_event)
    finally:
        stream.close()


def is_context_limit_error(message: str) -> bool:
    text = str(message or "").lower()
    needles = [
        "max_seq_len",
        "context_length_exceeded",
        "input tokens",
        "maximum context",
        "context window",
        "too many tokens",
        "tokens has exceeded",
        "exceeded max",
    ]
    return any(item in text for item in needles)


def friendly_codex_error(exc: Exception) -> str:
    message = str(exc)
    if is_context_limit_error(message):
        return f"当前请求材料超过模型上下文窗口：{message}"
    return message


def codex_home_dir(library: dict[str, Any]) -> Path:
    library_id = safe_provider_name(str(library.get("library_id") or "library"))
    path = app_data_dir() / "codex-home-web" / library_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_codex_prompt(
    *,
    library: dict[str, Any],
    codex_config: dict[str, Any],
    prompt: str,
    include_agentic_rag_skill: bool = False,
    ephemeral: bool = True,
    read_only: bool = False,
) -> dict[str, Any]:
    runtime = build_runtime_config(library, codex_config)
    provider = runtime["model_provider"]
    working_dir = repo_root()
    codex_config_obj = CodexConfig(
        cwd=str(working_dir),
        env={"CODEX_HOME": str(codex_home_dir(library))},
        config_overrides=build_config_overrides(runtime),
    )
    turn_input: list[Any] = []
    if include_agentic_rag_skill:
        skill_path = agentic_rag_skill_path()
        if not skill_path.exists():
            raise CodexConfigError(f"agentic-rag skill 不存在：{skill_path}")
        turn_input.append(SkillInput(name="agentic-rag", path=str(skill_path)))
    turn_input.append(TextInput(str(prompt or "")))

    sandbox = Sandbox.read_only if read_only else Sandbox.workspace_write
    with Codex(codex_config_obj) as codex:
        codex.login_api_key(runtime["api_key"])
        thread = codex.thread_start(
            cwd=str(working_dir),
            sandbox=sandbox,
            approval_mode=ApprovalMode.deny_all,
            model=runtime["model"],
            model_provider=provider,
            ephemeral=ephemeral,
        )
        result = run_thread_turn_with_diagnostics(
            thread,
            turn_input,
            sandbox=sandbox,
            summary=ReasoningSummary(root="concise"),
        )
    payload = result.to_api_payload()
    payload.update(
        {
            "ok": bool(result.final_response),
            "model": runtime["model"],
            "model_provider": provider,
        }
    )
    return payload


def run_codex_connectivity_probe(*, library: dict[str, Any], codex_config: dict[str, Any]) -> dict[str, Any]:
    try:
        result = run_codex_prompt(
            library=library,
            codex_config=codex_config,
            prompt=MINIMAL_CONNECTIVITY_PROMPT,
            include_agentic_rag_skill=False,
            ephemeral=True,
        )
        if not result.get("assistant_text"):
            return {
                **result,
                "ok": False,
                "message": "Codex turn 已完成，但没有收到 assistant 文本。",
            }
        return {
            **result,
            "ok": True,
            "message": f"测试成功：{result.get('assistant_text')}",
        }
    except CodexConfigError as exc:
        return {"ok": False, "message": str(exc), "diagnostics": {"error_type": "config"}}
    except CodexTurnError as exc:
        return {"ok": False, "message": friendly_codex_error(exc), "diagnostics": exc.diagnostics}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": friendly_codex_error(exc), "diagnostics": {"error_type": type(exc).__name__}}
