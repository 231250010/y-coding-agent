# Coding Agent 网页运行时架构

## 产品边界

Coding Agent 只提供本机网页版。唯一产品入口是：

```text
coding-agent -> coding_agent.web:main
```

HTTP 服务固定监听 `127.0.0.1`，浏览器负责项目、对话、审批、停止与 Diff 展示；模型请求、Agent 循环、文件和 Git 操作全部由本机 Python 进程执行。项目不包含桌面 GUI、终端 CLI、Web 框架或 Agent 框架。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `web.py` | 回环 HTTP 服务、静态资源、JSON 路由和 Web 安全边界 |
| `web_runtime.py` | 项目、对话、后台任务、审批、取消和状态快照 |
| `web_assets/` | 原生 HTML、CSS、JavaScript 网页界面 |
| `agent.py` | 模型—工具循环、事件、重复错误和步数终止 |
| `execution_state.py` | 修改版本、验证账本、完成证据与结果状态 |
| `model.py` | OpenAI Chat Completions 基础客户端适配、流式 tool call 与 DeepSeek `reasoning_content` 重组 |
| `providers.py` | 多工具提供者组合与默认工具集构建 |
| `tools.py` | 文件、搜索、编辑与通用命令工具 |
| `skills.py` | `SKILL.md` 发现、描述目录和按需指令加载 |
| `mcp.py` | MCP 配置、双向 stdio/Streamable HTTP、tools/resources/prompts、roots、通知和生命周期 |
| `git_service.py` | 参数数组式 Git 查询和写操作 |
| `git_tools.py` | Git Schema、参数验证和权限矩阵 |
| `devops_service.py` | Docker Compose 项目识别、环境选择、命令执行和结果归一化 |
| `worktree_service.py` | 从当前 HEAD 创建任务分支，并将项目子目录映射到独立 worktree |
| `devops_tools.py` | DevOps Schema、参数验证、审批矩阵和结构化错误 |
| `release_store.py` | 按工作区隔离的发布版本、回滚计划与审计事件原子存储 |
| `changes.py` | 对话级文件快照和累计 Diff |
| `context.py` | 上下文估算、摘要与保守裁剪 |
| `task_list.py` | 任务清单校验、结构化更新工具和 system 状态投影 |
| `session_store.py` | 本机会话原子持久化与兼容加载 |
| `local_settings.py` | Git 忽略的本机模型配置 |
| `directory_picker.py` | 为网页请求启动隔离的原生目录选择弹窗 |

## 请求与 Agent 数据流

```text
Browser
  │  loopback JSON API
  ▼
web.py / ThreadingHTTPServer
  │
  ▼
WebRuntime
  │  background task
  ▼
CodingAgent
  ├── ContextManager
  ├── ChatModel
  └── CompositeToolProvider
        ├── ToolRegistry (files / command)
        ├── TaskListToolProvider -> TaskListState
        ├── GitToolProvider -> GitService
        ├── GitHubActionsToolProvider -> GitHubActionsService -> gh CLI
        ├── DevOpsToolProvider -> DevOpsService -> Docker CLI / Context
        ├── SkillToolProvider -> SKILL.md / package resources
        └── MCPToolProvider
              ├── MCPStdioClient -> configured subprocess
              └── MCPStreamableHTTPClient -> configured endpoint
```

一次编程任务的主要流程：

1. 浏览器向 `/api/conversations/{id}/messages` 提交用户消息。
2. `WebRuntime` 校验对话状态并创建后台 Agent 任务。
3. `CodingAgent` 在预算阈值处把旧滚动摘要和新淘汰轮次合并为唯一的新摘要，再以流式 Chat Completions 调用模型并附带本地工具 Schema。文本 delta 立即进入临时展示状态；tool call delta 按 index 重组 ID、名称和参数，DeepSeek thinking 的 `reasoning_content` 单独重组但不进入可见文本。包含工具调用的 assistant 消息会原样保存该字段，并在工具结果后的下一轮请求中回传。摘要请求仍使用非流式调用，不把摘要正文展示给用户。

