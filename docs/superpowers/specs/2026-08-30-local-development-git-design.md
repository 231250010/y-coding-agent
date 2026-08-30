# 本地开发与 Git 一体化设计

## 背景与目标

Coding Agent 已具备本地文件工具、通用命令执行、会话级权限模式、工作目录管理和网页交互。本阶段在不引入任何 Agent 框架的前提下，增加结构化的本地开发与 Git 能力，让 Agent 可以可靠地启动项目、查看日志、打开本地页面或文件，并完成常见 Git 工作流。

本阶段的目标是：

- 自动识别并保存项目的开发服务配置；
- 启动、停止、重启和观察本地开发服务；
- 在浏览器、Agent 右侧预览区或系统资源管理器中打开本地内容；
- 通过结构化工具完成 Git 查询、暂存、提交、拉取和推送；
- 让所有操作继续服从现有三档会话权限和应用层安全策略；
- 为后续 Skill、MCP 和用户扩展预留稳定但最小的工具提供者接口。

本阶段不包含云部署、托管平台集成、远程服务器运维、Git 自动提交策略、插件市场、Skill 加载器或 MCP 客户端。

## 设计原则与边界

- 使用官方基础客户端库 `openai` 的 Chat Completions tool calling，不使用 `openai-agents` 或其他 Agent 框架。
- 对话历史、上下文管理、工具调度、审批、循环终止和错误恢复继续由项目代码实现。
- 常用开发和 Git 操作使用结构化工具，不让模型拼接任意 Shell 字符串。
- `run_command` 仍作为补充工具，但不能替代结构化工具的权限和状态管理。
- 所有服务、Git 和打开操作都在用户本机执行，不使用服务端托管代码执行或文件工具。
- 安全策略是应用层纵深防护，不宣称提供操作系统级隔离。
- 凭据只能来自环境变量、Git 凭据管理器或未入库配置，状态接口和日志必须脱敏。

## 总体架构

现有 `CodingAgent -> ToolRegistry` 主循环保持不变，新增四个边界清晰的能力组件：

```text
模型 tool_calls
      |
      v
ToolRegistry
  参数校验 / 权限审批 / 输出截断 / 结构化错误
      |
      +-- GitService
      |     status / diff / log / branch / stage / commit / pull / push
      |
      +-- ProcessManager
      |     detect / start / stop / restart / status / logs
      |
      +-- LocalOpener
      |     本地 URL / 文件预览 / 资源管理器 / 系统关联应用
      |
      +-- 现有文件工具与 run_command
```

### ToolProvider 扩展接口

工具注册从单一列表演进为项目自建的提供者接口：

```python
class ToolProvider(Protocol):
    def schemas(self) -> list[dict[str, object]]: ...
    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult: ...
```

内置文件、Git、开发服务和打开能力均可作为 `ToolProvider` 注册，但首版不实现动态插件系统。未来扩展时：

- Skill 只提供提示词、说明和资源，不接管 Agent 循环；
- MCP 适配器负责将远端工具转换为现有 JSON Schema 与 `ToolResult`；
- 所有扩展工具仍经过本项目的参数验证、权限策略、结果截断和错误处理；
- 不接入违反课程要求的服务端托管代码执行或文件工具。

## GitService

### 结构化工具

- `git_status`：返回分支、上游、领先/落后和文件状态。
- `git_diff`：按工作区、暂存区或指定文件返回截断后的 Diff。
- `git_log`：返回限制数量的提交记录。
- `git_branches`：列出本地分支、当前分支和上游。
- `git_create_branch`：创建并切换到新分支。
- `git_stage`：暂存明确列出的工作区相对路径。
- `git_unstage`：取消暂存明确列出的路径，不丢弃工作区内容。
- `git_commit`：提交当前暂存内容，提交消息必填。
- `git_pull`：执行非强制拉取，冲突时停止并报告。
- `git_push`：推送当前分支，不支持 `--force` 或删除远端引用。

Git 命令使用参数数组调用，不经过 Shell。仓库根目录由 `git rev-parse --show-toplevel` 解析，操作前确认当前项目位于该仓库内。路径必须经过已有规范化和符号链接边界检查。

### Git 权限矩阵

| 操作 | 请求批准 | 帮我批准 | 完全访问权限 |
| --- | --- | --- | --- |
| status / diff / log / show / branches | 自动 | 自动 | 自动 |
| stage / unstage / create branch / commit | 询问 | 自动 | 自动 |
| pull / merge / rebase / push | 询问 | 询问 | 自动 |
| reset --hard / clean -fd / force push / 删除远端分支 | 拒绝 | 拒绝 | 拒绝 |

首版不提供结构化 `merge` 和 `rebase` 工具；若通过通用命令请求，继续由现有策略审批。危险操作既不暴露为结构化工具，也继续由通用命令策略拒绝。

### Git 结果与错误

