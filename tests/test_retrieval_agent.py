from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_web_library import app_store, retrieval_agent, retrieval_agent_store, web
from zotero_web_library.sources import create_read_only_source
from zotero_web_library.web import create_app


def configured_codex(*_args, **_kwargs) -> dict[str, str]:
    return {
        "model": "gpt-5-codex",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
    }


def create_agent_job(client, library_id: str, *, topic: str = "我想复现推测解码") -> dict:
    response = client.post(
        f"/api/library/{library_id}/retrieval/guided-search-jobs",
        json={
            "topic": topic,
            "input_text": topic,
            "search_route": "agent",
            "mode": "quality",
            "time_range": {"preset": "10y"},
            "material_types": ["paper", "code", "benchmark"],
            "sources": ["crossref"],
            "limit_per_source": 5,
        },
    )
    assert response.status_code == 200
    return response.get_json()["job"]


def proposed_search_response() -> str:
    return json.dumps(
        {
            "action": "PROPOSE_SEARCH",
            "assistant_message": "我理解你要找可复现的推测解码论文、代码和基准。请确认后开始联网检索。",
            "intent_patch": {
                "topic": "推测解码复现",
                "normalized_topic": "speculative decoding reproducibility",
                "research_goal": "implementation",
                "must_include": ["speculative decoding"],
                "material_types": ["paper", "code", "benchmark"],
                "quality_criteria": ["提供开源实现", "有公开 benchmark"],
                "confidence": 0.91,
            },
            "readiness": {"score": 0.91, "missing": []},
            "search_plan": {
                "target": "寻找可复现的推测解码方法",
                "query_intents": [
                    {
                        "purpose": "核心论文",
                        "keywords": [
                            "speculative decoding draft model verification",
                            "assisted generation speculative decoding",
                        ],
                        "material_type": "paper",
                    },
                    {
                        "purpose": "公开实现",
                        "keywords": ["speculative decoding open source implementation"],
                        "material_type": "code",
                    },
                ],
                "source_preferences": ["crossref", "github"],
                "exclude_terms": [],
            },
            "memory_suggestions": [
                {
                    "kind": "workflow",
                    "content": {"summary": "优先寻找有开源实现和公开 benchmark 的资料"},
                }
            ],
        },
        ensure_ascii=False,
    )


def fake_candidate_search(query: str, **kwargs) -> dict:
    return {
        "query": query,
        "sources": kwargs["sources"],
        "candidates": [
            {
                "source": "crossref",
                "external_id": query,
                "item_type": "journalArticle",
                "resource_type": "paper",
                "title": f"Result for {query}",
                "year": "2025",
                "identifiers": {"doi": "10.1000/agent-result"},
                "item": {
                    "item_type": "journalArticle",
                    "fields": {"title": f"Result for {query}", "date": "2025"},
                    "creators": [],
                    "identifiers": {"doi": "10.1000/agent-result"},
                },
                "confidence": 0.9,
                "quality_score": 88,
                "coverage_tags": ["paper"],
                "authority_signals": {},
                "missing_authority_signals": [],
                "ai_evaluation": {
                    "score_source": "deterministic_rules",
                    "decision": "review",
                    "auto_select": False,
                },
            }
        ],
        "source_stats": {"crossref": {"ok": True, "count": 1, "error": ""}},
    }


