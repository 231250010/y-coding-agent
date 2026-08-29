# 小码本机网页版设计

## 目标

将当前 Tkinter 桌面界面替换为只监听 `127.0.0.1:8000` 的本机网页。浏览器负责项目、对话、审批、停止和 Diff 交互；Python 进程继续负责模型请求、Agent 循环、本地文件与命令工具、上下文管理、安全策略和会话持久化。

## 不可变边界

- 不在 Claude Code、Codex、OpenCode 等现成 Agent 产品上封装界面。
- 不安装或使用 `openai-agents`、OpenAI Agents SDK、LangChain、LlamaIndex、AutoGen、CrewAI、Claude Agent SDK 或其他 Agent 框架。
- 只允许 `from openai import OpenAI` 的基础 API 客户端调用 Chat Completions。
- 模型只通过原生 function/tool calling 请求项目自行定义的本地工具。
- 不使用 Code Interpreter、Files API、File Search、Computer Use 等服务端托管执行或文件工具。
- 对话历史、上下文压缩、工具定义与验证、本地执行、模型输出解析、Agent 循环、终止条件和错误恢复继续由本仓库代码实现。
- API Key 只从环境变量或 `.coding-agent/config.json` 读取；该目录保持 Git 忽略，凭据不进入源码、README、测试数据、日志或 Web 响应。
- Web 服务只绑定回环地址，不接受局域网或公网连接。
- 运行依赖仍只有 `openai` 和 `rich`；HTTP 服务使用 Python 标准库，前端使用原生 HTML、CSS 和 JavaScript。

## 架构

```text
Browser (HTML/CSS/JS)
  │ JSON API + short polling
  ▼
ThreadingHTTPServer @ 127.0.0.1:8000
  │
  ├─ WebRuntime：项目、对话、后台任务、审批状态、持久化
  ├─ CodingAgent：模型/tool_calls/循环/终止条件
  ├─ ToolRegistry：路径约束、本地读写、命令执行、安全审批
  ├─ ContextManager：预算估算、摘要与回退裁剪
  └─ SessionStore / ConversationChangeTracker
```

`web_runtime.py` 是不依赖 HTTP 或浏览器的应用服务层，所有状态更新在锁内完成。每段对话拥有自己的 `CodingAgent`、取消事件和文件改动追踪器。Agent 在后台线程运行，通过已有 `on_event` 回调更新工具状态；浏览器轮询状态快照，因此不需要新增异步框架。

`web.py` 只负责参数解析、静态资源、JSON 路由、状态码和服务生命周期，不承载 Agent 编排。`web_assets/` 只负责展示和用户输入。

## API 与数据流

- `GET /api/state`：返回项目、对话、当前选择、运行状态、待审批请求和非敏感设置。永不返回 API Key、模型消息历史或完整工具内部状态。
- `POST /api/projects`：接收本机绝对目录路径，验证目录存在后创建或定位项目。
- `POST /api/conversations`：创建项目内或未绑定目录的对话。
- `PATCH /api/projects/{id}`、`PATCH /api/conversations/{id}`：重命名。
- `DELETE /api/projects/{id}`、`DELETE /api/conversations/{id}`：删除 UI 会话记录；项目删除后所属对话变为未绑定，不删除磁盘目录。
- `POST /api/conversations/{id}/workspace`：在任务未运行时绑定已有本机目录。
- `POST /api/conversations/{id}/messages`：校验非空文本，记录用户消息并启动后台 Agent；同一对话不允许并发运行。
- `POST /api/conversations/{id}/cancel`：设置取消事件并终止正在运行的命令。
- `POST /api/approvals/{id}`：提交一次性允许或拒绝决定。
- `GET /api/conversations/{id}/changes/{path}`：只从该对话的追踪器返回结构化 Diff 行。

工具状态会显示为短暂进度记录。文件修改路径先去重收集，只在本轮最终 assistant、取消或错误消息上附加一次“本轮改动”，保持用户要求的单一入口。

