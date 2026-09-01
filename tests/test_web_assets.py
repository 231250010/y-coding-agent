from __future__ import annotations

import re
from html.parser import HTMLParser
from importlib.resources import files


class StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: set[str] = set()
        self.labels: list[dict[str, str | None]] = []
        self.scripts: list[dict[str, str | None]] = []
        self.styles = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.tags.append(tag)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "label":
            self.labels.append(values)
        if tag == "script":
            self.scripts.append(values)
        if tag == "style":
            self.styles += 1


def asset(name: str) -> str:
    return files("coding_agent").joinpath("web_assets", name).read_text(encoding="utf-8")


def test_page_has_navigation_conversation_diff_and_native_dialog_landmarks() -> None:
    parser = StructureParser()
    parser.feed(asset("index.html"))

    assert {"nav", "main", "aside", "dialog", "form"}.issubset(parser.tags)
    assert {
        "app",
        "project-list",
        "task-list",
        "transcript",
        "composer-form",
        "diff-panel",
        "approval-dialog",
        "settings-dialog",
        "delete-dialog",
        "browse-workspace",
        "permission-mode",
    }.issubset(parser.ids)
    assert parser.styles == 0
    assert parser.scripts == [{"src": "/assets/app.js", "defer": None}]


def test_every_form_control_has_an_accessible_label() -> None:
    parser = StructureParser()
    parser.feed(asset("index.html"))

    labelled_controls = {str(label["for"]) for label in parser.labels if label.get("for")}
    assert {"message-input", "workspace-path", "setting-model", "setting-base-url", "setting-api-key", "permission-mode"}.issubset(
        labelled_controls
    )


def test_assets_contain_no_remote_urls_or_secret_values() -> None:
    combined = "\n".join(asset(name) for name in ("index.html", "app.css", "app.js"))

    assert "https://" not in combined
    assert "http://" not in combined
    assert re.search(r"\bsk-[A-Za-z0-9_-]{12,}", combined) is None


def test_frontend_exposes_delete_for_projects_and_conversations() -> None:
    html = asset("index.html")
    js = asset("app.js")

    assert 'id="delete-dialog"' in html
    assert 'method: "DELETE"' in js


def test_header_menu_stops_click_bubbling_and_exposes_both_actions() -> None:
    js = asset("app.js")

    handler = js[js.index('document.querySelector("#rename-current")'):]
    assert "event.stopPropagation();" in handler[:300]
    assert 'element("button", "", "重命名")' in js
    assert 'element("button", "danger", "删除")' in js
    assert 'setAttribute("aria-expanded", "true")' in js
    assert "closeItemMenu()" in js


def test_assistant_markdown_uses_safe_dom_rendering_and_has_rich_styles() -> None:
    js = asset("app.js")
    css = asset("app.css")

    assert "function renderMarkdown" in js
    assert 'entry.kind === "assistant"' in js
    assert "appendInlineMarkdown" in js
    assert "innerHTML" not in js
    for selector in [".message-body.markdown", ".md-inline-code", ".md-code-block", ".md-table"]:
        assert selector in css


def test_frontend_streams_state_with_sse_and_renders_partial_assistant_text() -> None:
    javascript = asset("app.js")
    css = asset("app.css")

    assert 'new EventSource("/api/events")' in javascript
    assert 'stateEvents.addEventListener("state"' in javascript
    assert "task.streaming_content" in javascript
    assert "entry.streaming" in javascript
    assert "function renderTranscript(task, streaming)" in javascript
    assert "function updateStreamingMessage(text)" in javascript
    assert "ui.transcript.append(streamingMessage)" in javascript
    assert "lastTranscriptKey" not in javascript
    assert "setInterval(() => refresh(true), 5000)" in javascript
    assert ".message.streaming" in css
    assert "stream-cursor" in css


def test_frontend_renders_accessible_collapsible_decision_summaries() -> None:
    javascript = asset("app.js")
    css = asset("app.css")

    assert 'entry.kind === "decision_summary"' in javascript
    assert 'element("details", "decision-card")' in javascript
    assert 'element("summary", "decision-head")' in javascript
    assert 'card.addEventListener("toggle", syncDecisionState)' in javascript
    assert 'querySelectorAll(".decision-card[open]")' in javascript
    for selector in [
        ".message.decision_summary",
        ".decision-card",
        ".decision-head",
        ".decision-body",
    ]:
        assert selector in css