结果包含操作类型、仓库根目录、分支、提交哈希、文件数量和经过脱敏的输出。以下情况使用稳定错误类型：

- `not_repository`：目录不在 Git 仓库中；
- `nothing_to_commit`：没有可提交的暂存内容；
- `merge_conflict`：拉取产生冲突；
- `authentication_failed`：凭据或权限失败；
- `remote_rejected`：远端拒绝；
- `unsafe_operation`：请求了禁止操作；
- `git_failed`：其他非零退出。

## ProcessManager

### 服务识别

`detect_services` 只读取项目清单并返回候选，不直接执行命令。首版识别：

- `package.json` 中的 `dev`、`start` 和常见前后端脚本；
- `pyproject.toml` 中可识别的 ASGI、Flask、Django 或项目脚本；
- `Cargo.toml` 对应 `cargo run`；
- `go.mod` 对应 `go run .`；
- 静态站点的安全本地 HTTP 服务候选。

第一次启动候选服务时形成命名配置。用户或 Agent 可调整命令、工作目录、环境变量名称、端口提示和健康检查 URL。敏感环境变量只记录名称，不记录值。

### 服务配置

运行配置属于项目，保存在 Agent 自己的 Git 忽略目录：

```text
.coding-agent/projects/<project-id>/dev-services.json
```

每项包含：

- 稳定服务 ID 和显示名称；
- 参数数组形式的启动命令；
- 项目内相对工作目录；
- 非敏感环境变量；
- 需要从进程环境继承的敏感变量名称；
- 可选端口、健康检查 URL 和 URL 检测规则。

配置持久化，PID 和运行状态不持久化。

### 进程生命周期

- 同一项目可运行多个命名服务，例如前端和后端；
- 服务由一个进程内 `ProcessManager` 管理，状态在项目的所有对话间共享；
- 启动记录 PID、命令、启动时间、状态、最近日志和检测到的本地 URL；
- 重复启动同一服务不会创建第二个进程；
- 停止和重启会终止完整子进程树；
- Coding Agent 正常关闭时停止所有受管服务；
- Coding Agent 重启后保留配置，但不自动恢复服务；
- 异常退出后下一次启动不信任旧 PID，也不尝试接管未知进程。

### 日志与 URL 检测

stdout 和 stderr 分别由后台读取线程持续消费，写入每个服务的固定大小环形缓冲区。网页使用递增游标读取新增日志，避免重复传输全部内容。

日志进入状态接口前执行：

- UTF-8 容错解码；
- 单行和总量限制；
- API Key、访问令牌、带凭据 URL 和已知敏感环境变量值脱敏；
- `localhost`、`127.0.0.1`、`[::1]` URL 提取。

服务状态包括 `stopped`、`starting`、`running`、`failed` 和 `stopping`。进程启动后立即退出、健康检查超时或端口占用时进入 `failed` 并保留诊断日志。

## LocalOpener

### 打开规则

- 代码和文本文件：在 Agent 右侧只读文件标签页打开，可从完整文件切换到 Diff；
- 目录或“定位文件”：调用系统资源管理器并选中目标；
- 本地 URL：调用默认浏览器，只自动允许 `localhost`、`127.0.0.1` 和 `[::1]`；
- 图片、PDF 等普通文件：调用系统关联应用；
- `.exe`、脚本、安装包和无法安全分类的文件：拒绝直接打开，要求通过受控命令工具处理。

所有路径先规范化。`request` 和 `risk` 模式保持工作区边界；`full` 模式仅允许通过绝对路径访问工作区外内容。打开动作不读取或上传文件内容。

非本地 HTTP(S) URL 被视为互联网操作，不能通过 `open_local_url` 绕过现有权限策略。

## Agent 工具数据流

```text
模型产生 tool_call
  -> ToolRegistry 校验工具名和 JSON 参数
  -> 解析项目、仓库和工作目录
  -> 权限策略给出 AUTO / REVIEW / DENY
  -> REVIEW 在网页创建待审批项
  -> 对应服务执行本地操作
  -> 输出脱敏、截断并转换为 ToolResult
  -> 追加 tool 消息并再次调用模型
  -> WebRuntime 同步项目运行与 Git 摘要
```

审批拒绝、参数错误、Git 失败、服务超时和打开失败都作为结构化工具结果反馈模型，不终止整个网页服务。模型可以调整方案，但同一被拒绝操作不能通过换用通用命令规避权限。

## 网页界面

现有左侧项目、中央对话、右侧审查布局保持不变。

### 左侧

项目项增加运行状态圆点：未运行、启动中、运行中和异常。状态汇总该项目全部受管服务。

### 中间

输入框上方增加项目操作条，展示：

- 当前 Git 分支；
- 工作区和暂存区改动数；
- 运行中服务数量；
- 已检测本地 URL 的“打开”快捷操作。

