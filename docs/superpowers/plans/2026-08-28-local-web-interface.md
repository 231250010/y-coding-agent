# 小码本机网页版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将默认 Tkinter 桌面产品迁移为只监听 `127.0.0.1:8000` 的本机网页版，同时完整复用自行实现的 Agent 核心。

**Architecture:** 新增与 HTTP 解耦的 `WebRuntime` 管理项目、对话、后台 Agent、审批和持久化；标准库 `ThreadingHTTPServer` 提供 JSON API 与静态资源；原生 HTML/CSS/JavaScript 构建三栏 Codex 风格界面。现有 `CodingAgent`、`ToolRegistry`、`ContextManager`、`SessionStore` 和 `ConversationChangeTracker` 保持 Agent 编排职责。

**Tech Stack:** Python 3.11 标准库 HTTP/线程、原生 HTML/CSS/JavaScript、现有 `openai` 基础客户端、pytest。

**Spec:** `docs/superpowers/specs/2026-08-28-local-web-interface-design.md`

## Global Constraints

- 不使用任何 Agent 框架/SDK、现成 Agent 产品或服务端托管执行/文件工具。
- `openai` 只调用 `OpenAI(...).chat.completions.create(...)`。
- Web 服务只绑定 `127.0.0.1`，API Key 不进入仓库、README、日志或 Web 响应。
- 运行依赖仍只有 `openai` 和 `rich`。
- 新行为遵循测试先行；每轮文件改动只在最终消息显示一次。

---

### Task 1: Web 应用服务状态机

**Files:**
- Create: `src/coding_agent/web_runtime.py`
- Create: `tests/test_web_runtime.py`
- Reuse: `src/coding_agent/agent.py`, `src/coding_agent/session_store.py`, `src/coding_agent/changes.py`

**Interfaces:**
- Produces: `WebRuntime(config, settings, settings_root, model_factory=None)`；`snapshot()`；项目/对话 CRUD；`send_message()`；`cancel()`；`resolve_approval()`；`diff()`。
- Consumes: `CodingAgent.run`, `ToolRegistry`, `SessionStore` version 3 payload, `ConversationChangeTracker` serialization。

- [ ] 写项目/对话创建、目录绑定和快照的失败测试。
- [ ] 运行 `pytest tests/test_web_runtime.py -q`，确认因模块缺失而失败。
- [ ] 实现最小数据类、锁、加载/保存和安全快照，使测试通过。
- [ ] 写消息后台执行、单对话并发拒绝、取消和最终改动单次汇总的失败测试。
- [ ] 实现 Agent 工厂、事件处理与后台线程，使测试通过。
- [ ] 写审批等待/决定/取消和 Diff 序列化的失败测试。
- [ ] 实现有界审批状态机与结构化 Diff 行，使测试通过。

### Task 2: 标准库 HTTP 接口与本机安全边界

**Files:**
- Create: `src/coding_agent/web.py`
- Create: `tests/test_web_server.py`

**Interfaces:**
- Produces: `create_server(runtime, host="127.0.0.1", port=8000)`；`main(argv=None)`；JSON routes defined by the spec。
- Consumes: `WebRuntime` public methods and packaged `web_assets` files。

- [ ] 写真实临时 HTTP 服务测试，覆盖 `/api/state`、项目、对话、消息、取消、审批和 Diff。
- [ ] 运行目标测试并确认因路由不存在而失败。
- [ ] 实现固定路由、JSON 请求上限、统一 JSON 错误与正确状态码。
- [ ] 写 Host、Origin、静态路径穿越、未知资源和 API Key 不泄露测试并确认失败。
- [ ] 实现回环访问策略、固定静态资源映射和敏感字段过滤，使测试通过。

### Task 3: 浏览器三栏工作台

**Files:**
- Create: `src/coding_agent/web_assets/index.html`
- Create: `src/coding_agent/web_assets/app.css`
- Create: `src/coding_agent/web_assets/app.js`
- Create: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `/api/state` 轮询快照和 Task 2 的写接口。
- Produces: 可访问的项目/对话侧栏、消息流、输入区、审批层、响应式 Diff 审查面板。

- [ ] 写静态资源服务和关键可访问交互的失败测试。
- [ ] 实现语义 HTML、蓝莓/奶白 token、三栏布局和空状态。
- [ ] 实现原生 JS 状态渲染、CRUD、发送/停止、目录绑定和错误提示。
- [ ] 实现一次性“本轮改动”卡片、右侧彩色 Diff、审批对话框和键盘交互。
- [ ] 运行 Web 资源与 HTTP 测试并修复到绿色。

### Task 4: 默认入口、配置与文档迁移

**Files:**
- Modify: `src/coding_agent/__main__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `tests/test_cli_and_boundaries.py`
- Create or Modify: `tests/test_web_entrypoint.py`

**Interfaces:**
- Produces: `python -m coding_agent` 和 `coding-agent` 启动 Web；参数 `--workspace`, `--port`, `--no-browser`。
- Preserves: `coding-agent-cli`；模型与密钥环境变量；桌面模块不再作为默认入口。

- [ ] 写入口解析、脚本指向、依赖边界和缺少密钥仍可启动页面的失败测试。
- [ ] 改默认入口和项目脚本，保持依赖列表不新增框架。
- [ ] 更新 README 的本机网页运行、配置、安全和演示说明，不写入任何凭据。
- [ ] 运行入口与边界测试并修复到绿色。

### Task 5: 回归、浏览器验证与交付

**Files:**
- Modify as needed: implementation and tests above only。

**Interfaces:**
- Produces: 可启动、可交互、可恢复会话的最终本机网页版。

- [ ] 运行 `python -m pytest`，确认全部测试通过。
- [ ] 运行依赖名称检查，确认不存在禁止的 Agent/Web 框架。
- [ ] 使用 `python -m coding_agent --no-browser` 启动真实服务并请求健康页面。
- [ ] 在浏览器检查 1240px、窄屏布局、项目/对话切换、发送、审批、停止和 Diff。
- [ ] 搜索仓库敏感凭据模式，确认没有 API Key 进入受版本控制文件。
- [ ] 检查 `git diff`，只保留本次迁移相关改动。