def test_agent_job_waits_for_chat_and_explicit_search_approval(
    zotero_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("WEB_LIBRARY_RETRIEVAL_AGENT_INLINE", "1")
    monkeypatch.setenv("WEB_LIBRARY_RETRIEVAL_GUIDED_INLINE", "1")
    monkeypatch.setattr(web, "api_config_codex_for_library", configured_codex)
    monkeypatch.setattr(
        web,
        "run_codex_prompt",
        lambda **_kwargs: {
            "assistant_text": proposed_search_response(),
            "turn_id": "codex-turn-test",
            "usage": {},
        },
    )
    monkeypatch.setattr(web, "search_retrieval", fake_candidate_search)
    library = create_read_only_source(zotero_fixture, name="Agent Retrieval")
    client = create_app().test_client()

    job = create_agent_job(client, library["library_id"])
    assert job["status"] == "draft"
    assert job["run_ids"] == []

    turn_response = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "我要能复现的方案，优先论文、代码和 benchmark。"},
    )
    assert turn_response.status_code == 202

    clarification_response = client.get(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent/state"
    )
    assert clarification_response.status_code == 200
    clarification = clarification_response.get_json()
    assert clarification["agent_state"]["thread"]["thread_status"] == "waiting_user"
    assert clarification["agent_state"]["pending_actions"] == []
    assert "确认" in clarification["messages"][-1]["content"]
    assert clarification["messages"][-1]["metadata"]["action"] == "ASK_USER"

    answer_response = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "只接受作者官方仓库，使用 PyTorch，单张消费级 GPU 可以运行。"},
    )
    assert answer_response.status_code == 202

    state_response = client.get(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent/state"
    )
    assert state_response.status_code == 200
    payload = state_response.get_json()
    assert payload["agent_state"]["thread"]["thread_status"] == "waiting_approval"
    assert payload["agent_state"]["intent_model"]["research_goal"] == "implementation"
    assert payload["job"]["run_ids"] == []
    assert [item["role"] for item in payload["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    action = next(
        item for item in payload["agent_state"]["pending_actions"] if item["status"] == "pending"
    )
    assert action["estimated_cost"]["query_count"] == 3
    assert payload["memory"][0]["status"] == "suggested"

    approval_response = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-approvals",
        json={"action_id": action["action_id"], "decision": "approve"},
    )
    assert approval_response.status_code == 200
    approved = approval_response.get_json()
    assert approved["job"]["status"] == "completed"
    assert approved["job"]["run_ids"]
    assert approved["candidates"]
    assert approved["agent_state"]["thread"]["thread_status"] == "waiting_user"
    assert any(item["status"] == "executed" for item in approved["agent_state"]["pending_actions"])


def test_new_agent_conversation_persists_before_first_message(
    zotero_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("WEB_LIBRARY_RETRIEVAL_AGENT_INLINE", "1")
    monkeypatch.setattr(web, "api_config_codex_for_library", configured_codex)
    monkeypatch.setattr(
        web,
        "run_codex_prompt",
        lambda **_kwargs: {
            "assistant_text": json.dumps(
                {
                    "action": "ASK_USER",
                    "assistant_message": "先确认这次的研究主题和主要用途。",
                    "clarifying_questions": [
                        "这次要检索的具体研究主题是什么？",
                        "资料主要用于综述、复现还是寻找创新点？",
                    ],
                    "intent_patch": {"confidence": 0.2},
                    "readiness": {"score": 0.2, "missing": ["主题", "研究用途"]},
                },
                ensure_ascii=False,
            ),
            "turn_id": "codex-turn-new-conversation",
            "usage": {},
        },
    )
    library = create_read_only_source(zotero_fixture, name="New Agent Conversation")
    client = create_app().test_client()

    created_response = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs",
        json={
            "topic": "",
            "input_text": "",
            "empty_task": True,
            "search_route": "agent",
            "mode": "quality",
            "time_range": {"preset": "10y"},
            "material_types": ["paper", "code"],
            "sources": ["crossref"],
            "limit_per_source": 5,
        },
    )
    assert created_response.status_code == 200
    job = created_response.get_json()["job"]
    assert job["topic"] == "待确认研究需求"
    assert job["options"]["empty_task"] is True
    assert retrieval_agent_store.list_messages(library["library_id"], job["job_id"]) == []

    latest = client.get(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/latest?search_route=agent"
    ).get_json()["job"]
    assert latest["job_id"] == job["job_id"]

    first_turn = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "我想研究双臂机器人操作中的模仿学习。"},
    )
    assert first_turn.status_code == 202
    state = client.get(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent/state"
    ).get_json()
    assert state["job"]["topic"] == "我想研究双臂机器人操作中的模仿学习。"
    assert state["job"]["options"]["empty_task"] is False
    assert [item["role"] for item in state["messages"]] == ["user", "assistant"]
    assert state["agent_state"]["thread"]["thread_status"] == "waiting_user"


