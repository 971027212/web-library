from __future__ import annotations

from pathlib import Path

import pytest

from zotero_web_library.sources import create_local_copy
from zotero_web_library.web import create_app


def test_source_index_contains_service_path_and_upload_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    index_html = (root / "src" / "zotero_web_library" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (root / "src" / "zotero_web_library" / "static" / "app.js").read_text(encoding="utf-8")
    app_css = (root / "src" / "zotero_web_library" / "static" / "app.css").read_text(encoding="utf-8")

    assert "光明科研工作站" in index_html
    assert "AI文献综述分析平台" in index_html
    assert "本地只读模式" in index_html
    assert "副本编辑模式" in index_html
    assert "选择路径" in index_html
    assert "复制并编辑" in index_html
    assert "上传文件夹" in index_html
    assert "进入文库" in index_html
    assert "当前选择目录" in index_html
    assert "子目录" in index_html
    assert "features_page" not in index_html
    assert "data-upload-progress" in index_html
    assert "data-server-path-modal" in index_html
    assert "Standalone Zotero Web Library" not in index_html
    assert "Zotero 网页文库" not in index_html
    assert "连接只读模式" not in index_html
    assert "本地副本模式" not in index_html
    assert "class=\"library-list-link\"" not in index_html
    assert "/api/server-paths/roots" in app_js
    assert "/api/server-paths/list" in app_js
    assert "/api/sources/upload-folder" in app_js
    assert "SERVER_VIRTUAL_ROOT" in app_js
    assert "进入子目录" in app_js
    assert "data-server-path-use" not in app_js
    assert "webkitdirectory" in app_js
    assert "XMLHttpRequest" in app_js
    assert ".upload-progress-bar" in app_css
    assert ".library-enter-btn" in app_css
    assert ".path-entry-enter" in app_css


def test_frontend_contains_refined_interaction_hooks() -> None:
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "src" / "zotero_web_library" / "static" / "app.js").read_text(encoding="utf-8")
    app_css = (root / "src" / "zotero_web_library" / "static" / "app.css").read_text(encoding="utf-8")
    library_html = (root / "src" / "zotero_web_library" / "templates" / "library.html").read_text(encoding="utf-8")
    knowledge_html = (root / "src" / "zotero_web_library" / "templates" / "knowledge.html").read_text(encoding="utf-8")
    knowledge_js = (root / "src" / "zotero_web_library" / "static" / "knowledge.js").read_text(encoding="utf-8")
    api_config_html = (root / "src" / "zotero_web_library" / "templates" / "api_config.html").read_text(encoding="utf-8")
    reader_html = (root / "src" / "zotero_web_library" / "templates" / "reader.html").read_text(encoding="utf-8")
    reader_js = (root / "src" / "zotero_web_library" / "static" / "reader.js").read_text(encoding="utf-8")
    reader_css = (root / "src" / "zotero_web_library" / "static" / "reader.css").read_text(encoding="utf-8")
    features_html = (root / "src" / "zotero_web_library" / "templates" / "features.html").read_text(encoding="utf-8")
    assert "data-current-tag-toggle" in app_js
    assert "data-shortcut-add-tag" in app_js
    assert "data-shortcut-form" in app_js
    assert "form-action-btn" in app_js
    assert "\"remark\", \"备注\"" in app_js
    assert "\"title_zh\", \"中文标题\"" in app_js
    assert "\"abstract_zh\", \"中文摘要\"" in app_js
    assert "tag-delete-btn" in app_js
    assert "tag-icon-btn" in app_js
    assert "function renderTagPopover" in app_js
    assert "function rerenderActiveTagPopover" in app_js
    assert "function renderStructuredCell" in app_js
    assert "function ratingLabelFromValues" in app_js
    assert "const RATING_STAR = \"⭐\"" in app_js
    assert "const RATING_CONTROL_STAR = \"⭐\"" in app_js
    assert "const ITEM_TYPE_META" in app_js
    assert "const ITEM_TYPE_ALIASES" in app_js
    assert "function normalizeItemTypeKey" in app_js
    assert "function itemTypeMeta" in app_js
    assert "function itemTypeLabel" in app_js
    assert "computerProgram" in app_js
    assert "webpage" in app_js
    assert "magazineArticle" in app_js
    assert "newspaperArticle" in app_js
    assert "webPage: \"webpage\"" in app_js
    assert "software: \"computerProgram\"" in app_js
    assert "data-edit-structured-cell" in app_js
    assert "data-save-structured-cell" in app_js
    assert "/structured-field" in app_js
    assert "selectedItemKeys: new Set()" in app_js
    assert "data-toggle-select-all" in app_js
    assert "data-row-select" in app_js
    assert "function currentDetailItem" in app_js
    assert "function bulkActionState" in app_js
    assert "function renderBulkActionStates" in app_js
    assert "aria-disabled" in app_js
    assert "请点击任意条目后操作" in app_js
    assert "请勾选任意数量条目后操作" in app_js
    assert "请点击有可读PDF的条目后操作" in app_js
    assert "notifyFeatureInProgress" in app_js
    assert "data-selected-count" in app_js
    assert "data-bulk-action" in app_js
    assert "删除条目" in library_html
    assert "导入知识库" in library_html
    assert "data-bulk-action=\"import-knowledge\"" in library_html
    assert "文献矩阵" not in library_html
    assert "知识库问答" not in library_html
    assert "data-bulk-action=\"move-items\"" in library_html
    assert "附件编辑" in library_html
    assert "文献研读" in library_html
    assert "添加附件" not in library_html
    assert "data-bulk-action=\"edit-attachments\"" in library_html
    assert "data-bulk-action=\"read-paper\"" in library_html
    assert "features_page" in library_html
    assert "多源检索" in library_html
    assert "knowledge_page" in library_html
    assert "knowledge_page" in api_config_html
    assert "knowledge_page" in reader_html
    assert "knowledge_page" in features_html
    assert "api_config_page" in library_html
    assert "API 配置" in library_html
    assert "title=\"知识库\"" in knowledge_html
    assert "data-knowledge-page" in knowledge_html
    assert "data-knowledge-workbench" in knowledge_html
    assert "data-knowledge-list" in knowledge_html
    assert "data-knowledge-matrix-head" in knowledge_html
    assert "data-knowledge-matrix-body" in knowledge_html
    assert "data-knowledge-placeholder-action=\"create\"" in knowledge_html
    assert "新建知识库" in knowledge_html
    assert "data-api-config-page" in api_config_html
    assert "data-api-config-panel" in api_config_html
    assert "api_config_page" in features_html
    assert "API 配置" in features_html
    assert "function setupApiConfigPage" in app_js
    assert "/api-config" in app_js
    assert "data-save-api-config" in app_js
    assert "data-toggle-api-config-secrets" in app_js
    assert "data-check-api-config" in app_js
    assert "模型名称" in app_js
    assert "请求地址" in app_js
    assert "API Key" in app_js
    assert "Codex 模型配置" in app_js
    assert "data-codex-config-form" in app_js
    assert "data-save-codex-config" in app_js
    assert "data-toggle-codex-config-secret" in app_js
    assert "data-embedding-config-form" in app_js
    assert "data-save-embedding-config" in app_js
    assert "data-toggle-embedding-config-secret" in app_js
    assert "/rag/embeddings/config" in app_js
    assert "保存 Codex 设置" in app_js
    assert "Reasoning Effort" in app_js
    assert "openai-codex" in app_js
    assert "模型档案" not in app_js
    assert "新增档案" not in app_js
    assert "删除当前档案" not in app_js
    assert "Runtime Kind" not in app_js
    assert "Selected Agent" not in app_js
    assert "Reasoning Summary" not in app_js
    assert "Tool Policy" not in app_js
    assert "启用流式输出" not in app_js
    assert "GitHub Token" in app_js
    assert "HuggingFace Token" in app_js
    assert "Zenodo Token" in app_js
    assert "MinerU PDF 解析" in app_js
    assert "name=\"mineru_api_key\"" in app_js
    assert "name=\"mineru_base_url\"" in app_js
    assert "data-mineru-config-form" in app_js
    assert "data-save-mineru-config" in app_js
    assert "data-toggle-mineru-config-secret" in app_js
    assert "data-embedding-index" in knowledge_js
    assert "data-embedding-rebuild" in knowledge_js
    assert "内容未变化的 chunk 会保留并复用现有 embedding" in knowledge_js
    assert "强制重建当前知识库" in knowledge_js
    assert "补齐全库语义索引" in knowledge_js
    assert 'knowledge_base_id: force ? knowledgeState.activeId : ""' in knowledge_js
    assert "现有文档索引和 embedding 未改动" in app_js
    assert 'await postJSON(`/api/library/${state.libraryId}/rag/index`, {});' not in app_js
    assert "/embeddings/status" in knowledge_js
    assert "/embeddings/index" in knowledge_js
    assert 'response_mode: "async"' in knowledge_js
    assert "function pollKnowledgeChatRun" in knowledge_js
    assert "function cancelKnowledgeChat" in knowledge_js
    assert "function restartKnowledgeChat" in knowledge_js
    assert "data-restart-agent-run" in knowledge_js
    assert "/restart`" in knowledge_js
    assert "function renderKnowledgeAgentTrace" in knowledge_js
    assert "knowledge-citation-chip" in knowledge_js
    assert ".knowledge-agent-timeline" in app_css
    assert ".knowledge-run-restart" in app_css
    assert ".api-config-field-grid" in app_css
    assert ".knowledge-embedding-panel" in app_css
    assert "data-attachment-editor-modal" in library_html
    assert "data-reader-pdf-picker-modal" in library_html
    assert "data-delete-items-modal" in library_html
    assert "data-move-items-modal" in library_html
    assert "data-delete-items-form" in app_js
    assert "data-move-items-form" in app_js
    assert "/items/delete" in app_js
    assert "/items/move" in app_js
    assert "state.attachmentEditorItemKey = item.key" in app_js
    assert "data-add-file-attachment-form" in app_js
    assert "data-add-url-attachment-form" in app_js
    assert "data-edit-attachment-name" in app_js
    assert "data-delete-selected-attachments" in app_js
    assert "/attachments/file" in app_js
    assert "/attachments/url" in app_js
    assert "function openReadPaper" in app_js
    assert "currentDetailItem()" in app_js
    assert "currentItemPdfAttachments" in app_js
    assert "data-reader-pdf-picker" in app_js
    assert "data-reader-pdf-picker-form" in app_js
    assert "/pdf-attachments" in app_js
    assert "文献研读仅支持勾选 1 条文献" not in app_js
    assert "data-collection-menu" in app_js
    assert "data-rename-collection" in app_js
    assert "data-move-collection" in app_js
    assert "data-delete-collection" in app_js
    assert "data-create-child-collection" in app_js
    assert "key: \"__root\", name: \"根目录\"" in app_js
    assert "在根目录下新建文件夹" in app_js
    assert "manageable-root" in app_js
    assert "data-membership-form" not in app_js
    assert "data-create-collection-form" not in library_html
    assert "data-add-item-modal" in library_html
    assert "data-export-citation-modal" in library_html
    assert "data-import-identifier-form" in app_js
    assert "data-import-text-form" in app_js
    assert "/items/import-identifier" in app_js
    assert "/items/import-text" in app_js
    assert "data-add-item-mode=\"retrieval\"" not in app_js
    assert "function renderRetrievalPage" in app_js
    assert "function setupRetrievalPage" in app_js
    assert "function renderSimpleRetrievalMain" in app_js
    assert "retrievalSearchRoute: \"natural_language\"" in app_js
    assert "[\"keyword\", \"natural_language\", \"agent\"].includes(route)" in app_js
    assert "[\"natural_language\", \"keyword\", \"agent\"].map" in app_js
    assert "data-retrieval-route=\"${route}\"" in app_js
    assert "search_route: searchRoute" in app_js
    assert "planner_version: \"v4\"" in app_js
    assert "use_ai_planning: searchRoute !== \"keyword\"" in app_js
    assert "function renderRetrievalGuidedActionButtons" in app_js
    assert "name=\"guided_action\" value=\"plan\"" in app_js
    assert "name=\"guided_action\" value=\"search\"" in app_js
    assert "guidedAction !== \"search\"" in app_js
    assert "const showSearchAction = state.retrievalGuidedPlanDraft || guidedActive || state.addItemBusy" in app_js
    assert "data-guided-limit-per-source" in app_js
    assert "data-guided-source-limit" in app_js
    assert "data-guided-query-limit" not in app_js
    assert "currentGuidedQueryLimit" not in app_js
    assert "limit_per_source: currentGuidedLimitPerSource()" in app_js
    assert "source_limits: guidedSourceLimitsForSubmit(sources)" in app_js
    assert "function renderGuidedSourceLimits" in app_js
    assert "function retrievalSourceTypeLabels" in app_js
    assert "[\"coverage\", \"全覆盖\"]" not in app_js
    assert "/retrieval/guided-search-plan" in app_js
    assert "function renderRetrievalSurface" in app_js
    assert "function renderRetrievalGuidedPlanEditor" in app_js
    assert "function renderRetrievalGuidedEvents" in app_js
    assert "data-guided-event-list" in app_js
    assert "data-guided-plan-group-queries" in app_js
    assert "function retrievalGuidedPlanAiStatus" in app_js
    assert "AI 规划失败，当前使用规则兜底" not in app_js
    assert "AI 已参与规划，部分类型规则补足" in app_js
    assert "function retrievalGuidedGroupPlanningStatus" in app_js
    assert "该源规则补足" not in app_js
    assert "该类型规则补足" in app_js
    assert "function retrievalGuidedGroupSourcesLabel" in app_js
    assert "规则兜底生成" in app_js
    assert "AI 检索计划已生成。请确认或编辑后再开始检索。" in app_js
    assert "function renderAgentRetrievalPanel" in app_js
    assert "确认并开始检索" in app_js
    assert "data-agent-chat-form" in app_js
    assert "agent-approvals" in app_js
    assert "candidate-feedback" in app_js
    assert "只有你确认启用后，未来任务才会使用" in app_js
    assert 'turnBusy || status.ready === false || state.retrievalAgentBusy' not in app_js
    assert 'turnBusy || state.retrievalAgentBusy ? "disabled" : ""' in app_js
    assert 'await loadRetrievalAgentStatus({ check: true, silent: true });' in app_js
    assert "检查配置并发送" in app_js
    assert "retrieval-route-tabs" in app_css
    assert "grid-template-columns: repeat(2, minmax(72px, 1fr));" in app_css
    assert "guided-source-limit-grid" in app_css
    assert ".guided-source-limit-grid em" in app_css
    assert "guided-plan-ai-status" in app_css
    assert "guided-plan-source-status" in app_css
    assert "guided-event-list" in app_css
    assert ".guided-event.success strong" in app_css
    assert "guided-plan-editor" in app_css
    assert "retrieval-agent-workbench" in app_css
    assert "retrieval-agent-layout" in app_css
    assert "智能体会先追问关键条件；回答后再形成检索计划。" in app_js
    assert "async function startNewRetrievalAgentTask()" in app_js
    assert 'createRetrievalAgentJob("", { emptyTask: true })' in app_js
    assert "empty_task: emptyTask" in app_js
    assert '"新对话已创建"' in app_js
    assert "请描述这次要检索的内容，智能体会先向你提问。" in app_js
    assert "function delegatedRetrievalSubmitEvent" in app_js
    assert "currentTarget: form" in app_js
    assert "submitRetrievalAgentMessage(delegatedEvent)" in app_js
    assert (
        'if (event.target.matches("[data-agent-chat-form]")) {\n'
        "      submitRetrievalAgentMessage(event);"
    ) not in app_js
    assert "submitRetrievalSearch(delegatedEvent)" in app_js
    assert "SIMPLE_RETRIEVAL_SOURCE_CATEGORIES" in app_js
    assert "function renderSimpleRetrievalSourceCategory" in app_js
    assert "论文文献" in app_js
    assert "数据集 / 软件 / 代码对象" in app_js
    assert "本地 / 内部系统" in app_js
    assert "simple-retrieval-guide" in app_js
    assert "retrieval-advanced" in app_js
    assert "高级设置" in app_js
    assert "function renderRetrievalSourceConfigGuide" in app_js
    assert "function renderRetrievalSourceConfigHeader" in app_js
    assert "公共源按资料类型分类" in app_js
    assert "url_template：检索接口" in app_js
    assert "保存配置" in app_js
    assert "data-retrieval-search-form" in app_js
    assert "data-retrieval-candidate-check" in app_js
    assert "data-select-retrieval-candidates" in app_js
    assert "function setRetrievalCandidateSelection" in app_js
    assert "retrievalDeletedCandidateKeys: new Set()" in app_js
    assert "function deleteSelectedRetrievalCandidates" in app_js
    assert "data-delete-selected-retrieval-candidates" in app_js
    assert "AI 自动决定检索词数量" not in app_js
    assert "data-import-retrieval-selected" in app_js
    assert "function retrievalSourceSetupText" in app_js
    assert "config_mode" in app_js
    assert "function formatRateLimitSeconds" in app_js
    assert "function renderCandidateAiEvaluation" in app_js
    assert "retrievalAiEvaluationSummary" in app_js
    assert "data-score-retrieval-candidates-ai" in app_js
    assert "function scoreRetrievalCandidatesWithAi" in app_js
    assert "/retrieval/ai-scoring-jobs" in app_js
    assert "function retrievalCandidateIsAiRecommended" in app_js
    assert "function applyRetrievalAiScoringJob" in app_js
    assert "function loadLatestRetrievalAiScoringJob" in app_js
    assert "后台 AI 推荐排序中" in app_js
    assert "data-stop-retrieval-ai-scoring" in app_js
    assert "function stopRetrievalAiScoring" in app_js
    assert "AbortController" in app_js
    assert "function renderRetrievalPanel" not in app_js
    assert "function renderSimpleAiQueryPlan" not in app_js
    assert "data-simple-ai-query-plan" not in app_js
    assert "simplePlanBatchJobId" not in app_js
    assert "retrievalBatchMode" not in app_js
    assert "data-simple-batch-mode" not in app_js
    assert "data-simple-source-limit" not in app_js
    assert ".simple-ai-plan" not in app_css
    assert ".simple-plan-source-limits" not in app_css
    assert "调用多源接口" not in app_js
    assert 'data-select-retrieval-candidates="ai"' in app_js
    assert "function retrievalCandidateTypeMeta" in app_js
    assert "function renderRetrievalZoteroPreview" in app_js
    assert "retrieval-type-pill" in app_js
    assert "retrieval-zotero-preview" in app_js
    assert "/retrieval/guided-search-jobs/${encodeURIComponent(cleanJobId)}/candidates" in app_js
    assert "retrievalRunId" in app_js
    assert "retrievalRuns" in app_js
    assert "retrievalSummary" in app_js
    assert "function renderRetrievalSummary" in app_js
    assert "function loadRetrievalSummary" in app_js
    assert "\"import-knowledge\", \"导入知识库\"" in app_js
    assert "setupKnowledgePage();" in knowledge_js
    assert "knowledge-workbench" in knowledge_js
    assert "loadMatrixState" in knowledge_js
    assert "data-run-reading-matrix" in knowledge_html
    assert "data-stop-reading-matrix" in knowledge_html
    assert "conversationId" in knowledge_js
    assert "resetKnowledgeConversation" in knowledge_js
    assert "activeKnowledgeBase" in knowledge_js
    assert "loadKnowledgeConversation" in knowledge_js
    assert "/chat/history?" in knowledge_js
    assert "function renderKnowledgeToolTrace" in knowledge_js
    assert "检索步骤" in knowledge_js
    assert ".knowledge-workbench" in app_css
    assert ".knowledge-create-btn" in app_css
    assert ".knowledge-tool-trace" in app_css
    assert ".matrix-table" in app_css

    assert "function downloadRetrievalSummaryReport" in app_js
    assert "retrievalLocalPaths" in app_js
    assert "retrievalLocalFieldMap" in app_js
    assert "retrievalLocalPreview" in app_js
    assert "fieldMapText" in app_js
    assert "field_map_text" in app_js
    assert "retrievalHttpJsonConfig" in app_js
    assert "retrievalHttpJsonTemplates" in app_js
    assert "retrievalHttpJsonPreview" in app_js
    assert "retrievalSqliteConfig" in app_js
    assert "retrievalSqlitePreview" in app_js
    assert "retrievalManifestConfig" in app_js
    assert "retrievalManifestPreview" in app_js
    assert "retrievalBatchJobs" in app_js
    assert "function renderRetrievalLocalConfig" in app_js
    assert "function renderRetrievalLocalConfigWithPreview" in app_js
    assert "function renderRetrievalLocalPreview" in app_js
    assert "function renderRetrievalHttpJsonConfig" in app_js
    assert "function renderRetrievalHttpJsonPreview" in app_js
    assert "function renderRetrievalSqliteConfig" in app_js
    assert "function renderRetrievalSqlitePreview" in app_js
    assert "function renderRetrievalManifestConfig" in app_js
    assert "function renderRetrievalManifestPreview" in app_js
    assert "retrieval-local-preview-quality" in app_js
    assert "retrieval-local-preview-issues" in app_js
    assert "coverage" in app_js
    assert "recommendations" in app_js
    assert "function loadRetrievalLocalPreview" in app_js
    assert "function suggestRetrievalLocalFieldMap" in app_js
    assert "function suggestedLocalFieldMapFromPreview" in app_js
    assert "function loadRetrievalHttpJsonConfig" in app_js
    assert "function loadRetrievalHttpJsonTemplates" in app_js
    assert "function applyRetrievalHttpJsonTemplate" in app_js
    assert "function suggestRetrievalHttpJsonFieldMap" in app_js
    assert "function loadRetrievalHttpJsonPreview" in app_js
    assert "function saveRetrievalHttpJsonConfig" in app_js
    assert "await loadRetrievalHttpJsonPreview({ silent: false })" in app_js
    assert "function clearRetrievalHttpJsonConfig" in app_js
    assert "function loadRetrievalSqliteConfig" in app_js
    assert "function loadRetrievalSqliteTemplates" in app_js
    assert "function applyRetrievalSqliteTemplate" in app_js
    assert "function suggestRetrievalSqliteFieldMap" in app_js
    assert "function loadRetrievalSqlitePreview" in app_js
    assert "function saveRetrievalSqliteConfig" in app_js
    assert "await loadRetrievalSqlitePreview({ silent: false })" in app_js
    assert "function clearRetrievalSqliteConfig" in app_js
    assert "function loadRetrievalManifestConfig" in app_js
    assert "function loadRetrievalManifestTemplates" in app_js
    assert "function applyRetrievalManifestTemplate" in app_js
    assert "function suggestRetrievalManifestFieldMap" in app_js
    assert "function applyRetrievalFieldMapSuggestionToConfig" in app_js
    assert "function applyRetrievalReadinessFieldMapSuggestionToConfig" in app_js
    assert "function loadRetrievalManifestPreview" in app_js
    assert "function saveRetrievalManifestConfig" in app_js
    assert "await loadRetrievalManifestPreview({ silent: false })" in app_js
    assert "function clearRetrievalManifestConfig" in app_js
    assert "function renderRetrievalBatchPanel" in app_js
    assert "function formatRetrievalEta" in app_js
    assert "function submitRetrievalBatch" in app_js
    assert "function loadRetrievalBatchJobs" in app_js
    assert "function pauseRetrievalBatch" in app_js
    assert "function resumeRetrievalBatch" in app_js
    assert "function cancelRetrievalBatch" in app_js
    assert "function retryRetrievalBatchFailures" in app_js
    assert "function downloadRetrievalBatchReport" in app_js
    assert "function downloadRetrievalQueryPlanReport" in app_js
    assert "data-report-scope=\"sources\"" in app_js
    assert "SRC CSV" in app_js
    assert "function downloadRetrievalSourceSetupReport" in app_js
    assert "data-pause-retrieval-batch" in app_js
    assert "data-resume-retrieval-batch" in app_js
    assert "data-cancel-retrieval-batch" in app_js
    assert "data-retry-retrieval-batch" in app_js
    assert "data-download-retrieval-batch-report" in app_js
    assert "function loadRetrievalLocalPaths" in app_js
    assert "function saveRetrievalLocalPaths" in app_js
    assert "data-suggest-retrieval-local-field-map" in app_js
    assert "retrievalSourceInfo" in app_js
    assert "retrievalSourcesChecking" in app_js
    assert "retrievalModelStatus" in app_js
    assert "function loadRetrievalModelStatus" in app_js
    assert "function setupRetrievalRehearsalKit" in app_js
    assert "function validateRetrievalRehearsalRun" in app_js
    assert "retrievalReadiness" in app_js
    assert "retrievalReadinessBusy" in app_js
    assert "function renderRetrievalReadiness" in app_js
    assert "retrievalOnboarding" in app_js
    assert "retrievalOnboardingBusy" in app_js
    assert "function renderRetrievalOnboarding" in app_js
    assert "function renderRetrievalOnboardingGates" in app_js
    assert "function renderRetrievalOnboardingSourceEvidence" in app_js
    assert "function retrievalSourceEvidenceDiagnostic" in app_js
    assert "function loadRetrievalOnboarding" in app_js
    assert "retrieval-onboarding-actions" in app_js
    assert "Batch report" in app_js
    assert "Source CSV" in app_js
    assert "source_evidence" in app_js
    assert "acceptance_gates" in app_js
    assert "import_readiness" in app_js
    assert "import ready" in app_js
    assert "data-retrieval-onboarding-gates" in app_js
    assert "data-download-retrieval-gate-artifact" in app_js
    assert "function downloadRetrievalGateArtifact" in app_js
    assert "retrievalGateArtifactFallbackFilename" in app_js
    assert "Unsupported onboarding artifact endpoint" in app_js
    assert "source_gap" in app_js
    assert "low_sample" in app_js
    assert "query samples" in app_js
    assert "source coverage" in app_js
    assert "config evidence" in app_js
    assert "batch_config_context_status" in app_js
    assert "config_context_status" in app_js
    assert "source_errors" in app_js
    assert "source errors" in app_js
    assert "field_map_suggestion" in app_js
    assert "suggested_field_count" in app_js
    assert "function loadRetrievalReadiness" in app_js
    assert "function downloadRetrievalReadinessReport" in app_js
    assert "function downloadRetrievalTuningReport" in app_js
    assert "function downloadRetrievalOnboardingReport" in app_js
    assert "function downloadRetrievalOnboardingPackage" in app_js
    assert "function downloadRetrievalConfigBundle" in app_js
    assert "retrievalConfigBundleText" in app_js
    assert "function renderRetrievalConfigBundleImport" in app_js
    assert "function dryRunRetrievalConfigBundleImport" in app_js
    assert "function importRetrievalConfigBundle" in app_js
    assert "function retrievalConfigBundleResultCsv" in app_js
    assert "function downloadRetrievalConfigBundleResultCsv" in app_js
    assert "retrieval-config-bundle-dry-run.csv" in app_js
    assert "result,source,status,action,reason,configured,dry_run,bundle_schema" in app_js
    assert "retrievalSourceIntakeInput" in app_js
    assert "function renderRetrievalSourceIntake" in app_js
    assert "function analyzeRetrievalSourceIntake" in app_js
    assert "function downloadRetrievalSourceIntakeReport" in app_js
    assert "function applyRetrievalSourceIntakeToFieldMapLab" in app_js
    assert "function applyRetrievalSourceIntakeToConfig" in app_js
    assert "retrievalFieldMapLabSource" in app_js
    assert "retrievalFieldMapLabUseAi" in app_js
    assert "function renderRetrievalFieldMapLab" in app_js
    assert "function suggestRetrievalFieldMapLab" in app_js
    assert "function downloadRetrievalFieldMapReport" in app_js
    assert "function downloadRetrievalConfiguredFieldMapReport" in app_js
    assert "function applyRetrievalFieldMapLabDraft" in app_js
    assert "function retrievalFieldMapLabSamples" in app_js
    assert "/retrieval/sources" in app_js
    assert "/retrieval/sources/report" in app_js
    assert "/retrieval/onboarding?" in app_js
    assert "/retrieval/onboarding/report" in app_js
    assert "/retrieval/onboarding/package" in app_js
    assert "/retrieval/config-bundle/download" in app_js
    assert "/retrieval/config-bundle?dry_run=1" in app_js
    assert "/retrieval/source-intake" in app_js
    assert "/retrieval/source-intake/report" in app_js
    assert "/retrieval/rehearsal/setup" in app_js
    assert "/retrieval/rehearsal/validate" in app_js
    assert "validation_summary" in app_js
    assert "/retrieval/model-status" in app_js
    assert "/retrieval/field-map/suggest" in app_js
    assert "/retrieval/field-map/report" in app_js
    assert "/retrieval/readiness" in app_js
    assert "/retrieval/readiness/report" in app_js
    assert "/retrieval/query-plan" in app_js
    assert "/retrieval/query-plan/report" in app_js
    assert "/retrieval/tuning/report" in app_js
    assert "/retrieval/summary" in app_js
    assert "/retrieval/summary/report" in app_js
    assert "/retrieval/batches/" in app_js
    assert "/retrieval/local-files" in app_js
    assert "/retrieval/local-files/preview" in app_js
    assert "/retrieval/local-files/field-map/suggest" in app_js
    assert "/field-map/report" in app_js
    assert "/retrieval/http-json" in app_js
    assert "/retrieval/http-json/templates" in app_js
    assert "/retrieval/http-json/field-map/suggest" in app_js
    assert "/retrieval/http-json/preview" in app_js
    assert "data-check-retrieval-sources" in app_js
    assert "data-setup-retrieval-rehearsal" in app_js
    assert "data-validate-retrieval-rehearsal" in app_js
    assert "data-download-retrieval-source-setup" in app_js
    assert "data-check-retrieval-readiness" in app_js
    assert "data-draft-retrieval-batch-queries" in app_js
    assert "function draftRetrievalBatchQueries" in app_js
    assert "data-download-retrieval-query-plan" in app_js
    assert "PLAN RPT" in app_js
    assert "function unavailableRetrievalSources" in app_js
    assert "function availableRetrievalSources" in app_js
    assert "function unavailableRetrievalSourceMessage" in app_js
    assert "所选数据源暂不可用" in app_js
    assert "data-download-retrieval-field-map-report" in app_js
    assert "data-download-retrieval-configured-field-map" in app_js
    assert "data-download-retrieval-readiness" in app_js
    assert "data-download-retrieval-tuning" in app_js
    assert "data-check-retrieval-onboarding" in app_js
    assert "data-download-retrieval-onboarding" in app_js
    assert "data-download-retrieval-onboarding-package" in app_js
    assert "PLAN coverage" in app_js
    assert "data-retrieval-onboarding-source-evidence" in app_js
    assert "data-download-retrieval-config-bundle" in app_js
    assert "data-retrieval-config-bundle-import" in app_js
    assert "data-dry-run-retrieval-config-bundle" in app_js
    assert "data-import-retrieval-config-bundle" in app_js
    assert "data-clear-retrieval-config-bundle" in app_js
    assert "data-download-retrieval-config-bundle-result" in app_js
    assert "data-retrieval-source-intake" in app_js
    assert "data-retrieval-source-intake-sample-url" in app_js
    assert "data-analyze-retrieval-source-intake" in app_js
    assert "data-download-retrieval-source-intake" in app_js
    assert "data-apply-retrieval-source-intake" in app_js
    assert "data-apply-retrieval-source-intake-config" in app_js
    assert "data-apply-retrieval-source-intake-queries" in app_js
    assert "function retrievalSourceNameFromIntake" in app_js
    assert "function retrievalTargetSourceNameFromIntake" in app_js
    assert "target_source?.name" in app_js
    assert "function applyRetrievalSourceIntakeQueriesToBatch" in app_js
    assert "state.retrievalSources = new Set([sourceName])" in app_js
    assert "only this target source is selected by default" in app_js
    assert "sample_url" in app_js
    assert "validation_queries" in app_js
    assert "validation_plan" in app_js
    assert "Validation plan /" in app_js
    assert "minimum queries" in app_js
    assert "draft coverage" in app_js
    assert "intakeConfigContextTitle" in app_js
    assert "next action" in app_js
    assert "function safeRetrievalEndpoint" in app_js
    assert "function retrievalRemediationButtonHtml" in app_js
    assert "function retrievalRemediationPayload" in app_js
    assert "function runRetrievalBatchRemediation" in app_js
    assert "data-run-retrieval-remediation" in app_js
    assert "Unsupported remediation endpoint." in app_js
    assert "No remediation queries available" in app_js
    assert "validationPlan.gates" in app_js
    assert "data-retrieval-field-map-lab" in app_js
    assert "data-retrieval-field-map-lab-source" in app_js
    assert "data-retrieval-field-map-lab-ai" in app_js
    assert "data-retrieval-query-plan-ai" in app_js
    assert "retrievalQueryPlanUseAi" in app_js
    assert "AI PLAN" in app_js
    assert "currentRetrievalBatchQueriesText" in app_js
    assert "applyRetrievalOnboardingQueryParams" in app_js
    assert "required_queries" in app_js
    assert "query source" in app_js
    assert "query coverage" in app_js
    assert 'params.set("use_ai", "1")' in app_js
    assert ".retrieval-inline-toggle" in app_css
    assert "data-retrieval-model-status" in app_js
    assert "data-check-retrieval-model-status" in app_js
    assert "/retrieval/model-status${check ? \"?check=1\" : \"\"}" in app_js
    assert "use_ai" in app_js
    assert "data-suggest-retrieval-field-map-lab" in app_js
    assert "data-apply-retrieval-field-map-lab" in app_js
    assert "data-retrieval-readiness" in app_js
    assert "data-apply-retrieval-readiness-field-map" in app_js
    assert ".retrieval-readiness-head strong.low_sample" in app_css
    assert ".retrieval-readiness-sources .low_sample" in app_css
    assert ".retrieval-readiness-sources .blocked" in app_css
    assert "data-retrieval-local-paths-form" in app_js
    assert "data-clear-retrieval-local-paths" in app_js
    assert "data-refresh-retrieval-local-preview" in app_js
    assert "data-retrieval-http-json-form" in app_js
    assert "data-apply-retrieval-http-json-template" in app_js
    assert "data-clear-retrieval-http-json" in app_js
    assert "data-suggest-retrieval-http-json-field-map" in app_js
    assert "data-refresh-retrieval-http-json-preview" in app_js
    assert "elapsed_ms" in app_js
    assert "error_kind" in app_js
    assert "rate_limit_seconds" in app_js
    assert "rate_limit_wait_ms" in app_js
    assert "data-download-retrieval-report" in app_js
    assert "data-download-retrieval-summary" in app_js
    assert "data-report-format=\"csv\"" in app_js
    assert "data-report-format=\"json\"" in app_js
    assert "function downloadRetrievalReport" in app_js
    assert "rank_reasons" in app_js
    assert "confidence_label" in app_js
    assert "duplicate_hint" in app_js
    assert "existing_matches" in app_js
    assert "similarity_hint" in app_js
    assert "weak_similarity_matches" in app_js
    assert "candidate_ids" in app_js
    assert "/retrieval/search" in app_js
    assert "/retrieval/search/jobs" in app_js
    assert "function applyRetrievalSearchJob" in app_js
    assert "function loadLatestRetrievalSearchJob" in app_js
    assert "/retrieval/import" in app_js
    assert "/retrieval/runs" in app_js
    assert "function loadRetrievalRunCandidates" in app_js
    assert "data-load-retrieval-run-candidates" in app_js
    assert "恢复结果" in app_js
    assert "/retrieval/batches" in app_js
    assert "/retrieval/query-plan/jobs" in app_js
    assert "/retrieval/ai-scoring-jobs" in app_js
    assert "function applyRetrievalQueryPlanJob" in app_js
    assert "function applyRetrievalAiScoringJob" in app_js
    assert "function loadLatestRetrievalQueryPlanJob" in app_js
    assert "function loadLatestRetrievalAiScoringJob" in app_js
    assert "data-retrieval-runs" in app_js
    assert "data-retrieval-batches" in app_js
    assert "data-retrieval-batch-form" in app_js
    assert "data-refresh-retrieval-batches" in app_js
    assert "data-refresh-retrieval-runs" in app_js
    assert "/items/export-citations" in app_js
    assert "data-export-citation-form" in app_js
    assert "data-export-citation-format" in app_js
    assert "data-export-citation-error" in app_js
    assert "BibLaTeX" in app_js
    assert "CSL JSON" in app_js
    assert "selectedItemKeys()" in app_js
    assert "data-import-result-status" in app_js
    assert "条目已存在，已定位到已有条目" in app_js
    assert "function importEvidenceMessage" in app_js
    assert "summary.import_evidence" in app_js
    assert "provenance_recorded_count" in app_js
    assert "run_report_markdown_endpoint" in app_js
    assert "溯源记录" in app_js
    assert "currentRealCollectionKey" in app_js
    assert ".add-item-card" in app_css
    assert ".feature-table" in app_css
    assert ".feature-strip" in app_css
    assert "data-retrieval-page" in features_html
    assert "data-retrieval-page-panel" in features_html
    assert "多源检索" in features_html
    assert "feature-topbar" in features_html
    assert "推荐流程" not in features_html
    assert "什么时候打开高级区" not in features_html
    assert ".simple-retrieval-guide" in app_css
    assert ".simple-search-form" in app_css
    assert ".simple-section-title" in app_css
    assert ".simple-source-categories" in app_css
    assert ".simple-source-category" in app_css
    assert ".simple-source-option" in app_css
    assert ".simple-result-tools" in app_css
    assert ".retrieval-ai-row.evaluating" in app_css
    assert ".retrieval-advanced" in app_css
    assert ".retrieval-source-config-guide" in app_css
    assert ".retrieval-public-source-category" in app_css
    assert ".retrieval-config-source-links" in app_css
    assert ".retrieval-source-config-status" in app_css
    assert ".retrieval-advanced-section" in app_css
    assert ".retrieval-candidates" in app_css
    assert ".retrieval-candidate" in app_css
    assert ".retrieval-type-pill" in app_css
    assert ".retrieval-zotero-preview" in app_css
    assert ".retrieval-stats" in app_css
    assert ".retrieval-actions" in app_css
    assert ".retrieval-local-config" in app_css
    assert ".retrieval-http-json-config" in app_css
    assert ".retrieval-http-json-templates" in app_css
    assert ".retrieval-local-preview" in app_css
    assert ".retrieval-local-preview-mappings" in app_css
    assert ".retrieval-local-preview-quality" in app_css
    assert ".retrieval-local-preview-issues" in app_css
    assert ".retrieval-batch" in app_css
    assert ".retrieval-batch-actions" in app_css
    assert ".retrieval-batch-progress" in app_css
    assert ".retrieval-source-message" in app_css
    assert ".retrieval-readiness" in app_css
    assert ".retrieval-readiness-grid" in app_css
    assert ".retrieval-readiness-sources" in app_css
    assert ".retrieval-config-bundle" in app_css
    assert ".retrieval-config-bundle-result" in app_css
    assert ".retrieval-field-map-lab" in app_css
    assert ".retrieval-field-map-lab-result" in app_css
    assert ".retrieval-rank-row" in app_css
    assert ".retrieval-duplicate" in app_css
    assert ".retrieval-similarity" in app_css
    assert ".retrieval-report-actions" in app_css
    assert ".retrieval-report-btn" in app_css
    assert ".retrieval-summary" in app_css
    assert ".retrieval-summary-title" in app_css
    assert ".retrieval-summary-grid" in app_css
    assert ".retrieval-summary-sources" in app_css
    assert ".retrieval-history" in app_css
    assert ".export-citation-card" in app_css
    assert ".import-results" in app_css
    assert ".floating-card .form-action-btn" in app_css
    assert "function renderTitleCell" in app_js
    assert "title-primary" in app_js
    assert "title-secondary" in app_js
    assert "data-add-tag-form" not in app_js
    assert "renderGlobalShortcutPanel" not in app_js
    assert "data-resize-column" in app_js
    assert "data-rating-item" in app_js
    assert "querySelectorAll(\"[data-open-columns]\")" in app_js
    assert "data-reading-popover" in app_js
    assert "class=\"reading-option" in app_js
    assert "data-semantic-filter=\"type\"" in library_html
    assert "type-badge" in app_js
    assert "normalizeHashTag" in app_js
    assert "attachment_badges" in app_js
    assert "笔记" in app_js
    assert "data-note-toggle" in app_js
    assert "function notePreview" in app_js
    assert "没有笔记" in app_js
    assert "data-manage-shortcuts" not in library_html
    assert "data-add-tag-form" not in library_html
    assert "data-shortcut-form" not in library_html
    assert "data-selected-count" in library_html
    assert "data-bulk-actions" in library_html
    assert "本地副本可编辑" not in library_html
    assert "本地副本" in library_html
    assert "topbar-meta" in library_html
    assert "添加条目" in library_html
    assert "删除条目" in library_html
    assert "移动条目" in library_html
    assert "附件编辑" in library_html
    assert "文献下载" in library_html
    assert "期刊&会议等级查询" in library_html
    assert "引用导出" in library_html
    assert "导入知识库" in library_html
    assert "文献矩阵" not in library_html
    assert "知识库问答" not in library_html
    assert "title=\"文献研读\"" in library_html
    assert "button:hover ~ button" not in app_css
    assert ".form-action-btn" in app_css
    assert ".nav-action-btn" in app_css
    assert "class=\"nav-action-btn\"" in library_html
    assert "class=\"form-action-btn column-save-btn\"" in library_html
    assert "column-order-btn" in app_js
    assert ".column-order-btn" in app_css
    assert ".structured-cell-editor" in app_css
    assert ".structured-cell-actions .form-action-btn" in app_css
    assert ".structured-cell-editor input" in app_css
    assert ".structured-detail-form" in app_css
    assert ".shortcut-pill-toggle .tag-delete-btn" in app_css
    assert ".reading-option" in app_css
    assert ".type-badge" in app_css
    assert ".title-primary" in app_css
    assert ".title-secondary" in app_css
    assert ".title-text" in app_css
    assert ".type-group-academic" in app_css
    assert ".note-line" in app_css
    assert ".note-toggle-btn" in app_css
    assert ".item-table th > span:first-child" in app_css
    assert ".selection-col" in app_css
    assert ".selection-toggle-btn" in app_css
    assert ".row-checkbox" in app_css
    assert ".bulk-actions" in app_css
    assert ".bulk-action-btn" in app_css
    assert ".bulk-action-btn.is-disabled" in app_css
    assert ".bulk-action-btn[aria-disabled=\"true\"]" in app_css
    assert ".collection-menu" in app_css
    assert ".tree-action-btn" in app_css
    assert ".tree-row.manageable-root" in app_css
    assert ".bulk-modal-form" in app_css
    assert ".attachment-editor-card" in app_css
    assert ".attachment-add-grid" in app_css
    assert ".reader-picker-card" in app_css
    assert "input[type=\"file\"]::file-selector-button" in app_css
    assert ".bulk-modal-actions .form-action-btn" in app_css
    assert ".attachment-editor-actions .form-action-btn" in app_css
    assert ".table-stats" in app_css
    assert ".topbar-meta" in app_css
    assert "overflow-x: hidden" in app_css
    assert "grid-template-columns: minmax(72px, 110px) minmax(0, 1fr)" in app_css
    assert ".field-grid strong" in app_css
    assert ".detail-card p" in app_css
    assert ".attachment-line a" in app_css
    assert "overflow-wrap: anywhere" in app_css
    assert "data-toggle-plain-tags" in library_html
    assert "code_status" not in library_html
    assert "data-reader-page" in reader_html
    assert "data-reader-layout" in reader_html
    assert "data-reader-outline" in reader_html
    assert "data-reader-splitter=\"left\"" in reader_html
    assert "data-reader-splitter=\"right\"" in reader_html
    assert "data-reader-annotation-toolbar" in reader_html
    assert "data-clear-annotation" in reader_html
    assert "data-reader-zoom-out" in reader_html
    assert "data-reader-zoom-in" in reader_html
    assert "data-reader-zoom-label" in reader_html
    assert "data-reader-page-input" in reader_html
    assert "data-reader-page-total" in reader_html
    assert "reader-outline-collapse-btn" in reader_html
    assert "reader-return-action" in reader_html
    assert "reader-title-block" in reader_html
    assert "reader-subtitle-row" in reader_html
    assert "data-reader-toolbar" in reader_html
    assert "nav-action-btn" in reader_html
    assert "toolbar toolbar" not in reader_html
    assert "vendor/pdfjs/pdf.min.js" in reader_html
    assert "reader.js" in reader_html
    assert "pdfjsLib" in reader_js
    assert "getOutline" in reader_js
    assert "renderTextLayer" in reader_js
    assert "devicePixelRatio" in reader_js
    assert "outputScale" in reader_js
    assert "IntersectionObserver" in reader_js
    assert "fitWidthScale" in reader_js
    assert "jumpToPage" in reader_js
    assert "buildPageCharacterModel" in reader_js
    assert "pointToCharacterOffset" in reader_js
    assert "selectionRangeToRects" in reader_js
    assert "selectionRangeToText" in reader_js
    assert "getClientRects" not in reader_js
    assert "selectionRectsForPage" not in reader_js
    assert "pointerdown" in reader_js
    assert "pointermove" in reader_js
    assert "pointerup" in reader_js
    assert "normalizedAnnotationBox" in reader_js
    assert "data-create-annotation" in reader_js
    assert "clearAnnotationsInSelection" in reader_js
    assert "/annotations/clear" in reader_js
    assert "/annotations" in reader_js
    assert "function updateReaderToolbarPosition" in reader_js
    assert "getBoundingClientRect()" in reader_js
    assert "--reader-toolbar-left" in reader_js
    assert "--reader-toolbar-top" in reader_js
    assert "convertToPdfPoint" in reader_js
    assert "convertToViewportRectangle" in reader_js
    assert "position: readerState.pendingSelection.position" in reader_js
    assert "itemAnnotations" not in reader_js
    assert ".reader-layout" in reader_css
    assert ".reader-toolbar" in reader_css
    assert "--reader-toolbar-left" in reader_css
    assert "--reader-toolbar-top" in reader_css
    assert "position: fixed" in reader_css
    assert "grid-template-areas: \"title return\"" in reader_css
    assert ".reader-subtitle-row" in reader_css
    assert ".reader-page-input" in reader_css
    assert ".reader-outline-collapse-btn" in reader_css
    assert ".reader-annotation-toolbar" in reader_css
    assert ".reader-annotation.highlight" in reader_css
    assert ".reader-annotation.underline" in reader_css
    assert ".reader-clear-btn" in reader_css
    assert ".reader-selection-layer" in reader_css
    assert ".reader-selection" in reader_css
    assert "opacity: 0.2" not in reader_css
    assert "z-index: 1" in reader_css
    assert "z-index: 2" in reader_css
    assert "z-index: 3" in reader_css
    assert (root / "src" / "zotero_web_library" / "static" / "vendor" / "pdfjs" / "pdf.min.js").exists()
    assert (root / "src" / "zotero_web_library" / "static" / "vendor" / "pdfjs" / "pdf.worker.min.js").exists()


