# Local Development and Git Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured Git workflows, managed local development services, and safe local opening/preview actions to the existing browser-based coding agent.

**Architecture:** Preserve the hand-written `CodingAgent` loop and introduce a composite `ToolProvider` boundary. Git, development services, and local opening live in focused service/provider modules; `WebRuntime` exposes their shared project state through the existing loopback-only JSON API, and the vanilla frontend renders Git, process, log, and preview state.

**Tech Stack:** Python 3.11+, standard library subprocess/threading/http.server/webbrowser, `openai` base client, Rich, vanilla HTML/CSS/JavaScript, pytest. No Agent framework, web framework, hosted code execution, or hosted file tool.

**Spec:** `docs/superpowers/specs/2026-08-30-local-development-git-design.md`

## Global Constraints

- Keep `OpenAI(...).chat.completions.create(...)` as the only model API integration.
- Do not add `openai-agents`, LangChain, LlamaIndex, AutoGen, CrewAI, Claude Agent SDK, FastAPI, Flask, Uvicorn, or any equivalent framework.
- File, Git, process, and opening actions execute locally and use the selected project directory.
- Preserve the three permission modes `request`, `risk`, and `full` and the existing application-layer hard denials.
- Never serialize API keys, Git credentials, sensitive environment values, or credential-bearing URLs into sessions, API responses, logs, tests, or documentation.
- Keep the web server bound to `127.0.0.1` with the existing Host, Origin, request-size, and static-path protections.
- Use code identifiers and comments in English; user-facing web/CLI text remains Chinese.
- Use TDD for every task and keep all existing tests passing.

---

### Task 1: Composite ToolProvider Boundary

**Files:**
- Create: `src/coding_agent/providers.py`
- Modify: `src/coding_agent/tools.py`
- Modify: `src/coding_agent/agent.py`
- Test: `tests/test_providers.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `ToolProvider.schemas() -> list[dict[str, Any]]`
- Produces: `ToolProvider.execute(name: str, arguments: dict[str, Any]) -> ToolResult`
- Produces: `CompositeToolProvider(providers: Sequence[ToolProvider])`
- Preserves: existing `ToolRegistry` constructor and behavior as one provider.

- [ ] **Step 1: Write failing provider composition tests**

```python
class StubProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def schemas(self) -> list[dict[str, object]]:
        return [{"type": "function", "function": {"name": self.name, "parameters": {"type": "object"}}}]

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(True, output=f"{name}:{arguments.get('value', '')}")


def test_composite_routes_tools_to_owning_provider() -> None:
    tools = CompositeToolProvider([StubProvider("first"), StubProvider("second")])
    assert [item["function"]["name"] for item in tools.schemas()] == ["first", "second"]
    assert tools.execute("second", {"value": "ok"}).output == "second:ok"