命令审批由 Agent 工作线程创建待审批对象并等待浏览器决定。服务关闭、对话取消或审批超时都会返回拒绝，不会让线程永久阻塞。

## 前端设计

主题是“本地代码工作台”，服务对象是独立开发者，页面唯一工作是让用户在一个本机项目里持续交给小码编程任务。

### 视觉令牌

- Blueberry rail `#244A67`：项目导航和品牌身份。
- Milk canvas `#F7F5F0`：主工作区背景。
- Paper surface `#FFFFFF`：消息、输入和 Diff 表面。
- Ink `#1E2D38`：正文。
- Peach cursor `#F2A97E`：唯一高辨识度动作色。
- Mint/rose diff `#DDF4E5` / `#FCE1E1`：新增与删除行。

显示与正文使用系统中文字体栈 `"Microsoft YaHei UI", "PingFang SC", sans-serif`，路径、行号和工具名使用 `"Cascadia Code", "SFMono-Regular", monospace`。不下载远程字体，确保离线与隐私边界。

### 布局

```text
┌──────────────┬────────────────────────────┬──────────────────┐
│ 小码 / 项目   │ 对话标题、目录和运行状态      │ 文件路径  +N -N   │
│              ├────────────────────────────┤                  │
│ 项目          │ 对话消息与工具进度            │ 彩色行级 Diff      │
│  └ 对话       │                            │                  │
│              ├────────────────────────────┤                  │
│ 新建对话      │ 输入框 / 目录 / 发送 / 停止    │                  │
└──────────────┴────────────────────────────┴──────────────────┘
```

右侧 Diff 不是永久空白栏：只有点击“本轮改动”后才滑入，占据第三栏；关闭后对话恢复宽度。这个“对话结束后收束成一条改动轨迹”是页面的唯一视觉签名。窄屏下侧栏成为抽屉，Diff 覆盖在内容之上；键盘焦点清晰，并尊重 `prefers-reduced-motion`。

设置区不展示已保存 API Key，只显示“已配置/未配置”。网页允许编辑模型、Base URL、上下文预算、最大步骤和审批模式；API Key 输入留空表示保持原值，新值写入 Git 忽略配置仅在用户明确选择记住时发生。

## 安全与错误处理

- 服务器固定使用 `127.0.0.1`；`Host` 只接受回环主机，写请求校验 `Origin` 为同源或缺省的本机客户端。
- 静态资源路径使用固定映射，不接受任意文件路径。
- JSON 请求设置大小上限，拒绝非法 JSON、错误类型、未知路由和过长消息。
- 工作目录必须是已存在目录，并在后端解析；浏览器文本值不直接用于文件响应。
- Diff 路径必须存在于对应对话的 `ConversationChangeTracker`，不能借接口读取任意文件。
- 所有异常转换为不含凭据、堆栈和内部绝对路径的 JSON 错误；服务端只记录必要状态。
- 浏览器刷新不终止后台任务，重新轮询后恢复当前状态。

## 启动与兼容

默认入口改为：

```powershell
python -m coding_agent
```

它启动 `http://127.0.0.1:8000/` 并默认打开系统浏览器。提供 `--no-browser`、`--port` 和 `--workspace`。桌面模块可以作为兼容代码保留，但不再注册为产品入口；`coding-agent` 指向 Web 入口，`coding-agent-cli` 继续作为调试备用入口。

缺少 API Key 时服务仍可启动，网页显示配置引导；发送任务前返回明确配置错误。

## 测试与验收

- 应用服务测试项目/对话 CRUD、目录绑定、会话恢复、消息并发保护、取消、审批和单次改动汇总。
- HTTP 集成测试静态页面、状态接口、输入校验、回环 Host/Origin、防路径读取和 API Key 不泄露。
- 脚本化假模型验证浏览器发起任务后仍由现有 `CodingAgent` 完成 tool calling 循环。
- 前端资源检查语义结构、键盘标签、三栏/Diff 标记和敏感字段缺失；人工在真实浏览器检查响应式布局和交互。
- 完整 `pytest` 必须通过，依赖清单不得出现任何 Agent 框架或 Web 框架。