def test_frontend_contains_persistent_light_dark_theme_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    template_root = root / "src" / "zotero_web_library" / "templates"
    static_root = root / "src" / "zotero_web_library" / "static"
    page_templates = [
        "index.html",
        "library.html",
        "features.html",
        "knowledge.html",
        "reader.html",
        "writing.html",
        "api_config.html",
    ]

    theme_head = (template_root / "_theme_head.html").read_text(encoding="utf-8")
    theme_toggle = (template_root / "_theme_toggle.html").read_text(encoding="utf-8")
    theme_js = (static_root / "theme.js").read_text(encoding="utf-8")
    app_css = (static_root / "app.css").read_text(encoding="utf-8")
    reader_css = (static_root / "reader.css").read_text(encoding="utf-8")

    for template_name in page_templates:
        html = (template_root / template_name).read_text(encoding="utf-8")
        assert 'data-theme="light"' in html
        assert '{% include "_theme_head.html" %}' in html

    for template_name in ["index.html", "library.html", "features.html", "knowledge.html", "reader.html", "writing.html", "api_config.html"]:
        html = (template_root / template_name).read_text(encoding="utf-8")
        assert '{% include "_theme_toggle.html" %}' in html

    assert "guangming-theme" in theme_head
    assert "document.documentElement.dataset.theme" in theme_head
    assert "theme.js" in theme_head
    assert "data-theme-toggle" in theme_toggle
    assert "aria-pressed" in theme_toggle
    assert 'const THEME_STORAGE_KEY = "guangming-theme"' in theme_js
    assert "guangming:themechange" in theme_js
    assert "prefers-reduced-motion: reduce" in theme_js
    assert 'window.addEventListener("storage"' in theme_js
    assert ':root[data-theme="dark"]' in app_css
    assert 'html[data-theme="dark"]' in app_css
    assert ".theme-toggle-rail" in app_css
    assert "--color-reader-paper: #ffffff" in app_css
    assert "background: var(--color-reader-paper)" in reader_css