上下文裁剪采用软、硬两级预算。超过软阈值时只滚动摘要旧完整轮次，旧工具输出在进入摘要请求前以头尾保留方式限长；最近两轮不因软阈值截断。只有摘要后的消息仍超过硬上限时，才按时间从旧到新缩减近期工具结果，并同时保留开头、结尾和省略量，尽量保住命令尾部的错误与测试总结。
4. 模型返回工具调用时，组合提供者按工具名路由。Skill 平时只把名称与描述放进菜单，命中 `load_skill` 时才读取完整 `SKILL.md`，包内参考文件再经 `read_skill_resource` 二次披露。MCP Server 在首次生成 Schema 时完成 initialize、initialized 和 capability 检查；tools/list 的远端名称映射成唯一的 `mcp_<server>_<tool>`，resources/prompts capability 则启用只读桥接工具。stdio 读取线程和可选 HTTP GET SSE 监听把响应、通知与反向请求分流；tools/list_changed 使提供器原子重建远端工具菜单。连续且由本地提供者显式声明为只读的调用进入最多四线程的并行批次；未声明、参数无效、会改变状态或共享 MCP 连接的调用形成串行屏障。并行结果始终按原始 tool call 顺序写回，保持模型协议确定性。
5. 工具进行参数、路径和权限检查；DevOps 长操作同时回传阶段、环境、耗时和完成比例，执行后返回结构化结果。每个完整 `ToolResult` 先写入协议历史检查点，再发送可选的网页展示事件。
6. `ExecutionState` 根据统一 Diff 追踪递增修改版本，并把参数数组式测试、构建、静态检查和 Compose 验证记入有界验证账本。模型首次尝试结束但当前版本缺少成功证据时，Agent 追加一次完成门禁提醒；仍无法验证时允许结束，但结果明确为 `completed_unverified`。
7. Agent 把 `tool` 消息交回模型，并把展示事件交给 `WebRuntime`。
8. 浏览器通过 `/api/events` 的 SSE 长连接接收带递增 revision 的状态快照，实时渲染模型文本、工具、审批和部署进度；每五秒的 `/api/state` 轮询负责初始加载、断线恢复和旧浏览器兼容，较旧 revision 不会覆盖新状态。

## 对话与持久化

每个对话独立保存模型协议历史、网页展示条目、权限模式、工作目录关联、取消事件、运行状态、累计文件 Diff 和执行证据。生成中的 assistant 文本仅存在于 `WebTask` 内存状态，最多约每 50 ms 唤醒一次 SSE 订阅者；用户消息、压缩后的历史、每条完整工具结果和最终状态才触发持久化检查点，避免按 token 高频写盘，同时缩短工具已修改磁盘但协议证据尚未保存的崩溃窗口。

多阶段目标使用 `TaskListState` 独立保存，不依赖模型消息历史。`update_task_list` 采用完整快照语义并原子校验目标、稳定 ID、状态和阻塞原因；持久化失败时回滚内存状态。Agent 在每次模型调用前删除旧投影并插入唯一的最新 system 锚点，位置在首条用户消息之前。上下文压缩把该锚点视为稳定前言，只滚动摘要普通历史；网页则从同一状态对象渲染任务飞行计划，避免出现两份进度来源。

项目、对话、Diff 和执行证据状态写入版本 6 的 `.coding-agent/sessions.json`。存储层先写临时同级文件再原子替换；无效或旧版本数据在加载时经过校验与迁移，不影响 Web 服务启动。

## 工具组合与 Git

`build_default_tool_provider()` 为有工作目录的对话组合七组工具：

- 文件与命令：浏览、读取、搜索、单文件或批量写入、单文件或批量精确替换和受控命令；`run_process` 使用参数数组与 `shell=False`，优先承载可识别的测试、构建和静态检查；批量操作限制文件数和总内容，全量预检通过后才提交，提交异常时恢复原文件；
- 任务计划：维护独立持久化的目标、阶段、进度与阻塞原因；
- Git：status、diff、log、branches、create branch、stage、unstage、commit、pull 和 push。
- GitHub Actions：按 Commit 查询状态、读取失败日志，以及人工确认后重跑失败任务；
- DevOps：inspect、preflight、build、pull、deploy、status、logs、verify、restart、stop、版本发布和两阶段回滚。
- Skill：扫描本机和项目 `.coding-agent/skills/*/SKILL.md`，常驻有界描述目录，按需读取正文和目录内单个 UTF-8 资源；`scripts/` 内受支持脚本只能在逐次审批后以固定解释器、最小环境和工作区 cwd 执行，修改进入统一 Diff；
- MCP：读取本机和项目 `.coding-agent/mcp.json`，自行管理 stdio/Streamable HTTP JSON-RPC 生命周期、远端工具命名空间、resources、prompts 和受控 sampling。

