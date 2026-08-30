# Coding Agent：从零实现的本地编程智能体

这是一个个人独立设计并实现、面向开发运维部署一体化的编程智能体。它在本机启动网页工作台，通过支持 OpenAI Chat Completions tool calling 的模型自主读取与修改工作区文件、执行命令、操作 Git，并对 Docker Compose 应用执行部署前检查、构建部署、状态验证、日志读取和服务运维，直到完成任务或触发安全终止条件。

浏览器只承担展示和输入。对话历史、上下文压缩、工具协议、本地调度、模型输出解析、循环控制、安全策略和错误恢复均由本仓库代码实现。

## 依赖边界

本项目严格区分“模型 API 客户端”和“Agent 框架”：

- 允许使用 PyPI 包 `openai` 中的基础客户端 `OpenAI`，只调用 `client.chat.completions.create(...)`。
- `openai` 只发送 HTTP 请求和解析 Chat Completions 响应，不参与 Agent 编排。
- 不使用 `openai-agents`、OpenAI Agents SDK、LangChain、LlamaIndex、AutoGen、CrewAI、Claude Agent SDK 或其他 Agent 框架。
- 不在 Claude Code、Codex、OpenCode 等现成 Agent 产品上封装界面。
- 不使用 Code Interpreter、Files API、File Search、Computer Use 等服务端托管工具。
- 文件访问、命令执行、参数验证、tool call 回传、上下文管理和循环终止全部发生在本机。

网页服务也没有引入 Web 框架：它使用 Python 标准库 `ThreadingHTTPServer`，前端使用原生 HTML、CSS 和 JavaScript。运行依赖为基础客户端 `openai`，以及为系统 SOCKS 代理提供传输支持的 `httpx[socks]`；它们都不承担 Agent 编排职责。

## 工作原理

```text
浏览器（项目 / 对话 / 审批 / Diff）
   │ 本机 JSON API
   ▼
127.0.0.1 Web 服务
   │
   ▼
本地 Agent 循环 ──────── 上下文预算与自动摘要
   │
   ├─ 调用 Chat Completions（附本地工具 JSON Schema）
   │
   ├─ 模型直接回答 ───────────────► 结束
   │
   └─ 模型返回 tool_calls
          │
          ▼
      参数校验与安全策略
          │
          ▼
      本地工具执行
          │
          ▼
      tool_call_id 对应的 tool 消息
          │
          └───────────────────────► 下一轮模型调用
```

核心模块：

- `agent.py`：自主循环、模型输出解析、工具结果回传和终止条件。
- `model.py`：`openai` 基础客户端的薄适配层，不包含 Agent 逻辑。
- `tools.py`：工具 Schema、参数校验和六个本地工具。
- `git_service.py` / `git_tools.py`：结构化 Git 控制面和审批规则。
- `worktree_service.py`：为网页对话创建任务级 Git 分支和隔离工作区。
- `github_actions_service.py` / `github_actions_tools.py`：基于本机 `gh` CLI 的 CI 状态、失败日志和重跑控制面。
- `devops_service.py` / `devops_tools.py`：结构化 Docker Compose 控制面、环境配置和审批规则。
- `safety.py`：命令风险分类。
- `context.py`：token 粗略估算、完整轮次摘要和保守裁剪。
- `web_runtime.py`：项目、对话、后台任务、审批和会话持久化。
- `web.py`：仅监听回环地址的标准库 HTTP 服务和 JSON 路由。
- `web_assets/`：浏览器界面，不执行文件或命令工具。
- `changes.py`：对话级文件快照、变更追踪和 Diff 数据。
- `session_store.py`：被 Git 忽略的本机会话存储。

## 安装

要求 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