def test_retrieval_candidates_and_knowledge_chat_follow_theme_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    static_root = root / "src" / "zotero_web_library" / "static"
    template_root = root / "src" / "zotero_web_library" / "templates"
    app_css = (static_root / "app.css").read_text(encoding="utf-8")
    knowledge_js = (static_root / "knowledge.js").read_text(encoding="utf-8")
    knowledge_html = (template_root / "knowledge.html").read_text(encoding="utf-8")

    candidate_hover = app_css.split(".retrieval-candidate:hover", 1)[1].split("}", 1)[0]
    recommended_score = app_css.split(".retrieval-ai-row.recommend {", 1)[1].split("}", 1)[0]
    assert "background: var(--color-bg-hover)" in candidate_hover
    assert "border-color: var(--color-border-focus)" in candidate_hover
    assert "background: var(--color-success-soft)" in recommended_score
    assert "border-color: var(--color-success-light)" in recommended_score

    assert 'class="chat-message knowledge-chat-message' in knowledge_js
    assert 'class="chat-avatar" aria-hidden="true">${avatarLabel}' in knowledge_js
    assert 'class="chat-avatar" aria-hidden="true">Agent' in knowledge_js
    assert 'class="chat-bubble"' in knowledge_js
    assert "renderKnowledgeAgentTrace" in knowledge_js
    assert "renderKnowledgeChatSources" in knowledge_js
    assert 'class="knowledge-chat-composer"' in knowledge_html
    assert "保留运行过程、工具调用与引用证据" in knowledge_html