Git 和服务操作仍作为工具事件显示在对话中，任务结束后只出现一次结构化结果摘要。

### 右侧

审查区域增加标签切换：

- 文件 / Diff；
- Git 改动；
- 服务日志。

服务卡片显示服务名、状态、启动命令、本地 URL，并提供打开、查看日志、重启和停止按钮。短操作继续使用 JSON API；日志使用增量轮询，首版不引入 WebSocket。

## 本机 HTTP API

新增端点遵循现有回环 Host、Origin 和 JSON 大小限制：

- `GET /api/projects/{id}/git/status`
- `GET /api/projects/{id}/git/diff`
- `POST /api/projects/{id}/git/stage`
- `POST /api/projects/{id}/git/unstage`
- `POST /api/projects/{id}/git/commit`
- `POST /api/projects/{id}/git/pull`
- `POST /api/projects/{id}/git/push`
- `GET /api/projects/{id}/services`
- `POST /api/projects/{id}/services/detect`
- `POST /api/projects/{id}/services/{service_id}/start`
- `POST /api/projects/{id}/services/{service_id}/stop`
- `POST /api/projects/{id}/services/{service_id}/restart`
- `GET /api/projects/{id}/services/{service_id}/logs?after=<cursor>`
- `POST /api/open`

网页按钮发起的写操作使用当前对话的权限模式；没有当前对话时拒绝需要审批的动作。Agent 工具和网页按钮共用同一服务层与权限判断，避免出现两套规则。

## 并发与一致性

- `ProcessManager` 使用独立锁保护服务表和日志缓冲区，不在持锁状态等待子进程退出；
- 同一服务的启动、停止和重启操作串行化；
- Git 写操作按仓库根目录加锁，Git 查询可以并行；
- 会话锁不包围长时间 Git、健康检查或进程等待；
- 项目删除只删除 Agent 中的项目记录和运行配置，不删除仓库文件；删除前停止该项目的受管服务；
- 项目重新绑定目录后重新计算 Git 和服务状态，不复用旧目录 PID。

## 错误处理

- Git：区分非仓库、冲突、无改动、认证失败、远端拒绝和通用失败；
- 服务：区分命令不存在、端口占用、立即退出、健康检查超时、日志读取失败和停止超时；
- 打开：区分目标不存在、URL 不允许、文件类型拒绝、无系统关联和系统调用失败；
- 审批：拒绝作为可恢复工具错误，取消任务会同时解除审批等待；
- API：返回中文安全错误，不返回堆栈、环境变量、凭据或完整敏感命令输出；
- 清理：进程关闭失败时执行有界升级终止并记录脱敏警告，不能无限阻塞 Agent 退出。

## 测试策略

自动化测试不访问真实互联网，不使用真实托管平台：

### Git

- 临时仓库覆盖状态、Diff、日志、分支、暂存、取消暂存和提交；
- 本地 bare 仓库模拟 pull/push；
- 覆盖无仓库、无改动、冲突和远端拒绝；
- 表驱动测试三档权限矩阵；
- 验证 hard reset、clean、force push 和删除远端分支始终拒绝；
- 验证路径参数不能逃逸项目或注入额外 Git 参数。

### 开发服务

- 使用短小 Python 子进程模拟正常服务、日志输出、URL 输出、异常退出和不响应停止；
- 覆盖自动识别、配置保存、重复启动、重启、环形日志、增量游标和子进程树清理；
- 验证多个对话共享同一项目服务状态，但不共享对话历史；
- 验证 Agent 关闭后受管进程全部终止，重启后不自动恢复。

### 本地打开与网页

- 注入系统打开函数，验证本地 URL 白名单、文件类型、路径边界和错误转换；
- 验证代码文件进入右侧预览，Git Diff 和服务日志标签可切换；
- 验证按钮操作与 Agent 工具使用同一权限矩阵；
- 验证状态接口、日志和工具结果不泄露 API Key、Git 凭据或敏感环境变量。

### 边界验收

- 全量现有测试继续通过；
- 依赖树中存在基础 `openai` 客户端，不存在任何 Agent 框架；
- Git 和服务测试不访问真实网络；
- Windows 与常见 POSIX 环境均能启动、停止和清理开发服务；
- 应用层安全限制在 README 中明确，不将完全访问模式描述为操作系统沙箱。

## 实施拆分

后续实施计划应按以下依赖顺序拆分：

1. `ToolProvider` 注册边界与兼容现有工具；
2. `GitService`、权限矩阵和结构化 Agent 工具；
3. 项目运行配置存储与服务自动识别；
4. `ProcessManager`、日志缓冲、URL 检测和关闭清理；
5. `LocalOpener` 与右侧完整文件预览；
6. WebRuntime/API 状态接入；
7. 项目操作条、Git 审查和服务日志 UI；
8. 文档、跨平台验证、凭据扫描和完整回归测试。