def test_agent_library_memory_requires_confirmation_and_is_injected_after_enable(
    zotero_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("WEB_LIBRARY_RETRIEVAL_AGENT_INLINE", "1")
    monkeypatch.setattr(web, "api_config_codex_for_library", configured_codex)
    prompts: list[str] = []

    def fake_codex(**kwargs):
        prompts.append(kwargs["prompt"])
        return {
            "assistant_text": json.dumps(
                {
                    "action": "ASK_USER",
                    "assistant_message": "你更重视速度还是可复现性？",
                    "intent_patch": {"topic": "推测解码", "confidence": 0.4},
                    "readiness": {"score": 0.4, "missing": ["质量偏好"]},
                    "memory_suggestions": [
                        {
                            "kind": "preference",
                            "content": {"summary": "优先可复现资料"},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            "turn_id": "codex-turn-memory",
            "usage": {},
        }

    monkeypatch.setattr(web, "run_codex_prompt", fake_codex)
    library = create_read_only_source(zotero_fixture, name="Agent Memory")
    client = create_app().test_client()
    job = create_agent_job(client, library["library_id"])

    first = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "优先找可复现的资料。"},
    )
    assert first.status_code == 202
    state_payload = client.get(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent/state"
    ).get_json()
    memory = state_payload["memory"][0]
    assert memory["status"] == "suggested"
    assert '"enabled_library_memory": []' in prompts[0]

    enabled_response = client.patch(
        f"/api/library/{library['library_id']}/retrieval/agent-memory/{memory['memory_id']}",
        json={"enabled": True},
    )
    assert enabled_response.status_code == 200
    assert enabled_response.get_json()["memory"]["status"] == "enabled"

    second = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "继续理解我的要求。"},
    )
    assert second.status_code == 202
    assert "优先可复现资料" in prompts[-1]


def test_agent_feedback_distinguishes_neutral_and_preference_updates(
    zotero_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("WEB_LIBRARY_RETRIEVAL_AGENT_INLINE", "1")
    monkeypatch.setenv("WEB_LIBRARY_RETRIEVAL_GUIDED_INLINE", "1")
    monkeypatch.setattr(web, "api_config_codex_for_library", configured_codex)
    monkeypatch.setattr(
        web,
        "run_codex_prompt",
        lambda **_kwargs: {
            "assistant_text": proposed_search_response(),
            "turn_id": "codex-turn-feedback",
            "usage": {},
        },
    )
    monkeypatch.setattr(web, "search_retrieval", fake_candidate_search)
    library = create_read_only_source(zotero_fixture, name="Agent Feedback")
    client = create_app().test_client()
    job = create_agent_job(client, library["library_id"])
    client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "开始规划。"},
    )
    client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "目标是复现，只接受有公开实现和 benchmark 的资料。"},
    )
    agent_payload = client.get(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent/state"
    ).get_json()
    action = next(item for item in agent_payload["agent_state"]["pending_actions"] if item["status"] == "pending")
    searched = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-approvals",
        json={"action_id": action["action_id"], "decision": "approve"},
    ).get_json()
    candidate_id = searched["candidates"][0]["stored_candidate_id"]

    duplicate = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/candidate-feedback",
        json={"candidate_id": candidate_id, "feedback_type": "duplicate"},
    )
    assert duplicate.status_code == 200
    duplicate_state = duplicate.get_json()["agent_state"]
    assert candidate_id in duplicate_state["feedback_summary"]["neutral_candidate_ids"]
    assert candidate_id not in duplicate_state["feedback_summary"]["negative_candidate_ids"]

    too_old = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/candidate-feedback",
        json={"candidate_id": candidate_id, "feedback_type": "too_old"},
    )
    assert too_old.status_code == 200
    assert "优先较新的资料" in too_old.get_json()["agent_state"]["intent_model"]["quality_criteria"]


