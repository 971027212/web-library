from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .utils import new_key, now_iso


AGENT_ACTIONS = {"ASK_USER", "UPDATE_INTENT", "PROPOSE_SEARCH", "FINAL"}
THREAD_STATUSES = {
    "created",
    "running",
    "waiting_user",
    "waiting_approval",
    "searching",
    "completed",
    "interrupted",
    "failed",
}
RESEARCH_GOALS = {
    "survey",
    "implementation",
    "innovation",
    "benchmark",
    "citation",
    "dataset",
    "model",
    "mixed",
}
MATERIAL_TYPES = {"paper", "code", "model", "dataset", "benchmark", "website"}
LIST_INTENT_FIELDS = {
    "must_include",
    "nice_to_have",
    "exclude_terms",
    "material_types",
    "preferred_sources",
    "quality_criteria",
    "ambiguities",
}
TEXT_INTENT_FIELDS = {"topic", "normalized_topic", "application_context"}


def default_intent_model() -> dict[str, Any]:
    return {
        "topic": "",
        "normalized_topic": "",
        "research_goal": "mixed",
        "must_include": [],
        "nice_to_have": [],
        "exclude_terms": [],
        "material_types": ["paper", "code", "model", "dataset", "benchmark", "website"],
        "time_range": {},
        "preferred_sources": [],
        "quality_criteria": [],
        "application_context": "",
        "ambiguities": [],
        "confidence": 0.0,
    }


def default_agent_state() -> dict[str, Any]:
    return {
        "version": 2,
        "thread": {
            "thread_status": "created",
            "active_turn_id": "",
            "last_turn_id": "",
            "last_error": "",
        },
        "intent_model": default_intent_model(),
        "readiness": {"score": 0.0, "missing": ["研究主题或目标"]},
        "coverage_state": {
            "overall_score": 0.0,
            "by_material_type": {},
            "by_method": {},
            "by_year": {},
            "by_source": {},
            "by_venue": {},
            "gaps": [],
            "last_updated_from_run_ids": [],
        },
        "pending_actions": [],
        "memory_policy": {
            "session_enabled": True,
            "task_enabled": True,
            "library_enabled": False,
        },
        "feedback_summary": {
            "counts": {},
            "positive_candidate_ids": [],
            "negative_candidate_ids": [],
            "neutral_candidate_ids": [],
            "recent": [],
        },
        "budget": {
            "max_search_rounds": 5,
            "max_queries_per_round": 18,
            "max_candidates": 500,
            "search_rounds_used": 0,
        },
        "last_assistant_message": "",
        "updated_at": now_iso(),
    }