MCP 配置只允许按变量名选择环境变量或 HTTP Header，不接受字面 secret 字段。stdio 子进程使用最小继承环境，不默认获得模型 API Key；显式传入的变量值会从 stderr、协议错误、通知和工具结果中脱敏。配置命令使用参数数组和 `shell=False`，`cwd` 必须留在工作区。Streamable HTTP 支持 POST JSON/SSE、Session ID、DELETE，以及显式 `listen=true` 的 GET SSE。客户端只声明当前工作区作为 roots；ping 自动响应。sampling 每次都审批，单任务限 3 次，以独立安全提示、无历史、无工具、受限输入与输出执行，并与主循环共用模型锁；elicitation 固定拒绝。通知在内存保留最近 50 条，GET 断开只记状态而不无限后台重试。MCP 工具不受本地路径与命令分类器保护，因此 `request` 模式全部审批，`risk` 模式只放行 Server 明确标记为只读的工具；resource/prompt 始终作为不可信 tool 内容。只有只读请求会在连接中断后重新 initialize 并重试，避免重复外部写操作。Provider 记录每个 Server 的最近成功/失败与连续失败数；连续 3 次失败进入 5 秒起、最高 60 秒的指数冷却，显式 `mcp_reconnect` 经人工批准后重建连接、清除冷却并原子刷新工具集合。Agent 任务退出时统一关闭子进程、HTTP 监听和 Session。

HTTP 401/403 走独立授权状态，不污染传输健康计数。401 challenge 中的 Protected Resource Metadata 只允许同源获取，禁止重定向和凭据 URL，不复用 MCP Authorization Header，并严格验证 RFC 9728 `resource`；远程元数据域名解析到非公网地址时按潜在 SSRF 拒绝。Authorization Server Metadata 是第二段显式审批操作，最多处理 4 个 issuer，按 RFC 8414 构造 well-known URL并验证返回 issuer，只输出白名单字段。本阶段止于发现，不实现浏览器授权码、PKCE、动态注册、token 交换或持久化。

Skill 脚本和 MCP Server 都是应用层受控扩展，不构成 OS 沙箱。前者必须先完整读取并记录 SHA-256，运行审批前后摘要一致且用户明确批准才会启动；后者位于本地文件/命令分类器之外。信任来源、最小凭据与最小权限仍是部署责任。

Git 命令使用参数数组和 `shell=False`。路径必须位于当前工作目录内；pull 固定使用 fast-forward-only；push 只使用已有上游或 `origin/当前分支`。hard reset、clean、force push 和删除远端引用不属于结构化能力，并由通用命令安全规则拒绝。

GitHub Actions 控制面只调用本机 `gh` CLI，并依赖开发者预先完成的 `gh auth login`。状态查询按 Commit 获取每个 workflow 最新一次运行；失败日志有界并脱敏；远端重跑在任何权限模式下都创建人工审批。发布配置可以要求指定 workflow 全部成功，run ID 和 URL 会进入发布来源证据。

网页右侧工作台复用 Diff 面板承载只读发布控制台。后端按当前对话的工作区重新解析 Compose 配置，汇总环境操作锁、容器状态、活动版本、Git/CI/镜像来源证据，并只返回经过裁剪的展示字段。页面中的历史版本操作只生成 Agent 回滚计划提示，不直接执行回滚，因此不会绕过一次性计划和人工审批边界。

任务级 worktree 通过网页中的独立确认对话框显式创建。分支名只由内部任务 ID 派生，目标目录位于 Git 忽略的本机状态目录，Git 以固定参数数组执行；所选项目即使是仓库子目录，也会在新 worktree 中保持相同相对位置。会话保存来源项目与隔离路径，工具提供器和变更追踪器统一使用隔离路径。DevOps 读取隔离目录中的 Compose 与源码，但发布记录和环境锁仍使用来源项目作为身份，防止同一部署目标因 worktree 路径不同而并发执行。