def test_agent_unavailable_does_not_fake_a_rule_based_turn(
    zotero_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(
        web,
        "api_config_codex_for_library",
        lambda *_args, **_kwargs: {"model": "", "base_url": "", "api_key": ""},
    )
    library = create_read_only_source(zotero_fixture, name="Agent Unavailable")
    client = create_app().test_client()
    job = create_agent_job(client, library["library_id"])

    response = client.post(
        f"/api/library/{library['library_id']}/retrieval/guided-search-jobs/{job['job_id']}/agent-turns",
        json={"message": "帮我检索。"},
    )
    assert response.status_code == 503
    assert "未就绪" in response.get_json()["error"]
    assert retrieval_agent_store.list_messages(library["library_id"], job["job_id"]) == []


def test_agent_decision_schema_rejects_provider_execution_and_invalid_actions() -> None:
    with pytest.raises(ValueError, match="不允许"):
        retrieval_agent.normalize_agent_decision(
            {
                "action": "OPEN_URL",
                "assistant_message": "我去抓取网页。",
            }
        )


def test_agent_requires_one_clarification_round_before_search() -> None:
    decision = retrieval_agent.normalize_agent_decision(proposed_search_response())
    messages = [{"role": "user", "content": "帮我找可复现的推测解码资料。"}]

    guarded = retrieval_agent.enforce_clarification_before_search(
        decision,
        state=retrieval_agent.default_agent_state(),
        messages=messages,
    )

    assert guarded["action"] == "ASK_USER"
    assert guarded["search_plan"] == {}
    assert 1 <= len(guarded["clarifying_questions"]) <= 2
    assert "等待用户确认关键检索偏好" in guarded["readiness"]["missing"]

    answered_messages = [
        *messages,
        {
            "role": "assistant",
            "content": guarded["assistant_message"],
            "metadata": {"action": "ASK_USER"},
        },
        {"role": "user", "content": "只接受官方代码，使用 PyTorch。"},
    ]
    allowed = retrieval_agent.enforce_clarification_before_search(
        decision,
        state=retrieval_agent.default_agent_state(),
        messages=answered_messages,
    )
    assert allowed["action"] == "PROPOSE_SEARCH"


def test_agent_makes_ask_user_questions_visible_from_ambiguities() -> None:
    decision = retrieval_agent.normalize_agent_decision(
        {
            "action": "ASK_USER",
            "assistant_message": "开始检索前，还需要确认两个会影响范围的问题。",
            "intent_patch": {
                "topic": "推测解码候选树剪枝",
                "ambiguities": [
                    "候选树剪枝是否严格限定在 tree-based speculative decoding",
                    "是否纳入 lookahead decoding 中类似的候选分支裁剪机制",
                ],
            },
            "readiness": {"score": 0.7, "missing": ["研究边界"]},
        }
    )

    visible = retrieval_agent.enforce_clarification_before_search(
        decision,
        state=retrieval_agent.default_agent_state(),
        messages=[{"role": "user", "content": "找候选树剪枝论文"}],
    )

    assert visible["action"] == "ASK_USER"
    assert len(visible["clarifying_questions"]) == 2
    assert "候选树剪枝是否严格限定" in visible["assistant_message"]
    assert "是否纳入 lookahead decoding" in visible["assistant_message"]
    assert "？" in visible["assistant_message"]


def test_new_agent_turn_expires_unconfirmed_search_plan() -> None:
    state = retrieval_agent.default_agent_state()
    state["pending_actions"] = [
        {
            "action_id": "agent-action-old",
            "kind": "search",
            "status": "pending",
            "plan_preview": {},
        }
    ]

    running = retrieval_agent.mark_turn_running(state, "agent-turn-next")

    assert running["pending_actions"][0]["status"] == "expired"
    assert running["thread"]["thread_status"] == "running"


def test_agent_store_migrates_legacy_tables_without_clearing_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    app_store.ensure_app_store()
    with app_store.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE retrieval_agent_messages (
              message_id TEXT PRIMARY KEY,
              library_id TEXT NOT NULL,
              job_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              visibility TEXT NOT NULL DEFAULT 'session_only',
              retention_until TEXT NOT NULL DEFAULT '',
              intent_snapshot_json TEXT NOT NULL DEFAULT '{}',
              codex_turn_id TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE retrieval_agent_turns (
              turn_request_id TEXT PRIMARY KEY,
              library_id TEXT NOT NULL,
              job_id TEXT NOT NULL,
              user_message_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              intent_patch_json TEXT NOT NULL DEFAULT '{}',
              action_json TEXT NOT NULL DEFAULT '{}',
              error TEXT NOT NULL DEFAULT '',
              worker_token TEXT NOT NULL DEFAULT '',
              lease_expires_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE retrieval_agent_memory (
              memory_id TEXT PRIMARY KEY,
              library_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              content_json TEXT NOT NULL DEFAULT '{}',
              enabled INTEGER NOT NULL DEFAULT 0,
              confidence REAL NOT NULL DEFAULT 0,
              evidence_json TEXT NOT NULL DEFAULT '{}',
              source_job_id TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO retrieval_agent_memory
              (memory_id, library_id, kind, enabled, created_at, updated_at)
            VALUES ('memory-old', 'library-old', 'preference', 1, '2026-01-01', '2026-01-01');
            """
        )
        conn.commit()

    retrieval_agent_store.ensure_store()

    with app_store.connect() as conn:
        message_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(retrieval_agent_messages)")
        }
        turn_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(retrieval_agent_turns)")
        }
        memory_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(retrieval_agent_memory)")
        }
        old_memory = conn.execute(
            "SELECT status FROM retrieval_agent_memory WHERE memory_id = 'memory-old'"
        ).fetchone()

    assert "metadata_json" in message_columns
    assert {"turn_id", "result_json"} <= turn_columns
    assert "turn_request_id" not in turn_columns
    assert "status" in memory_columns
    assert old_memory["status"] == "enabled"