def test_composite_rejects_duplicate_and_unknown_tools() -> None:
    with pytest.raises(ValueError, match="重复工具"):
        CompositeToolProvider([StubProvider("same"), StubProvider("same")])
    assert CompositeToolProvider([]).execute("missing", {}).error == "未知工具: missing"
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_providers.py -q`

Expected: collection fails because `coding_agent.providers` does not exist.

- [ ] **Step 3: Implement the protocol and composite router**

```python
class ToolProvider(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...
    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class CompositeToolProvider:
    def __init__(self, providers: Sequence[ToolProvider]) -> None:
        self._schemas: list[dict[str, Any]] = []
        self._owners: dict[str, ToolProvider] = {}
        for provider in providers:
            for schema in provider.schemas():
                name = str(schema["function"]["name"])
                if name in self._owners:
                    raise ValueError(f"重复工具: {name}")
                self._owners[name] = provider
                self._schemas.append(schema)

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        owner = self._owners.get(name)
        return owner.execute(name, arguments) if owner else ToolResult(False, error=f"未知工具: {name}")
```

Change `CodingAgent` annotations from `ToolRegistry` to `ToolProvider`; do not alter loop behavior or message serialization.

- [ ] **Step 4: Run provider and agent tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_providers.py tests/test_agent.py tests/test_tools.py -q`

Expected: all pass and existing `ToolRegistry` callers remain compatible.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/coding_agent/providers.py src/coding_agent/tools.py src/coding_agent/agent.py tests/test_providers.py tests/test_agent.py
git commit -m "refactor: add composite tool provider boundary"
```

---

### Task 2: Structured Git Service and Permission Matrix

**Files:**
- Create: `src/coding_agent/git_service.py`
- Create: `src/coding_agent/git_tools.py`
- Modify: `src/coding_agent/permissions.py`
- Modify: `src/coding_agent/web_runtime.py`
- Test: `tests/test_git_service.py`
- Test: `tests/test_git_tools.py`

**Interfaces:**
- Consumes: `ToolProvider`, `ToolResult`, `ApprovalCallback`, `ConversationChangeTracker`.
- Produces: `GitService(workspace: Path, runner: GitRunner | None = None)`.
- Produces: `GitToolProvider(service, approval_mode, approver, change_tracker)`.
- Produces: `GitService.status/diff/log/branches/create_branch/stage/unstage/commit/pull/push`.

- [ ] **Step 1: Write failing GitService tests with temporary repositories**

```python
def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def test_status_stage_commit_and_diff_are_structured(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    service = GitService(tmp_path)
    assert service.status()["files"] == [{"path": "a.txt", "index": "?", "worktree": "?"}]
    service.stage(["a.txt"])
    commit = service.commit("feat: add a")
    assert len(commit["commit"]) >= 7
    assert service.status()["files"] == []


def test_stage_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    init_repo(tmp_path)
    with pytest.raises(GitOperationError, match="路径超出工作区"):
        GitService(tmp_path).stage(["../outside.txt"])
```

- [ ] **Step 2: Write failing permission-matrix provider tests**

```python
@pytest.mark.parametrize(
    ("mode", "tool", "asks"),
    [
        ("request", "git_status", False),
        ("request", "git_commit", True),
        ("risk", "git_commit", False),
        ("risk", "git_push", True),
        ("full", "git_push", False),
    ],
)
def test_git_permission_matrix(tmp_path: Path, mode: str, tool: str, asks: bool) -> None:
    approvals: list[str] = []
    provider = git_provider_for_test(tmp_path, mode, approvals)
    provider.execute(tool, valid_arguments(tool))
    assert bool(approvals) is asks
```

Also assert that no schemas exist for hard reset, clean, force push, or remote-branch deletion, and that `git_push` accepts no `force` parameter.

- [ ] **Step 3: Run Git tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_git_service.py tests/test_git_tools.py -q`

Expected: collection fails because Git modules do not exist.

- [ ] **Step 4: Implement GitService using argument arrays**

Use `subprocess.run(["git", "-C", str(workspace), ...], shell=False, timeout=...)`. Parse status with `git status --porcelain=v1 -z --branch`, keep all paths workspace-relative, and map stderr to stable codes:

```python
class GitOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output
```

Implement non-force `pull --ff-only` for the first version. `push` targets the configured upstream or `origin <current-branch>` and never accepts arbitrary refspecs.

- [ ] **Step 5: Implement GitToolProvider schemas, approval, and change capture**

Expose only the ten tools from the spec. Validate commit messages as non-empty strings up to 500 characters and paths as arrays of workspace-relative strings. Capture the workspace around `git_pull`; other Git metadata-only operations do not create file Diff entries.

- [ ] **Step 6: Compose Git tools in WebRuntime**

In `_make_agent`, create `GitToolProvider` only when the selected directory is a Git repository; otherwise still expose query tools so the model receives `not_repository` rather than an unknown-tool error. Compose it with the existing `ToolRegistry` using `CompositeToolProvider`.

- [ ] **Step 7: Run Git, Agent, and safety tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_git_service.py tests/test_git_tools.py tests/test_agent.py tests/test_safety.py tests/test_web_runtime.py -q`

Expected: all pass; tests use only temporary normal and bare local repositories.

- [ ] **Step 8: Commit Task 2**

```powershell
git add src/coding_agent/git_service.py src/coding_agent/git_tools.py src/coding_agent/permissions.py src/coding_agent/web_runtime.py tests/test_git_service.py tests/test_git_tools.py
git commit -m "feat: add structured git tools"
```

---

### Task 3: Project Service Configuration and Detection

**Files:**
- Create: `src/coding_agent/dev_services.py`
- Create: `src/coding_agent/project_runtime_store.py`
- Test: `tests/test_dev_services.py`
- Test: `tests/test_project_runtime_store.py`

**Interfaces:**
- Produces: `ServiceDefinition` dataclass.
- Produces: `detect_service_candidates(workspace: Path) -> list[ServiceDefinition]`.
- Produces: `ProjectRuntimeStore(root: Path).load/save(project_id, definitions)`.

- [ ] **Step 1: Write failing manifest-detection tests**

```python
def test_detects_package_json_dev_and_start_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite", "start": "node server.js", "lint": "eslint ."}}),
        encoding="utf-8",
    )
    found = detect_service_candidates(tmp_path)
    assert [(item.name, item.command) for item in found] == [
        ("前端开发服务", ("npm", "run", "dev")),
        ("项目服务", ("npm", "start")),
    ]


def test_detection_never_executes_manifest_content(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": f"echo bad > {marker}"}}), encoding="utf-8"
    )
    detect_service_candidates(tmp_path)
    assert not marker.exists()
```

Add fixtures for `pyproject.toml`, `Cargo.toml`, `go.mod`, and a static `index.html` project.

- [ ] **Step 2: Write failing local-store tests**

```python
def test_store_round_trips_without_secret_values(tmp_path: Path) -> None:
    definition = ServiceDefinition(
        id="frontend", name="前端", command=("npm", "run", "dev"), cwd=".",
        environment={"NODE_ENV": "development"}, secret_environment=("API_TOKEN",),
    )
    store = ProjectRuntimeStore(tmp_path)
    store.save("project-1", [definition])
    raw = (tmp_path / "projects" / "project-1" / "dev-services.json").read_text(encoding="utf-8")
    assert "API_TOKEN" in raw
    assert "secret-value" not in raw
    assert store.load("project-1") == [definition]
```

- [ ] **Step 3: Run tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dev_services.py tests/test_project_runtime_store.py -q`

Expected: collection fails because the modules do not exist.

- [ ] **Step 4: Implement immutable service definitions and conservative detection**

```python
@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    id: str
    name: str
    command: tuple[str, ...]
    cwd: str = "."
    environment: dict[str, str] = field(default_factory=dict)
    secret_environment: tuple[str, ...] = ()
    port: int | None = None
    health_url: str | None = None
```

Read manifests as data only. Generate stable IDs from the manifest type and script name. Do not import project Python modules or execute package scripts during detection.

- [ ] **Step 5: Implement atomic JSON persistence**

Write `version: 1` plus service records to a temporary sibling file and use `Path.replace()`. Reject invalid project IDs, absolute `cwd`, `..` traversal, empty commands, invalid ports, and non-loopback `health_url` values when loading.

- [ ] **Step 6: Run configuration and boundary tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dev_services.py tests/test_project_runtime_store.py tests/test_session_store.py tests/test_local_settings.py -q`

Expected: all pass.

- [ ] **Step 7: Commit Task 3**

```powershell
git add src/coding_agent/dev_services.py src/coding_agent/project_runtime_store.py tests/test_dev_services.py tests/test_project_runtime_store.py
git commit -m "feat: detect and persist development services"
```

---

### Task 4: Managed Development Process Lifecycle

**Files:**
- Create: `src/coding_agent/process_manager.py`
- Create: `src/coding_agent/dev_tools.py`
- Modify: `src/coding_agent/web_runtime.py`
- Modify: `src/coding_agent/web.py`
- Test: `tests/test_process_manager.py`
- Test: `tests/test_dev_tools.py`

**Interfaces:**
- Consumes: `ServiceDefinition`, `ToolProvider`, existing approval callback.
- Produces: `ProcessManager.start/stop/restart/list/logs/close`.
- Produces: `ServiceSnapshot` and cursor-based `LogBatch`.
- Produces: `DevToolProvider` tools `detect_services`, `list_services`, `start_service`, `stop_service`, `restart_service`, `read_service_logs`.

- [ ] **Step 1: Write failing lifecycle tests with Python child processes**

```python
def test_start_collect_logs_detect_url_and_stop(tmp_path: Path) -> None:
    definition = python_service(
        "import time; print('ready http://127.0.0.1:8765', flush=True); time.sleep(30)"
    )
    manager = ProcessManager()
    started = manager.start("project-1", tmp_path, definition, secret_values=[])
    batch = wait_for_log(manager, "project-1", definition.id, "ready")
    assert started.status in {"starting", "running"}
    assert batch.urls == ("http://127.0.0.1:8765",)
    manager.stop("project-1", definition.id)
    assert manager.list("project-1")[0].status == "stopped"
```

Cover duplicate start, restart, immediate failure, ring-buffer truncation, incremental cursors, secret redaction, stop timeout, child-tree termination, and `close()` cleanup.

- [ ] **Step 2: Write failing DevToolProvider tests**

Assert that detection/list/log reads are automatic; starting/stopping/restarting use `request` approval and are automatic in `risk` and `full`. Verify unknown service IDs and projectless conversations return structured errors.

- [ ] **Step 3: Run tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_process_manager.py tests/test_dev_tools.py -q`

Expected: collection fails because process modules do not exist.

- [ ] **Step 4: Implement ProcessManager without Shell invocation**

Use `subprocess.Popen(list(definition.command), cwd=..., env=..., shell=False)`, `CREATE_NEW_PROCESS_GROUP` on Windows, and `start_new_session=True` on POSIX. Drain stdout/stderr on daemon reader threads into a locked `deque` with monotonically increasing sequence numbers. Redact exact known secret values and credential-bearing URL userinfo before storage.

- [ ] **Step 5: Implement bounded stop and cleanup**

Stop the process group, wait up to five seconds, then terminate the tree with existing platform-specific logic. Never hold the manager lock while waiting. Register `ProcessManager.close()` in `web.main`'s `finally` block before server close.

- [ ] **Step 6: Implement DevToolProvider and runtime composition**

Load definitions from `ProjectRuntimeStore`, expose detected candidates, and require a saved definition before start. Compose the provider into each project-bound agent. A single `ProcessManager` instance belongs to `WebRuntime`, so all conversations in a project see shared service state.

- [ ] **Step 7: Add service JSON API routes**

Implement the service list, detect, start, stop, restart, and cursor-log endpoints from the spec. Reuse the current conversation permission mode for button-triggered writes and return HTTP 409 for lifecycle conflicts.

- [ ] **Step 8: Run process, web, and cancellation tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_process_manager.py tests/test_dev_tools.py tests/test_web_runtime.py tests/test_web_server.py tests/test_tools.py -q`

Expected: all pass and no child process remains after each test fixture.

- [ ] **Step 9: Commit Task 4**

```powershell
git add src/coding_agent/process_manager.py src/coding_agent/dev_tools.py src/coding_agent/web_runtime.py src/coding_agent/web.py tests/test_process_manager.py tests/test_dev_tools.py tests/test_web_runtime.py tests/test_web_server.py
git commit -m "feat: manage local development services"
```

---

### Task 5: Safe Local Opening and Full File Preview

**Files:**
- Create: `src/coding_agent/local_opener.py`
- Create: `src/coding_agent/open_tools.py`
- Modify: `src/coding_agent/tools.py`
- Modify: `src/coding_agent/agent.py`
- Modify: `src/coding_agent/web_runtime.py`
- Modify: `src/coding_agent/web.py`
- Test: `tests/test_local_opener.py`
- Test: `tests/test_open_tools.py`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Produces: `LocalOpener.open_url/reveal/open_associated` with injected OS functions.
- Extends: `ToolResult.data: dict[str, Any]` for UI actions and structured result data.
- Produces: `OpenToolProvider` tools `open_local_url`, `open_file`, `reveal_in_explorer`.
- Produces: `WebRuntime.file_preview(task_id, path, start_line, max_lines)`.

- [ ] **Step 1: Write failing opener policy tests**

```python
@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:5173", "http://[::1]:3000"])
def test_open_local_url_accepts_only_loopback(url: str) -> None:
    opened: list[str] = []
    LocalOpener(open_url=opened.append).open_local_url(url)
    assert opened == [url]


@pytest.mark.parametrize("url", ["https://example.com", "file:///C:/secret.txt", "javascript:alert(1)"])
def test_open_local_url_rejects_non_local_targets(url: str) -> None:
    with pytest.raises(OpenError, match="仅允许本机地址"):
        LocalOpener().open_local_url(url)
```

Add tests that executable/script extensions are refused by associated opening, while directories can be revealed and normal image/PDF files use the injected association function.

- [ ] **Step 2: Write failing ToolResult data and preview tests**

```python
def test_tool_result_serializes_structured_data() -> None:
    payload = json.loads(ToolResult(True, data={"ui_action": "preview_file", "path": "a.py"}).to_message())
    assert payload["data"] == {"ui_action": "preview_file", "path": "a.py"}


def test_open_file_requests_right_panel_preview(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('ok')\n", encoding="utf-8")
    result = OpenToolProvider(tmp_path, mode="risk").execute("open_file", {"path": "a.py"})
    assert result.data == {"ui_action": "preview_file", "path": "a.py"}
```

- [ ] **Step 3: Run tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_local_opener.py tests/test_open_tools.py -q`

Expected: collection or assertions fail because opener modules and `ToolResult.data` do not exist.

- [ ] **Step 4: Implement LocalOpener and OpenToolProvider**

Use `webbrowser.open` for loopback URLs. Use `explorer /select,PATH` on Windows, `open -R PATH` on macOS, and `xdg-open PARENT` on Linux. Use explicit suffix denial for `.exe`, `.msi`, `.bat`, `.cmd`, `.ps1`, `.sh`, `.com`, `.scr`, `.app`, and desktop launchers.

After validating the path, `open_file` returns a preview UI action for UTF-8 text/code files and invokes the injected system association function for allowed binary formats such as images and PDF. It refuses executable/script suffixes. Apply workspace boundaries in `request/risk`, and allow external files in `full` only through absolute paths.

- [ ] **Step 5: Propagate ToolResult data through agent events**

Add `data` to `tool_end` event payload without altering the OpenAI `tool` message format. In `WebRuntime._handle_agent_event`, recognize `preview_file` and set a per-task preview path; never execute arbitrary action names returned by a provider.

- [ ] **Step 6: Add preview/open API endpoints**

Add a bounded UTF-8 file-preview response with line numbers and truncation. Add `/api/open` for user button actions, reusing `LocalOpener` and current task permission/path rules.

- [ ] **Step 7: Run opener, agent, and web tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_local_opener.py tests/test_open_tools.py tests/test_agent.py tests/test_web_runtime.py tests/test_web_server.py -q`

Expected: all pass; injected opening functions prevent real windows during tests.

- [ ] **Step 8: Commit Task 5**

```powershell
git add src/coding_agent/local_opener.py src/coding_agent/open_tools.py src/coding_agent/tools.py src/coding_agent/agent.py src/coding_agent/web_runtime.py src/coding_agent/web.py tests/test_local_opener.py tests/test_open_tools.py tests/test_agent.py tests/test_web_runtime.py tests/test_web_server.py
git commit -m "feat: add safe local opening and file preview"
```

---

### Task 6: Project Git and Service State in WebRuntime

**Files:**
- Modify: `src/coding_agent/web_runtime.py`
- Modify: `src/coding_agent/web.py`
- Test: `tests/test_web_runtime.py`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: `GitService`, `ProcessManager`, `ProjectRuntimeStore`, `LocalOpener`.
- Produces: project payload fields `git` and `services_summary`.
- Produces: task payload field `preview_path`.
- Produces: all Git JSON endpoints listed in the spec.

- [ ] **Step 1: Write failing project-state tests**

```python
def test_project_payload_contains_git_and_service_summary(tmp_path: Path) -> None:
    init_repo_with_commit(tmp_path)
    runtime = runtime_for(tmp_path)
    project = runtime.add_project(str(tmp_path))
    payload = next(item for item in runtime.snapshot()["projects"] if item["id"] == project["id"])
    assert payload["git"]["branch"]
    assert payload["git"]["changed"] == 0
    assert payload["services_summary"] == {"running": 0, "failed": 0, "total": 0}
```

Add tests proving projectless conversations expose `git: null`, task previews are conversation-scoped, and projects sharing a directory observe the same repository/service state.

- [ ] **Step 2: Write failing Git route integration tests**

Exercise status, diff, stage, unstage, commit, pull, and push routes against temporary local repositories. Verify write routes require a valid current conversation, apply its permission mode, return 409 for approval/lifecycle conflicts, and never return credential-bearing remote URLs.

- [ ] **Step 3: Run tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_web_runtime.py tests/test_web_server.py -q`

Expected: assertions fail because payload fields and Git routes do not exist.

- [ ] **Step 4: Add cached bounded project summaries**

Compute Git status on snapshot with a short TTL cache keyed by repository root and invalidate it after Git writes. Read process summaries directly from `ProcessManager`. A failed Git query returns `{available: false, error: "..."}` rather than failing the full state request.

- [ ] **Step 5: Implement Git routes through the shared services**

Do not call subprocess from request handlers. Parse and validate JSON in `web.py`, then delegate to `WebRuntime`, which applies the same permission functions as Agent tools.

- [ ] **Step 6: Run runtime/server regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_web_runtime.py tests/test_web_server.py tests/test_git_service.py tests/test_git_tools.py tests/test_process_manager.py -q`

Expected: all pass.

- [ ] **Step 7: Commit Task 6**

```powershell
git add src/coding_agent/web_runtime.py src/coding_agent/web.py tests/test_web_runtime.py tests/test_web_server.py
git commit -m "feat: expose project development state"
```

---

### Task 7: Web Project Bar, Git Review, Service Cards, and Logs

**Files:**
- Modify: `src/coding_agent/web_assets/index.html`
- Modify: `src/coding_agent/web_assets/app.css`
- Modify: `src/coding_agent/web_assets/app.js`
- Test: `tests/test_web_assets.py`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: project `git`, `services_summary`, service snapshots/log batches, task `preview_path`.
- Produces: project operation bar, right-panel tabs, service cards, incremental log polling, file preview renderer.

- [ ] **Step 1: Write failing HTML structure and static behavior tests**

Require these accessible IDs and labels:

```python
assert {
    "project-operation-bar", "git-branch", "git-changes", "running-services",
    "open-local-page", "review-tabs", "file-preview", "git-review",
    "service-review", "service-list", "service-log",
}.issubset(parser.ids)
```

Assert `app.js` contains API calls for `/git/status`, `/services`, cursor-based `/logs?after=`, and `/api/open`, and contains no remote asset URL or secret-like value.

- [ ] **Step 2: Run asset tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_web_assets.py -q`

Expected: missing IDs and API behavior assertions fail.

- [ ] **Step 3: Add operation bar and right-panel tabs**

Keep the existing visual language. Add compact branch/change/service chips above the composer. Add accessible tab buttons for 文件、Git 改动、服务日志 and use one right panel rather than introducing another column.

- [ ] **Step 4: Add service rendering and actions**

Render service name, state, command, detected local URL, and buttons 打开、日志、重启、停止. Disable lifecycle buttons while a request is pending. Use the existing approval dialog for operations that require confirmation.

- [ ] **Step 5: Add incremental log and file preview rendering**

Maintain `serviceLogCursor` per selected service. Poll only while the service-log tab is visible; append escaped text nodes, cap rendered lines, and reset on project/service change. Render file previews with line numbers and truncation notices, never with `innerHTML` from file contents.

- [ ] **Step 6: Add responsive CSS and accessible state**

At widths below 980px keep the review panel as the existing overlay; below 720px allow the operation chips to wrap and keep start/stop buttons touch-sized. Ensure tab selection uses `aria-selected`, service status is text plus color, and every control has a label.

- [ ] **Step 7: Run frontend and server tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_web_assets.py tests/test_web_server.py -q`

Run: `node --check src/coding_agent/web_assets/app.js`

Expected: all tests pass and Node exits 0.

- [ ] **Step 8: Commit Task 7**

```powershell
git add src/coding_agent/web_assets/index.html src/coding_agent/web_assets/app.css src/coding_agent/web_assets/app.js tests/test_web_assets.py tests/test_web_server.py
git commit -m "feat: add project development controls"
```

---

### Task 8: Documentation, Cross-Platform Cleanup, and Final Acceptance

**Files:**
- Modify: `README.md`
- Modify: `tests/test_cli_and_boundaries.py`
- Modify: `.gitignore` only if a new local runtime path is not already covered.
- Test: all `tests/`.

**Interfaces:**
- Documents: local development workflow, Git approval matrix, service lifecycle, opening rules, Skill/MCP extension seam, and application-layer security caveat.

- [ ] **Step 1: Add failing dependency and credential-boundary assertions**

Extend `test_dependency_boundary` to preserve the exact runtime dependency allowlist and add a repository scan test that excludes `.git` and local ignored state but fails on `sk-`-style credentials or serialized authorization headers in tracked files.

- [ ] **Step 2: Update README with exact user workflows**

Document how to detect/start/stop services, open the detected page, inspect logs, use Git status/diff/stage/commit/pull/push, and understand each permission mode. State that services stop with the Agent, do not auto-resume, and that full access is not an OS sandbox.

- [ ] **Step 3: Run focused documentation/boundary tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli_and_boundaries.py -q`

Expected: all pass and no forbidden framework appears in `pyproject.toml`.

- [ ] **Step 4: Run the complete automated suite**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: zero failures; no test accesses the public internet.

- [ ] **Step 5: Run static and repository checks**

```powershell
node --check src/coding_agent/web_assets/app.js
git diff --check
git status --short
rg -l -i 'sk-[a-z0-9_-]{12,}' --glob '!*.pyc' --glob '!.git/**' .
```

Expected: JavaScript and diff checks exit 0; the credential scan prints no tracked project file; status contains only intended changes before the final commit.

- [ ] **Step 6: Perform local browser acceptance**

Start the Agent, add a disposable Git project, detect and start a test service, open its loopback URL, view incremental logs, edit a file, inspect Git Diff, stage and commit it, then stop the service. Confirm the right panel switches among file, Git, and logs without console errors. Use only a local bare repository if push/pull is demonstrated.

- [ ] **Step 7: Commit Task 8**

```powershell
git add README.md tests/test_cli_and_boundaries.py .gitignore
git commit -m "docs: explain local development and git workflows"
```

- [ ] **Step 8: Request final code review and finish the branch**

Use `superpowers:requesting-code-review`, address every Critical or Important issue, rerun the full verification commands, then use `superpowers:finishing-a-development-branch` to present merge/push choices.