def _clean_text(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_list(value: Any, *, limit: int = 24, item_limit: int = 160) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[\n,，;；]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_text(item, item_limit)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _clean_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1:
        score /= 100
    return round(max(0.0, min(score, 1.0)), 3)


def normalize_intent_model(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    intent = default_intent_model()
    for field in TEXT_INTENT_FIELDS:
        intent[field] = _clean_text(raw.get(field), 600)
    for field in LIST_INTENT_FIELDS:
        intent[field] = _clean_list(raw.get(field))
    goal = _clean_text(raw.get("research_goal"), 40).lower()
    intent["research_goal"] = goal if goal in RESEARCH_GOALS else "mixed"
    materials = [
        item.lower()
        for item in intent["material_types"]
        if item.lower() in MATERIAL_TYPES
    ]
    intent["material_types"] = list(dict.fromkeys(materials)) or ["paper"]
    time_range = raw.get("time_range") if isinstance(raw.get("time_range"), dict) else {}
    normalized_time: dict[str, int] = {}
    for key in ("start_year", "end_year"):
        try:
            year = int(time_range.get(key))
        except (TypeError, ValueError):
            continue
        if 1800 <= year <= 2200:
            normalized_time[key] = year
    intent["time_range"] = normalized_time
    intent["confidence"] = _clean_confidence(raw.get("confidence"))
    return intent


def normalize_agent_state(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    state = default_agent_state()
    state.update({key: raw[key] for key in state if key in raw})
    state["intent_model"] = normalize_intent_model(raw.get("intent_model"))
    thread = raw.get("thread") if isinstance(raw.get("thread"), dict) else {}
    status = _clean_text(thread.get("thread_status"), 40).lower()
    state["thread"] = {
        "thread_status": status if status in THREAD_STATUSES else "created",
        "active_turn_id": _clean_text(thread.get("active_turn_id"), 100),
        "last_turn_id": _clean_text(thread.get("last_turn_id"), 100),
        "last_error": _clean_text(thread.get("last_error"), 1000),
    }
    readiness = raw.get("readiness") if isinstance(raw.get("readiness"), dict) else {}
    state["readiness"] = {
        "score": _clean_confidence(readiness.get("score")),
        "missing": _clean_list(readiness.get("missing"), limit=12),
    }
    coverage = raw.get("coverage_state") if isinstance(raw.get("coverage_state"), dict) else {}
    state["coverage_state"] = {
        "overall_score": _clean_confidence(coverage.get("overall_score")),
        "by_material_type": coverage.get("by_material_type") if isinstance(coverage.get("by_material_type"), dict) else {},
        "by_method": coverage.get("by_method") if isinstance(coverage.get("by_method"), dict) else {},
        "by_year": coverage.get("by_year") if isinstance(coverage.get("by_year"), dict) else {},
        "by_source": coverage.get("by_source") if isinstance(coverage.get("by_source"), dict) else {},
        "by_venue": coverage.get("by_venue") if isinstance(coverage.get("by_venue"), dict) else {},
        "gaps": _clean_list(coverage.get("gaps"), limit=30),
        "last_updated_from_run_ids": _clean_list(
            coverage.get("last_updated_from_run_ids"),
            limit=100,
            item_limit=100,
        ),
    }
    pending = raw.get("pending_actions") if isinstance(raw.get("pending_actions"), list) else []
    state["pending_actions"] = [
        item for item in pending if isinstance(item, dict)
    ][-20:]
    policy = raw.get("memory_policy") if isinstance(raw.get("memory_policy"), dict) else {}
    state["memory_policy"] = {
        "session_enabled": policy.get("session_enabled") is not False,
        "task_enabled": policy.get("task_enabled") is not False,
        "library_enabled": policy.get("library_enabled") is True,
    }
    budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    state["budget"] = {
        "max_search_rounds": max(1, min(int(budget.get("max_search_rounds") or 5), 10)),
        "max_queries_per_round": max(3, min(int(budget.get("max_queries_per_round") or 18), 40)),
        "max_candidates": max(20, min(int(budget.get("max_candidates") or 500), 2000)),
        "search_rounds_used": max(0, int(budget.get("search_rounds_used") or 0)),
    }
    feedback = raw.get("feedback_summary") if isinstance(raw.get("feedback_summary"), dict) else {}
    state["feedback_summary"] = {
        "counts": feedback.get("counts") if isinstance(feedback.get("counts"), dict) else {},
        "positive_candidate_ids": _clean_list(feedback.get("positive_candidate_ids"), limit=100),
        "negative_candidate_ids": _clean_list(feedback.get("negative_candidate_ids"), limit=100),
        "neutral_candidate_ids": _clean_list(feedback.get("neutral_candidate_ids"), limit=100),
        "recent": [item for item in feedback.get("recent") or [] if isinstance(item, dict)][-30:],
    }
    state["last_assistant_message"] = _clean_text(raw.get("last_assistant_message"), 4000)
    state["updated_at"] = _clean_text(raw.get("updated_at"), 80) or now_iso()
    return state


def merge_intent_model(current: Any, patch: Any) -> dict[str, Any]:
    base = normalize_intent_model(current)
    raw_patch = patch if isinstance(patch, dict) else {}
    merged = dict(base)
    for field in TEXT_INTENT_FIELDS | LIST_INTENT_FIELDS | {
        "research_goal",
        "time_range",
        "confidence",
    }:
        if field in raw_patch:
            merged[field] = raw_patch[field]
    return normalize_intent_model(merged)


def intent_summary(intent: dict[str, Any]) -> str:
    normalized = normalize_intent_model(intent)
    pieces = [
        normalized["normalized_topic"] or normalized["topic"],
        normalized["research_goal"],
    ]
    if normalized["must_include"]:
        pieces.append("必须包含：" + "、".join(normalized["must_include"][:4]))
    if normalized["exclude_terms"]:
        pieces.append("排除：" + "、".join(normalized["exclude_terms"][:3]))
    return "；".join(piece for piece in pieces if piece)


def feedback_summary(feedback: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(
        _clean_text(item.get("feedback_type"), 40).lower()
        for item in feedback
        if isinstance(item, dict)
    )
    positive_types = {"accepted", "imported", "useful_code"}
    negative_types = {"irrelevant"}
    neutral_types = {"duplicate", "already_known", "too_old", "weak_metadata"}

    def ids_for(types: set[str]) -> list[str]:
        return list(
            dict.fromkeys(
                _clean_text(item.get("candidate_id"), 120)
                for item in feedback
                if isinstance(item, dict)
                and _clean_text(item.get("feedback_type"), 40).lower() in types
                and _clean_text(item.get("candidate_id"), 120)
            )
        )[:100]

    return {
        "counts": dict(counts),
        "positive_candidate_ids": ids_for(positive_types),
        "negative_candidate_ids": ids_for(negative_types),
        "neutral_candidate_ids": ids_for(neutral_types),
        "recent": [
            {
                "candidate_id": _clean_text(item.get("candidate_id"), 120),
                "feedback_type": _clean_text(item.get("feedback_type"), 40),
                "note": _clean_text(item.get("note"), 240),
            }
            for item in feedback[:30]
            if isinstance(item, dict)
        ],
    }


def coverage_state_from_guided(
    coverage: Any,
    *,
    run_ids: list[str] | None = None,
    source_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = coverage if isinstance(coverage, dict) else {}
    material = raw.get("material_counts") if isinstance(raw.get("material_counts"), dict) else {}
    gaps = raw.get("gaps") if isinstance(raw.get("gaps"), list) else raw.get("missing") or []
    status = _clean_text(raw.get("status"), 40).lower()
    score = raw.get("overall_score")
    if score is None:
        score = {"good": 0.9, "partial": 0.55, "weak": 0.3}.get(status, 0.0)
    sources: dict[str, Any] = {}
    for name, stats in (source_stats or {}).items():
        if not isinstance(stats, dict):
            continue
        sources[str(name)] = {
            "ok": stats.get("ok") is not False,
            "count": int(stats.get("count") or 0),
            "error": _clean_text(stats.get("error"), 200),
        }
    return {
        "overall_score": _clean_confidence(score),
        "by_material_type": material,
        "by_method": raw.get("by_method") if isinstance(raw.get("by_method"), dict) else {},
        "by_year": raw.get("by_year") if isinstance(raw.get("by_year"), dict) else {},
        "by_source": sources,
        "by_venue": raw.get("by_venue") if isinstance(raw.get("by_venue"), dict) else {},
        "gaps": _clean_list(gaps, limit=30),
        "last_updated_from_run_ids": _clean_list(run_ids or [], limit=100, item_limit=100),
    }


def _prompt_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "role": _clean_text(item.get("role"), 20),
            "content": _clean_text(item.get("content"), 2400),
        }
        for item in messages[-16:]
        if isinstance(item, dict) and _clean_text(item.get("content"), 2400)
    ]


def _prompt_memory(memory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": _clean_text(item.get("kind"), 80),
            "content": item.get("content") if isinstance(item.get("content"), dict) else {},
        }
        for item in memory[:20]
        if isinstance(item, dict) and item.get("status") == "enabled"
    ]


def clarification_context(messages: list[dict[str, Any]]) -> dict[str, Any]:
    question_indexes: list[int] = []
    completed_rounds = 0
    for index, item in enumerate(messages):
        if not isinstance(item, dict) or _clean_text(item.get("role"), 20) != "assistant":
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if _clean_text(metadata.get("action"), 40).upper() == "ASK_USER":
            question_indexes.append(index)
            if any(
                isinstance(later, dict) and _clean_text(later.get("role"), 20) == "user"
                for later in messages[index + 1 :]
            ):
                completed_rounds += 1
    return {
        "required_before_search": completed_rounds == 0,
        "questions_asked": len(question_indexes),
        "completed_rounds": completed_rounds,
        "instruction": (
            "先询问 1-2 个未明确且最影响结果的问题，不得提出检索计划。"
            if completed_rounds == 0
            else "已完成至少一轮需求确认；仍有关键歧义时可以继续追问。"
        ),
    }


def _clarifying_questions(intent: dict[str, Any]) -> list[str]:
    normalized = normalize_intent_model(intent)
    questions: list[str] = []
    if not normalized["topic"] and not normalized["normalized_topic"]:
        questions.append("你希望检索的核心研究主题或具体问题是什么？")
    if normalized["research_goal"] == "mixed":
        questions.append("这次资料主要用于综述、复现、寻找创新点，还是做基准比较？")
    if not normalized["time_range"]:
        questions.append("时间范围希望限定近三年、近五年，还是不限年份？")
    if set(normalized["material_types"]) == MATERIAL_TYPES:
        questions.append("论文、代码、模型、数据集和基准中，哪些是必须拿到的？")
    if not normalized["quality_criteria"]:
        questions.append("质量上更看重顶会高引用，还是公开代码和可复现性？")

    goal_questions = {
        "implementation": [
            "代码范围上，你只接受作者官方仓库，还是维护良好的社区复现也可以？",
            "复现环境有什么硬约束，例如框架、GPU 显存、许可证或操作系统？",
        ],
        "innovation": [
            "你更希望寻找方法创新、系统优化，还是具体应用方向的创新点？",
            "资料质量上是否只看指定顶会顶刊，还是也接受高质量预印本？",
        ],
        "survey": [
            "综述更需要经典脉络，还是近年的最新进展和未来方向？",
            "你希望覆盖到什么粒度：代表性工作，还是尽量完整的技术谱系？",
        ],
        "benchmark": [
            "基准比较最关心哪些指标，例如准确率、延迟、吞吐量或资源消耗？",
            "是否有必须使用的数据集、硬件平台或基线方法？",
        ],
        "citation": [
            "这些引用将用于背景论证、方法依据，还是相关工作对比？",
            "是否有指定年份、期刊会议或引用影响力要求？",
        ],
        "dataset": [
            "数据集需要满足哪些任务、语言、规模、许可证或下载条件？",
            "你是否只接受可公开下载并带明确数据说明的数据集？",
        ],
        "model": [
            "模型需要满足哪些规模、框架、许可证或推理硬件限制？",
            "你更需要原始 checkpoint，还是带完整部署与评测说明的模型？",
        ],
    }
    questions.extend(goal_questions.get(normalized["research_goal"], []))
    return _clean_list(questions, limit=2, item_limit=240)


def _as_question(value: Any) -> str:
    text = _clean_text(value, 240).rstrip("。.!！?？;；")
    if not text:
        return ""
    return f"{text}？"


def _decision_with_visible_questions(
    decision: dict[str, Any],
    *,
    state: dict[str, Any],
) -> dict[str, Any]:
    if decision.get("action") != "ASK_USER":
        return decision
    intent = merge_intent_model(
        normalize_agent_state(state).get("intent_model"),
        decision.get("intent_patch"),
    )
    questions = _clean_list(
        decision.get("clarifying_questions"),
        limit=2,
        item_limit=240,
    )
    if not questions:
        questions = [_as_question(item) for item in intent.get("ambiguities") or []]
        questions = _clean_list(questions, limit=2, item_limit=240)
    if not questions:
        questions = _clarifying_questions(intent)
    visible = _clean_text(decision.get("assistant_message"), 4000)
    missing = [
        question
        for question in questions
        if question.rstrip("?？").casefold() not in visible.casefold()
    ]
    if missing:
        visible = _clean_text(
            visible
            + " "
            + " ".join(
                f"{index + 1}. {_as_question(question)}"
                for index, question in enumerate(missing)
            ),
            4000,
        )
    guarded = dict(decision)
    guarded["assistant_message"] = visible
    guarded["clarifying_questions"] = questions
    return guarded


def enforce_clarification_before_search(
    decision: dict[str, Any],
    *,
    state: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    context = clarification_context(messages)
    if decision.get("action") == "ASK_USER":
        return _decision_with_visible_questions(decision, state=state)
    if not context["required_before_search"]:
        return decision
    intent = merge_intent_model(
        normalize_agent_state(state).get("intent_model"),
        decision.get("intent_patch"),
    )
    questions = _clarifying_questions(intent)
    guarded = dict(decision)
    guarded["action"] = "ASK_USER"
    guarded["clarifying_questions"] = questions
    guarded["assistant_message"] = (
        "在制定检索计划前，我想先确认两点："
        + " ".join(f"{index + 1}. {question}" for index, question in enumerate(questions))
    )
    guarded["search_plan"] = {}
    readiness = (
        dict(decision.get("readiness"))
        if isinstance(decision.get("readiness"), dict)
        else {"score": 0.0, "missing": []}
    )
    readiness["score"] = min(_clean_confidence(readiness.get("score")), 0.75)
    readiness["missing"] = _clean_list(
        [*(readiness.get("missing") or []), "等待用户确认关键检索偏好"],
        limit=12,
    )
    guarded["readiness"] = readiness
    return _decision_with_visible_questions(guarded, state=state)


def build_agent_prompt(
    *,
    state: dict[str, Any],
    messages: list[dict[str, Any]],
    enabled_library_memory: list[dict[str, Any]],
    selected_sources: list[str],
    candidate_summary: list[dict[str, Any]],
) -> str:
    clean_state = normalize_agent_state(state)
    context = {
        "intent_model": clean_state["intent_model"],
        "readiness": clean_state["readiness"],
        "coverage_state": clean_state["coverage_state"],
        "feedback_summary": clean_state["feedback_summary"],
        "budget": clean_state["budget"],
        "selected_sources_hard_constraint": _clean_list(selected_sources, limit=80),
        "enabled_library_memory": _prompt_memory(enabled_library_memory),
        "recent_messages": _prompt_messages(messages),
        "clarification_policy": clarification_context(messages),
        "candidate_summary": [
            {
                "candidate_id": _clean_text(item.get("candidate_id") or item.get("stored_candidate_id"), 120),
                "title": _clean_text(item.get("title"), 300),
                "source": _clean_text(item.get("source"), 80),
                "year": _clean_text(item.get("year"), 10),
                "resource_type": _clean_text(item.get("resource_type"), 40),
            }
            for item in candidate_summary[:30]
            if isinstance(item, dict)
        ],
    }
    schema = {
        "action": "ASK_USER | UPDATE_INTENT | PROPOSE_SEARCH | FINAL",
        "assistant_message": "直接展示给用户的中文回复",
        "clarifying_questions": ["最多两个需要用户回答的问题，仅 ASK_USER 使用"],
        "intent_patch": {
            "topic": "string",
            "normalized_topic": "string",
            "research_goal": "survey | implementation | innovation | benchmark | citation | dataset | model | mixed",
            "must_include": ["string"],
            "nice_to_have": ["string"],
            "exclude_terms": ["string"],
            "material_types": ["paper | code | model | dataset | benchmark | website"],
            "time_range": {"start_year": 2020, "end_year": 2026},
            "preferred_sources": ["string"],
            "quality_criteria": ["string"],
            "application_context": "string",
            "ambiguities": ["string"],
            "confidence": 0.0,
        },
        "readiness": {"score": 0.0, "missing": ["string"]},
        "search_plan": {
            "target": "string",
            "query_intents": [
                {
                    "purpose": "string",
                    "keywords": ["query string"],
                    "material_type": "paper | code | model | dataset | benchmark | website",
                }
            ],
            "source_preferences": ["string"],
            "exclude_terms": ["string"],
        },
        "memory_suggestions": [
            {
                "kind": "preference | exclusion | workflow",
                "content": {"summary": "只保存可跨任务复用的结构化偏好"},
            }
        ],
    }
    return (
        "你是科研资料检索智能体的理解与决策层。"
        "你负责通过对话理解、追问、更新需求和提出检索计划；"
        "后端负责调用真实数据源、存储候选和控制导入。\n"
        "严格规则：\n"
        "1. 不要声称已经联网或已经检索，除非上下文中的 coverage/candidate 明确表明后端已完成。\n"
        "2. 不输出 URL、shell 命令、provider 调用或自动导入动作。\n"
        "3. 用户选择的数据源是硬约束；source_preferences 只是语义偏好。\n"
        "4. 首轮需求访谈是强制步骤。clarification_policy.required_before_search=true 时，"
        "action 必须是 ASK_USER，每次最多问两个最有价值的问题；"
        "不要重复用户已经明确的条件，即使需求看起来完整，也要追问最影响结果的潜在歧义。\n"
        "5. 至少完成一轮用户回答后，理解充分时才使用 PROPOSE_SEARCH，"
        "仍需用户点击确认后才会联网；如果仍有关键缺口，可以继续 ASK_USER。\n"
        "6. 检索计划必须适配研究目标，关键词要具体、可检索、避免机械添加后缀；"
        "中文需求应为英文源生成自然英文专业术语，可保留必要中文查询。\n"
        "7. query_intents 按资料类型表达要找什么，不直接决定底层 provider。"
        "每个资料类型给出彼此有区分度的检索词，通常 3-6 条，不足时宁可少而精。\n"
        "8. 长期记忆只能建议，不能自行启用；不要把私密项目名、密钥或完整聊天写入 memory_suggestions。\n"
        "9. 只输出一个合法 JSON 对象，不要 Markdown，不要解释，不要隐藏推理。\n\n"
        f"当前上下文：\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        f"输出结构：\n{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Codex 没有返回有效 JSON。")
        try:
            value = json.loads(clean[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Codex 返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Codex 决策必须是 JSON 对象。")
    return value


def normalize_agent_decision(value: Any) -> dict[str, Any]:
    raw = _extract_json_object(value) if isinstance(value, str) else value
    if not isinstance(raw, dict):
        raise ValueError("Codex 决策必须是对象。")
    action = _clean_text(raw.get("action"), 40).upper()
    if action not in AGENT_ACTIONS:
        raise ValueError(f"Codex 返回了不允许的 action：{action or '空'}")
    assistant_message = _clean_text(raw.get("assistant_message"), 4000)
    if not assistant_message:
        raise ValueError("Codex 决策缺少 assistant_message。")
    readiness = raw.get("readiness") if isinstance(raw.get("readiness"), dict) else {}
    decision = {
        "action": action,
        "assistant_message": assistant_message,
        "clarifying_questions": _clean_list(
            raw.get("clarifying_questions"),
            limit=2,
            item_limit=240,
        ),
        "intent_patch": raw.get("intent_patch") if isinstance(raw.get("intent_patch"), dict) else {},
        "readiness": {
            "score": _clean_confidence(readiness.get("score")),
            "missing": _clean_list(readiness.get("missing"), limit=12),
        },
        "search_plan": {},
        "memory_suggestions": [],
    }
    if action == "PROPOSE_SEARCH":
        plan = raw.get("search_plan") if isinstance(raw.get("search_plan"), dict) else {}
        query_intents: list[dict[str, Any]] = []
        for raw_intent in plan.get("query_intents") or []:
            if not isinstance(raw_intent, dict):
                continue
            material = _clean_text(raw_intent.get("material_type"), 40).lower()
            keywords = _clean_list(raw_intent.get("keywords"), limit=10, item_limit=240)
            if material not in MATERIAL_TYPES or not keywords:
                continue
            query_intents.append(
                {
                    "purpose": _clean_text(raw_intent.get("purpose"), 240),
                    "keywords": keywords,
                    "material_type": material,
                }
            )
        if not query_intents:
            raise ValueError("Codex 的检索计划没有有效 query_intents。")
        decision["search_plan"] = {
            "target": _clean_text(plan.get("target"), 600),
            "query_intents": query_intents,
            "source_preferences": _clean_list(plan.get("source_preferences"), limit=40),
            "exclude_terms": _clean_list(plan.get("exclude_terms"), limit=24),
        }
    suggestions = raw.get("memory_suggestions") if isinstance(raw.get("memory_suggestions"), list) else []
    decision["memory_suggestions"] = [
        {
            "kind": _clean_text(item.get("kind"), 80) or "preference",
            "content": item.get("content") if isinstance(item.get("content"), dict) else {},
        }
        for item in suggestions[:8]
        if isinstance(item, dict) and isinstance(item.get("content"), dict) and item.get("content")
    ]
    return decision


def apply_decision_to_state(state: Any, decision: dict[str, Any], *, turn_id: str) -> dict[str, Any]:
    current = normalize_agent_state(state)
    current["intent_model"] = merge_intent_model(
        current.get("intent_model"),
        decision.get("intent_patch"),
    )
    current["readiness"] = decision.get("readiness") or current.get("readiness")
    current["last_assistant_message"] = _clean_text(
        decision.get("assistant_message"),
        4000,
    )
    thread = current["thread"]
    thread["active_turn_id"] = ""
    thread["last_turn_id"] = turn_id
    thread["last_error"] = ""
    action = decision.get("action")
    if action == "PROPOSE_SEARCH":
        for pending in current["pending_actions"]:
            if pending.get("kind") == "search" and pending.get("status") == "pending":
                pending["status"] = "expired"
                pending["updated_at"] = now_iso()
        action_id = f"agent-action-{new_key(14).lower()}"
        current["pending_actions"].append(
            {
                "action_id": action_id,
                "kind": "search",
                "reason": decision.get("assistant_message") or "",
                "plan_preview": decision.get("search_plan") or {},
                "estimated_cost": {
                    "query_count": sum(
                        len(item.get("keywords") or [])
                        for item in (decision.get("search_plan") or {}).get("query_intents") or []
                        if isinstance(item, dict)
                    )
                },
                "status": "pending",
                "created_at": now_iso(),
            }
        )
        current["pending_actions"] = current["pending_actions"][-20:]
        thread["thread_status"] = "waiting_approval"
    elif action == "FINAL":
        thread["thread_status"] = "completed"
    else:
        thread["thread_status"] = "waiting_user"
    current["updated_at"] = now_iso()
    return normalize_agent_state(current)


def mark_turn_running(state: Any, turn_id: str) -> dict[str, Any]:
    current = normalize_agent_state(state)
    for pending in current["pending_actions"]:
        if pending.get("kind") == "search" and pending.get("status") == "pending":
            pending["status"] = "expired"
            pending["updated_at"] = now_iso()
    current["thread"] = {
        **current["thread"],
        "thread_status": "running",
        "active_turn_id": turn_id,
        "last_error": "",
    }
    current["updated_at"] = now_iso()
    return current


def mark_turn_failed(state: Any, turn_id: str, error: str) -> dict[str, Any]:
    current = normalize_agent_state(state)
    current["thread"] = {
        **current["thread"],
        "thread_status": "failed",
        "active_turn_id": "",
        "last_turn_id": turn_id,
        "last_error": _clean_text(error, 1200),
    }
    current["updated_at"] = now_iso()
    return current


def update_pending_action(
    state: Any,
    action_id: str,
    status: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = normalize_agent_state(state)
    clean_status = _clean_text(status, 40).lower()
    if clean_status not in {"approved", "rejected", "executed", "expired"}:
        raise ValueError("不支持的待确认操作状态。")
    matched: dict[str, Any] | None = None
    next_actions: list[dict[str, Any]] = []
    for item in current["pending_actions"]:
        value = dict(item)
        if value.get("action_id") == action_id:
            if value.get("status") != "pending":
                raise ValueError("该检索计划已处理，请使用最新的待确认计划。")
            value["status"] = clean_status
            value["updated_at"] = now_iso()
            matched = value
        next_actions.append(value)
    if not matched:
        raise ValueError("待确认的检索计划不存在。")
    current["pending_actions"] = next_actions
    current["thread"]["thread_status"] = (
        "searching" if clean_status in {"approved", "executed"} else "waiting_user"
    )
    current["updated_at"] = now_iso()
    return normalize_agent_state(current), matched