DevOps 控制面以 Docker Compose 为第一阶段目标。默认使用当前 Docker Context，也可从工作区内的 `coding-agent.toml` 选择预配置的远程 Context。Compose 文件必须位于工作区，环境、Context 和服务名称只接受安全字符；命令使用参数数组和 `shell=False`。部署先校验配置，再执行后台构建启动，等待容器 health 收敛后执行可选 HTTP 探针。日志限制行数并对常见 Token、密码和 URL 用户信息进行脱敏。

版本发布先生成只读门禁预览，逐项展示命令并按参数数组分类。Shell 和内联解释器直接拒绝，未知命令及本次任务修改过的门禁配置在 `full` 模式下也强制审批；审批绑定 `coding-agent.toml` SHA-256，服务执行前再次校验，避免确认后的配置替换。门禁随后采集 Git Commit、分支、脏工作区状态、Compose 摘要和检查结果；失败时不执行 Docker 变更。健康验证后记录 Compose 镜像引用和镜像 ID，并按环境维护活动版本。回滚计划与执行分离：计划只读、十分钟过期且只能使用一次；执行工具无视 `full` 权限豁免，始终创建人工审批。回滚先记录当前镜像现场，再检查历史镜像、恢复标签、使用 `--no-build` 重建服务并重新验证。数据库和数据卷明确位于自动回滚边界之外。

无工作目录的对话不暴露文件、Git、CI 或 DevOps 工具，仍可使用任务清单、本机级 Skill，以及没有配置 `cwd` 的本机级 MCP Server。

## 权限与审批

- `request`：只读操作自动执行，编辑、测试及 Git 写操作询问；
- `risk`：普通工作区编辑自动执行，联网、远端 Git 和高风险命令询问；
- `full`：减少常规审批，但不可恢复 Git 历史修改、提权和系统控制仍拒绝。

DevOps 的构建、拉取、部署、重启和停止在 `request` 与 `risk` 中都需要审批，因为它们可能改变远程环境；只读预检、状态、日志和验证自动执行。停止操作不会删除容器或数据卷，控制面不提供 `down -v`、任意远程命令或自动回滚。

需要审批时，后台 Agent 创建审批记录并等待。浏览器通过 `/api/approvals/{id}` 返回决定；拒绝会作为可恢复工具错误交给模型。

## Web 安全边界

- 仅绑定 `127.0.0.1`；
- 校验 `Host` 和写请求 `Origin`；
- 静态资源使用固定映射；
- JSON 请求限制为 1 MiB；
- 响应包含 CSP、`nosniff` 和 no-referrer；
- API 不返回密钥或服务端堆栈；
- 文件路径在规范化和符号链接解析后检查工作区边界。

这些措施属于应用层纵深防护，不等同于操作系统沙箱。

## 并发与取消

HTTP 服务按请求使用线程，SSE 使用条件变量等待 revision 变化并定期发送保活注释，不进行忙轮询。每个运行中的对话有独立取消事件；流式模型每收到一个 chunk 都检查取消，回调中止时关闭流且不重试已经产生部分内容的请求，避免重复文本。通用命令和 Docker CLI 都使用独立进程组，超时或取消时终止整个进程树。DevOps 服务在 0.2 秒轮询周期内观察取消信号，并以约 0.5 秒间隔上报阶段耗时；进度只保存在内存状态中，避免长构建期间反复写会话文件。所有变更型 DevOps 操作按工作区、Docker Context 和环境同时持有进程内锁与跨进程文件锁，冲突请求返回 `environment_busy`；发布状态的读改写阶段另持有全局事务锁，冲突返回 `release_store_busy`。锁由操作系统文件描述符所有，进程崩溃也会释放；持有者摘要写入独立 sidecar，供其他进程安全读取。

## 测试边界

测试覆盖 Agent 工具协议、上下文、完成证据门禁、验证账本、工具级持久化检查点、文件、Git 与 DevOps 安全边界、WebRuntime、HTTP 边界、持久化和浏览器静态资源。Git 远端测试只使用本地 bare 仓库；Compose 测试使用记录参数数组的假 Docker Runner；发布锁测试启动独立 Python 子进程验证真实跨进程竞争和释放。自动化测试不调用真实模型 API、Docker Engine 或公共互联网。