def test_ui_controls_share_one_explicit_font_family() -> None:
    css = asset("app.css")

    assert "button, input, textarea, select { font: inherit; font-family: var(--ui); }" in css
    assert "option { font-family: var(--ui); }" in css
    assert ".permission-mode" in css and "font-family: var(--ui)" in css


def test_change_cards_request_their_own_turn_diff() -> None:
    javascript = asset("app.js")

    assert "openDiff(path, entry.id)" in javascript
    assert "encodeURIComponent(entryId)" in javascript
    assert 'entry.change_scope === "conversation"' in javascript
    assert "对话累计改动（旧记录）" in javascript


def test_diff_panel_uses_adaptive_grid_without_covering_desktop_conversation() -> None:
    css = asset("app.css")

    assert ".shell { display: grid; grid-template-columns: 292px minmax(0, 1fr) 0;" in css
    assert ".shell.diff-open { grid-template-columns: 292px minmax(0, 1fr) clamp(340px, 38vw, 620px); }" in css
    assert ".shell.diff-open { grid-template-columns: 252px minmax(0, 1fr) clamp(300px, 40vw, 440px); }" in css
    assert "@media (max-width: 980px)" in css
    assert ".shell.diff-open { grid-template-columns: minmax(0, 1fr) clamp(320px, 40vw, 420px); }" in css
    assert "@media (max-width: 720px)" in css
    assert ".diff-panel { position: fixed; z-index: 8; inset: 0; width: 100%;" in css


def test_conversation_flex_layout_keeps_composer_at_bottom_when_optional_rows_hide() -> None:
    css = asset("app.css")

    assert ".conversation { position: relative; display: flex; flex-direction: column;" in css
    assert ".transcript { flex: 1 1 auto; min-height: 0;" in css
    assert ".composer { z-index: 2; flex: 0 0 auto; align-self: center;" in css
    assert "grid-template-rows: auto auto auto minmax(0, 1fr) auto" not in css


def test_devops_progress_rail_and_cancel_control_are_wired() -> None:
    html = asset("index.html")
    javascript = asset("app.js")
    css = asset("app.css")

    assert 'id="operation-progress"' in html
    assert 'role="progressbar"' in html
    assert 'id="cancel-operation"' in html
    assert "function renderProgress(task)" in javascript
    assert 'compose_release: "RELEASE"' in javascript
    assert 'compose_rollback: "ROLLBACK"' in javascript
    assert 'document.querySelector("#cancel-operation").addEventListener("click", stopTask)' in javascript
    assert ".operation-stages" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_release_console_exposes_environment_health_and_release_rail() -> None:
    html = asset("index.html")
    javascript = asset("app.js")
    css = asset("app.css")

    assert 'id="open-operations"' in html
    assert 'id="operations-view"' in html
    assert 'id="refresh-operations"' in html
    assert "function openOperations()" in javascript
    assert "/devops-overview" in javascript
    assert "function environmentNode(environment)" in javascript
    assert "生成回滚计划" in javascript
    for selector in [
        ".operations-manifest",
        ".environment-card",
        ".service-chip",
        ".release-rail",
        ".release-entry",
    ]:
        assert selector in css


def test_worktree_isolation_requires_explicit_dialog_confirmation() -> None:
    html = asset("index.html")
    javascript = asset("app.js")
    css = asset("app.css")

    assert 'id="create-worktree"' in html
    assert 'id="worktree-dialog"' in html
    assert 'id="worktree-form"' in html
    assert "function createTaskWorktree(event)" in javascript
    assert 'method: "POST", body: {}' in javascript
    assert "/worktree`" in javascript
    assert ".worktree-tool.active" in css
    assert ".worktree-preview" in css


def test_task_flight_plan_renders_structured_progress_and_blockers() -> None:
    html = asset("index.html")
    javascript = asset("app.js")
    css = asset("app.css")

    assert 'id="task-plan"' in html
    assert 'id="task-plan-items"' in html
    assert "function renderTaskPlan(task)" in javascript
    assert 'blocked: "阻塞"' in javascript
    assert ".task-plan-item.in_progress" in css
    assert ".task-plan-item.blocked" in css