def test_translator_document_records_v1_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "zotero-translators.md").read_text(encoding="utf-8")
    assert "Search translator" in doc
    assert "Web + Search translator" in doc
    assert "Import translator" in doc
    assert "Export translator" in doc
    assert "不直接执行 Zotero translator JS" in doc
    assert "BibLaTeX" in doc
    assert "Zotero RDF" in doc
    assert "RIS" in doc
    assert "BibTeX" in doc


def test_retrieval_deployment_document_records_source_setup() -> None:
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "retrieval-deployment.md").read_text(encoding="utf-8")

    assert "OPENALEX_API_KEY" in doc
    assert "ADS_API_TOKEN" in doc
    assert "WEB_LIBRARY_RETRIEVAL_HTTP_JSON_CONFIG" in doc
    assert "WEB_LIBRARY_RETRIEVAL_SQLITE_CONFIG" in doc
    assert "WEB_LIBRARY_RETRIEVAL_MANIFEST_CONFIG" in doc
    assert "WEB_LIBRARY_RETRIEVAL_RATE_LIMIT_SECONDS" in doc
    assert "/retrieval/sources/report" in doc
    assert "/retrieval/readiness/report" in doc
    assert "/retrieval/tuning/report" in doc
    assert "/retrieval/onboarding/report" in doc
    assert "/retrieval/batches/<job_id>/report" in doc
    assert "batch_validation" in doc
    assert "import_readiness" in doc
    assert "/retrieval/config-bundle/download" in doc
    assert "/retrieval/model-status" in doc
    assert "/retrieval/field-map/suggest" in doc
    assert "/retrieval/field-map/report" in doc
    assert "source_setup" in doc
    assert "field_map_reports" in doc
    assert "/retrieval/source-intake" in doc
    assert "/retrieval/source-intake/report" in doc
    assert "Source intake" in doc
    assert "sample_url=true" in doc
    assert "target_source" in doc
    assert "validation_plan" in doc
    assert "validation_queries" in doc
    assert "Use queries" in doc
    assert "目标源仍不可用" in doc
    assert "普通检索和批量检索" in doc
    assert "/retrieval/rehearsal/setup" in doc
    assert "/retrieval/rehearsal/validate" in doc
    assert "AI_PIXEL_BASE_URL" in doc
    assert "AI_PIXEL_API_KEY" in doc
    assert "use_ai=true" in doc
    assert "READY" in doc
    assert "Object Manifest" in doc
    assert "RAG" in doc