如需 GitHub Actions 集成，另行安装 [GitHub CLI](https://cli.github.com/) 并执行一次 `gh auth login`。Coding Agent 复用 `gh` 的本机认证，不读取、保存或返回 GitHub Token。

macOS 或 Linux：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 模型配置

支持标准 OpenAI 或兼容 Chat Completions 网关。凭据从环境变量或 Git 忽略的 `.coding-agent/config.json` 读取：

- `CODING_AGENT_API_KEY`：API 凭据。
- `CODING_AGENT_MODEL`：支持 function calling 的模型名称。
- `CODING_AGENT_BASE_URL`：兼容网关地址。
- `CODING_AGENT_CONTEXT_TOKENS`：上下文预算，默认 `32000`。

也可以在网页的“模型与运行设置”中保存本机配置。页面永远不会把已经配置的 API Key 返回给浏览器；新 Key 只有在明确勾选“保存到本地配置”后才会写入 `.coding-agent/config.json`。

`.coding-agent/`、`.env` 和虚拟环境均已加入 `.gitignore`。真实凭据不得写入源码、README、测试数据、日志或提交记录。

## 启动本机网页版

```powershell
coding-agent
```

或者：

```powershell
python -m coding_agent
```

程序默认打开：

```text
http://127.0.0.1:8000/
```

常用参数：

```powershell
python -m coding_agent --workspace C:\path\to\project
python -m coding_agent --port 8123
python -m coding_agent --no-browser
```

网页服务固定绑定 `127.0.0.1`，不能改成局域网或公网地址。若关闭了自动打开浏览器，请在服务启动后手动访问终端显示的本机地址。

## 网页工作台

- 左侧项目树：一个工作目录对应一个项目，项目下包含该目录中的全部对话。
- 中间交互区：展示用户消息、模型答复、工具进度和输入框。
- 工作目录：可调用本机目录选择器，也可输入绝对路径；不同对话可以绑定不同目录。
- 文件改动：每轮任务结束后只显示一次“本轮改动”，不会为每次工具调用重复生成入口。
- 右侧工作台：点击改动文件进入 Diff；点击页头“发布台”查看 Compose 连接、发布门禁、各环境锁状态、服务健康、活动版本和版本时间线。
- 对话权限：输入框旁可选择“请求批准”“帮我批准”或“完全访问权限”，每个对话独立保存。
- 任务隔离：页头“隔离”会在二次确认后从当前 `HEAD` 创建 `coding-agent/task-<id>` 分支和独立 worktree；该对话的 Agent、Diff、测试与 DevOps 随后全部切换到隔离目录。
- 停止：取消 Agent 循环，并尝试终止当前本地命令。
- 部署进度：Compose 操作显示目标环境、已用时间和真实阶段轨道；部署依次展示配置校验、构建启动与健康验证。
- 取消部署：进度条中的“取消部署”会设置对话取消信号，并终止当前 Docker CLI 进程组及其构建子进程。
- 回滚入口：发布时间线只负责展示审计证据；选择历史版本会把“生成回滚计划”写入对话输入框，仍由 Agent 生成预览并走人工确认，不会从页面直接绕过审批。
- 本机会话：项目、对话和 Diff 追踪保存在 `.coding-agent/sessions.json`，不会进入 Git。

网页通过仅监听回环地址的本机 Python 服务打开原生目录选择器；选择结果只作为该对话的工作目录保存在本机，不会上传到远程服务。

## 本地工具

| 工具 | 功能 | 关键限制 |
| --- | --- | --- |
| `list_files` | 浏览目录 | 跳过 `.git`，限制结果数 |
| `read_file` | 按行读取 UTF-8 文本 | 限制行数和输出长度 |
| `search_text` | 搜索文本 | 优先 `rg`，提供纯 Python 回退 |
| `write_file` | 创建或覆盖文本文件 | 限制单次内容大小 |
| `replace_text` | 精确替换文本 | 默认要求唯一匹配 |
| `run_command` | 执行工作区命令 | 风险分类、确认、超时和输出截断 |
| `git_status` / `git_diff` | 查询分支、上游、文件状态以及工作区/暂存区 Diff | 只读；支持限定工作区相对路径 |
| `git_log` / `git_branches` | 查询提交记录与本地分支 | 数量和输出有上限 |
| `git_create_branch` | 创建并切换本地分支 | 校验 Git 分支名，不覆盖现有分支 |
| `git_stage` / `git_unstage` | 暂存或取消暂存明确路径 | 不接受工作区外路径，不丢弃工作区内容 |
| `git_commit` | 提交当前暂存内容 | 不自动暂存；要求单行提交消息 |
| `git_pull` | 拉取当前分支 | 固定使用 `--ff-only`，不自动合并 |
| `git_push` | 推送当前分支 | 仅使用已有上游或 `origin/当前分支`，不支持 force/refspec |
| `github_actions_status` | 查询当前或指定 Commit 的最新工作流状态 | 只读；每个 workflow 只采用最新一次运行 |
| `github_actions_failed_logs` | 读取失败步骤日志 | 只读；限制字符数并脱敏常见凭据 |
| `github_actions_rerun_failed` | 重跑指定 run 的失败任务 | 修改远端 CI 状态，任何权限模式下都要求人工确认 |
| `devops_inspect` / `compose_preflight` | 识别技术栈、Compose 文件和环境；检查 Engine、Compose 与配置 | 只读，不启动容器 |
| `compose_status` / `compose_logs` / `compose_verify` | 查询服务状态、读取有界日志、汇总健康结果 | 日志最多 1000 行并脱敏常见凭据 |
| `compose_build` / `compose_pull` | 构建或拉取镜像 | 修改镜像状态，必须审批（完全访问模式除外） |
| `compose_deploy` | 校验配置，执行 `up --detach --build`，立即验证 | 不跳过 preflight/verify，不声称未验证的成功 |
| `compose_release` / `compose_releases` | 执行门禁、发布命名版本并查询来源证据、活动版本和镜像 ID | 门禁与健康验证均成功后才切换活动版本；版本号不可重复 |
| `compose_rollback_plan` | 生成十分钟有效的一次性回滚预览 | 只读；明确来源版本、目标版本、服务和镜像影响 |
| `compose_rollback` | 恢复目标镜像、重建服务并再次健康验证 | 必须使用预览生成的计划 ID，任何权限模式下都要求人工确认 |
| `compose_restart` / `compose_stop` | 重启或停止服务 | 不执行 `down`，不删除容器、网络或数据卷 |

常见 Git 工作流使用参数数组直接调用 Git，不经过 Shell。Git 结果使用结构化 JSON 返回，并区分非仓库、无内容可提交、认证失败、远端拒绝、无法快进/冲突和其他失败。`git_pull` 造成的文件变化也会进入当前对话的累计 Diff。

### 任务级 worktree 隔离

worktree 隔离是显式启用的，不会在新建对话时暗中修改仓库。创建前要求当前对话尚未产生文件改动，并要求仓库至少有一个提交；来源工作区的未提交内容不会复制到隔离目录。隔离成功后，会话持久化 worktree 路径、任务分支、来源分支和基准 Commit，服务重启后仍恢复到同一目录。若目录被外部删除，会话安全回退到项目工作区并给出系统提示。

隔离分支不会自动合并、rebase、删除或清理。开发者应在 Diff 审查后提交任务分支，再通过已有 Git 工具推送并创建 PR，或在仓库中自行合并。删除网页对话只删除会话记录，故意保留 worktree 与分支，避免丢失未提交代码。隔离 worktree 与来源项目共享发布历史和环境操作锁，因此两个对话不会绕过同一 Docker 环境的并发保护。

结构化 Git 权限规则：

- `status`、`diff`、`log`、`branches` 在三种模式下均自动执行。
- 创建分支、暂存、取消暂存和提交在“请求批准”模式询问，在“帮我批准”和“完全访问权限”模式自动执行。
- `pull`、`push` 在“请求批准”和“帮我批准”模式询问，仅在“完全访问权限”模式自动执行。
- hard reset、clean、force push 和删除远端引用不属于结构化工具；通用命令的既有破坏性操作拒绝规则继续生效。

工具参数由项目自己的轻量验证器依据 JSON Schema 校验。非法 JSON、缺少参数、未知参数和未知工具都会成为结构化 `tool` 错误结果返回模型，让模型有机会修正调用。

## 开发、部署与运维一体化

### 目标环境选择

本项目把第一阶段交付目标定义为 **Docker Compose 单机环境**，同时用 Docker Context 支持远程 Linux 主机。这个范围适合课程项目，也能形成完整且可演示的闭环：

```text
分析项目 → 修改代码 → 运行测试 → 检查 Git Diff
    → Compose 预检 → 构建/部署 → 健康验证
    → 查看状态/日志 → 修复问题 → 再次验证
```

与直接引入 Kubernetes 相比，Compose 能覆盖个人开发者、小团队测试环境和单机应用部署的高频需求，并把编排复杂度控制在项目可以完整实现、测试和解释的范围内。通过预先创建的 Docker Context，同一套结构化工具可以操作本机 Docker Desktop，也可以操作经 SSH 连接的远程 Docker Engine；智能体不保存 SSH 私钥或仓库凭据。

### 项目配置

如果项目根目录已有 `compose.yaml`、`compose.yml`、`docker-compose.yaml` 或 `docker-compose.yml`，无需配置即可使用当前 Docker Context。需要多个环境时，可创建版本化的 `coding-agent.toml`：

```toml
[devops]
compose_file = "compose.yaml"
default_environment = "local"

[devops.environments.local]
health_timeout_seconds = 45
health_interval_seconds = 2

[[devops.environments.local.http_probes]]
name = "web-api"
url = "http://127.0.0.1:8088/health"
expected_status = 200
timeout_seconds = 3

[devops.environments.staging]
docker_context = "staging-host"

[devops.release]
require_git = true
require_clean_worktree = true

[[devops.release.checks]]
name = "unit-tests"
command = ["python", "-m", "pytest", "-q"]
timeout_seconds = 300

[devops.github_actions]
require_success = true
workflows = ["tests", "lint"]
```

其中 `staging-host` 应由开发者提前使用 `docker context create` 配置。健康验证会在超时范围内等待 Compose healthcheck 从 `starting` 收敛，并在容器就绪后执行可选 HTTP 探针。发布门禁可以强制要求 Git 提交、干净工作区和一组参数数组形式的检查命令。配置只保存 Context 名称，不保存主机密码、私钥、Token 或环境变量值；所有 Docker 和门禁命令都不经过 Shell。

发布确认会完整展示每个门禁检查的名称、参数数组、超时和安全分类。`pytest`、受限的 `python -m pytest/unittest/compileall/py_compile` 以及常见构建命令可以作为已识别检查；Shell、提权解释器和 `python -c` 直接拒绝，其他自定义程序必须人工确认，即使处于 `full` 模式。本次对话修改过 `coding-agent.toml` 时也会强制再次确认。确认绑定配置文件 SHA-256，确认后配置发生变化会以 `release_gate_approval_required` 停止，不会执行 Docker 变更。

启用 `devops.github_actions.require_success` 后，正式发布会查询当前 Commit；配置的 workflow 必须全部存在、完成且结论为 `success`，否则在任何 Docker 变更前停止。成功证据中的 workflow run ID、URL、Commit 和检查时间随版本记录保存。日常诊断可以先调用状态工具，读取失败 run 的日志，修复并推送代码；重新运行失败任务始终需要人工确认。

推荐在网页中这样下达任务：

> 检查这个项目的测试和 Compose 配置。修复测试失败，展示 Git Diff；部署到 staging 前先做预检，得到批准后部署，并用容器状态和 healthcheck 验证结果。如果失败，读取 web 服务最近 200 行日志并继续诊断。

仓库中的 `examples/devops-demo/` 是一个可直接选择为工作目录的最小演示项目，提供 Dockerfile、Compose 配置、healthcheck 和本机回环端口，便于答辩时展示完整工具链。首次构建需要 Docker 能够拉取 Python 基础镜像。

### DevOps 审批边界

- 环境识别、预检、状态、日志和健康验证属于只读查询，在三种权限模式下自动执行。
- 构建、拉取、部署、重启和停止会改变镜像、容器或服务状态，在 `request` 和 `risk` 模式下均要求明确批准。
- 只有 `full` 模式自动执行这些状态变更；它仍不提供删除数据卷、远程主机任意命令、提权或破坏 Git 历史的结构化能力。
- `compose_stop` 只停止服务，不执行 `down -v`；不提供失败后的无人值守自动回滚，失败时保留现场供开发者审查日志和状态。

### 部署进度与取消语义

进度不是按计时器伪造的完成比例，而由后端在实际命令边界上报告：每个阶段开始时显示此前已经完成的比例，Docker 命令运行期间持续更新该阶段的已用时间，命令成功后才推进进度。`compose_deploy` 的三个阶段分别是：

1. 校验 Compose 配置；
2. 构建镜像并启动服务；
3. 等待容器 healthcheck 收敛，并执行配置的 HTTP 应用探针。

Docker CLI 运行在独立进程组中。用户取消后，Windows 使用 `taskkill /T`，macOS/Linux 向进程组发送终止信号；应用随后回收输出管道并返回稳定的 `operation_cancelled` 错误。取消不会自动执行 `compose down` 或回滚已经完成的阶段，因此已创建的镜像或已启动的容器会保留，便于审查现场。

### 版本化发布与人工确认回滚

`compose_release` 接受 `v1.4.0`、`2026.08.30` 等明确版本号。任何 Docker 变更前都会执行发布门禁；通过后才部署、等待健康收敛并读取 `docker compose images --format json`。活动版本因此不是一个容易漂移的标签别名，而是一条可以审计并用于恢复镜像标签的发布记录。

发布与回滚状态保存在 Coding Agent 自身的 `.coding-agent/releases/` 下，并按工作区绝对路径哈希隔离，不写入被操作项目，也不会进入 Git。记录包含环境、版本、时间、Git Commit/分支/脏状态、Compose SHA-256、检查结果、服务、镜像 ID、健康结果和回滚事件，不保存凭据或环境变量值。每次保存增加单调修订号，写入带 PID 和随机标识的唯一临时文件，执行 `fsync` 后再原子替换；格式或修订号损坏时拒绝继续发布，而不是静默覆盖历史。

所有会改变 Compose 状态或发布审计记录的操作，都按“工作区 + Docker Context + 环境”同时获取进程内锁和操作系统文件锁。同一环境已有部署、发布或回滚时，其他对话或其他网页服务进程返回 `environment_busy` 和持有者 PID/操作摘要。版本发布、回滚计划和回滚还在完整操作期间持有发布状态事务锁，避免不同环境读取旧状态后覆盖记录；冲突返回 `release_store_busy`。锁跟随文件描述符，进程退出后由操作系统释放，不依赖容易遗留的锁文件所有权。

回滚采用强制两阶段协议：

1. `compose_releases` 查询目标环境的活动版本和历史；
2. `compose_rollback_plan` 生成十分钟有效、只能使用一次的计划 ID，并返回影响预览；
3. `compose_rollback` 根据计划弹出人工确认。即使对话处于 `full` 模式，也必须由用户批准；
4. 执行前记录当前镜像现场，确认目标镜像仍存在，恢复镜像标签并以 `--no-build` 重建服务；
5. 重新执行健康验证，成功后才切换活动版本，并写入回滚审计记录。

回滚只处理应用镜像与 Compose 服务，不自动回滚数据库 schema、数据卷或外部依赖。数据库迁移必须由项目提供向后兼容策略或独立人工流程。

## 安全模型

### Web 边界

- 服务只监听 `127.0.0.1`。
- HTTP `Host` 只接受回环主机，写请求拒绝跨站 `Origin`。
- 静态资源使用固定映射，不允许任意路径读取。
- JSON 请求限制大小，所有错误返回不包含凭据或堆栈。
- API 状态只暴露“凭据已配置/未配置”，不返回 API Key。

### 权限模式

输入框旁的权限选择只作用于当前对话；切换后会同时成为后续新对话的默认值，已有对话不受影响：

1. `request`（请求批准）：读取文件和明确只读命令可自动执行；创建、覆盖或替换文件，以及可能联网、生成文件或修改系统状态的命令均先询问。
2. `risk`（帮我批准，默认）：普通工作区文件编辑和已识别的测试、构建命令自动执行；联网、安装、删除、环境修改和未识别命令先询问。
3. `full`（完全访问权限）：不再为文件或联网操作询问，并允许文件工具使用工作区外的绝对路径；原安全要求中的提权、系统控制、不可恢复操作和破坏 Git 历史仍直接拒绝。

### 文件边界

在 `request` 和 `risk` 模式下，每个文件路径都会相对于工作区解析，并使用规范化后的真实路径再次检查。以下访问会被拒绝：

- 使用 `..` 逃逸工作区；
- 指向工作区之外的绝对路径；
- 工作区内指向外部目标的符号链接。

### 命令边界

命令分为三级：

1. `safe`：已识别的只读、测试或构建命令，在默认模式下自动执行。
2. `review`：联网、安装、修改、删除或未识别命令，需要用户确认。
3. `deny`：提权、系统控制、根目录删除、破坏 Git 历史等命令直接拒绝。

命令始终以工作区为当前目录，并有最长运行时间和输出上限。但该分类器只是应用层纵深防护，不是操作系统沙箱；尤其是“完全访问权限”不能替代操作系统隔离。请在容器、虚拟机或低权限账户中运行不受信任模型；最保守时使用“请求批准”。

## 上下文与终止条件

历史达到配置预算约 80% 时，项目代码会：

1. 按用户消息划分完整轮次，避免拆开 assistant 的工具调用和对应 tool 结果。
2. 保留系统提示与最近两个完整轮次。
3. 通过一次不提供工具的 Chat Completions 请求，把上一版摘要与本次淘汰轮次合并成一条滚动摘要；新摘要原位替换旧摘要，不会逐次累积。
4. 摘要失败时插入裁剪说明并保留近期轮次。
5. 必要时进一步截断过长的旧工具输出。

单个任务在以下任一情况终止：模型给出最终文本、达到最大步骤数、连续三次出现相同工具错误、用户停止，或模型 API 返回不可恢复错误。限流、连接失败和超时采用有限次数指数退避；认证和请求参数错误不重试。

## 测试

```powershell
python -m pytest
```

自动化测试使用脚本化假模型和假 Docker Runner，不访问真实 API、Docker Engine 或远程服务器。覆盖 Agent 循环、工具协议、文件与命令安全、Git 与 Compose 参数数组、DevOps 环境边界、审批矩阵、日志脱敏、部署验证、上下文压缩、会话持久化、本机 HTTP 边界、网页状态机、项目/对话管理和 Diff 展示。

## 人工端到端演示

选择一个可丢弃的示例目录，在网页中交给 Agent：

> 阅读项目并找到现有测试方式。为其中一个核心函数添加参数校验和测试，运行测试，根据失败继续修复，最后总结修改和验证结果。

应能观察到模型先列出和读取文件，再写入或精确替换内容，运行测试，并把测试结果反馈给模型继续判断。任务结束后只出现一次改动汇总，点击文件可在右侧审查 Diff。整个过程不依赖服务端代码执行或任何 Agent SDK。

## 已知限制

- 服务只供本机单用户使用，不提供身份认证或远程部署。
- 多个工具调用按模型返回顺序串行执行。
- 只处理 UTF-8 文本文件，不编辑二进制文件。
- 不自动提交 Git；结构化提交只会在模型明确调用且权限规则允许时执行。
- 暂不提供 Git 图形 merge/rebase、自动清理 worktree、PR 创建、插件系统或自主网络搜索。
- DevOps 第一阶段只支持 Docker Compose；不包含 Kubernetes、多主机编排、流量切换或失败后的无人值守自动回滚。
- 部署验证支持 Compose healthcheck 和配置化 HTTP 探针；未配置二者的服务只能确认处于 running，不能证明业务接口正确。
- Compose 进度以阶段和已用时间为粒度，不解析 BuildKit 的逐层百分比；取消后已经完成的镜像层或容器状态不会自动回滚。
- 已被 Docker 垃圾回收的历史镜像无法直接回滚；系统会在修改服务前检查镜像 ID 并以结构化错误停止。当前版本记录是本机控制面的审计数据，不替代远程镜像仓库的保留策略。
- 不同兼容网关对 Chat Completions tool calling 的实现程度可能不同。