def test_static_javascript_uses_browser_executable_mimetype() -> None:
    client = create_app().test_client()
    for path in ["/static/reader.js", "/static/vendor/pdfjs/pdf.min.js"]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("application/javascript")
        assert response.headers["Cache-Control"] == "no-store"


def test_features_index_page_points_to_library_selection() -> None:
    client = create_app().test_client()

    response = client.get("/features")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "多源检索" in html
    assert "选择文库" in html
    assert "href=\"/\"" in html


def test_library_features_page_mounts_retrieval_workspace(
    zotero_fixture: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    library = create_local_copy(zotero_fixture)
    client = create_app().test_client()

    response = client.get(f"/library/{library['library_id']}/features")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "多源检索" in html
    assert "data-retrieval-page" in html
    assert "data-retrieval-page-panel" in html
    assert "返回文库" in html


def test_library_api_config_page_mounts_config_workspace(
    zotero_fixture: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    library = create_local_copy(zotero_fixture)
    client = create_app().test_client()

    response = client.get(f"/library/{library['library_id']}/api-config")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "API 配置" in html
    assert "data-api-config-page" in html
    assert "data-api-config-panel" in html
    assert "多源检索" in html


def test_knowledge_page_route_renders_placeholder(
    zotero_fixture: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEB_LIBRARY_DATA_DIR", str(tmp_path / "app-data"))
    library = create_local_copy(zotero_fixture)
    client = create_app().test_client()

    response = client.get(f"/library/{library['library_id']}/knowledge")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "知识库" in html
    assert "data-knowledge-page" in html
    assert "data-knowledge-workbench" in html
    assert "新建知识库" in html


def test_ai_retrieval_review_guide_documents_review_flow() -> None:
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "ai-assisted-retrieval-review-guide.md").read_text(encoding="utf-8")

    assert "AI 辅助多源异构检索闭环" in guide
    assert "直接检索和计划检索的区别" in guide
    assert "快速模式" in guide
    assert "全量模式" in guide
    assert "Zotero 字段预览" in guide
    assert "screenshots/api-config.png" in guide
    assert "screenshots/multi-source-retrieval.png" in guide
    assert "screenshots/retrieval-candidates.png" in guide
    assert "建议 PR 描述" in guide
    assert "node --check src/zotero_web_library/static/app.js" in guide
