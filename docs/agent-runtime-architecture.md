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
| `model.py` | OpenAI Chat Completions 基础客户端适配 |
| `providers.py` | 多工具提供者组合与默认工具集构建 |
| `tools.py` | 文件、搜索、编辑与通用命令工具 |
| `git_service.py` | 参数数组式 Git 查询和写操作 |
| `git_tools.py` | Git Schema、参数验证和权限矩阵 |
| `devops_service.py` | Docker Compose 项目识别、环境选择、命令执行和结果归一化 |
| `devops_tools.py` | DevOps Schema、参数验证、审批矩阵和结构化错误 |
| `release_store.py` | 按工作区隔离的发布版本、回滚计划与审计事件原子存储 |
| `changes.py` | 对话级文件快照和累计 Diff |
| `context.py` | 上下文估算、摘要与保守裁剪 |
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
        ├── GitToolProvider -> GitService
        └── DevOpsToolProvider -> DevOpsService -> Docker CLI / Context
```

一次编程任务的主要流程：

1. 浏览器向 `/api/conversations/{id}/messages` 提交用户消息。
2. `WebRuntime` 校验对话状态并创建后台 Agent 任务。
3. `CodingAgent` 压缩上下文后调用模型并附带本地工具 Schema。
4. 模型返回工具调用时，组合提供者按工具名路由。
5. 工具进行参数、路径和权限检查；DevOps 长操作同时回传阶段、环境、耗时和完成比例，执行后返回结构化结果。
6. Agent 把 `tool` 消息交回模型，并把展示事件交给 `WebRuntime`。
7. 浏览器轮询 `/api/state`，渲染进度、审批、最终答复和文件改动。

## 对话与持久化

每个对话独立保存模型协议历史、网页展示条目、权限模式、工作目录关联、取消事件、运行状态和累计文件 Diff。

项目、对话和 Diff 状态写入 `.coding-agent/sessions.json`。存储层先写临时同级文件再原子替换；无效或旧版本数据在加载时经过校验与迁移，不影响 Web 服务启动。

## 工具组合与 Git

`build_default_tool_provider()` 为有工作目录的对话组合三组工具：

- 文件与命令：浏览、读取、搜索、写入、精确替换和受控命令；
- Git：status、diff、log、branches、create branch、stage、unstage、commit、pull 和 push。
- DevOps：inspect、preflight、build、pull、deploy、status、logs、verify、restart、stop、版本发布和两阶段回滚。

Git 命令使用参数数组和 `shell=False`。路径必须位于当前工作目录内；pull 固定使用 fast-forward-only；push 只使用已有上游或 `origin/当前分支`。hard reset、clean、force push 和删除远端引用不属于结构化能力，并由通用命令安全规则拒绝。

DevOps 控制面以 Docker Compose 为第一阶段目标。默认使用当前 Docker Context，也可从工作区内的 `coding-agent.toml` 选择预配置的远程 Context。Compose 文件必须位于工作区，环境、Context 和服务名称只接受安全字符；命令使用参数数组和 `shell=False`。部署先校验配置，再执行后台构建启动，等待容器 health 收敛后执行可选 HTTP 探针。日志限制行数并对常见 Token、密码和 URL 用户信息进行脱敏。

版本发布先生成只读门禁预览，逐项展示命令并按参数数组分类。Shell 和内联解释器直接拒绝，未知命令及本次任务修改过的门禁配置在 `full` 模式下也强制审批；审批绑定 `coding-agent.toml` SHA-256，服务执行前再次校验，避免确认后的配置替换。门禁随后采集 Git Commit、分支、脏工作区状态、Compose 摘要和检查结果；失败时不执行 Docker 变更。健康验证后记录 Compose 镜像引用和镜像 ID，并按环境维护活动版本。回滚计划与执行分离：计划只读、十分钟过期且只能使用一次；执行工具无视 `full` 权限豁免，始终创建人工审批。回滚先记录当前镜像现场，再检查历史镜像、恢复标签、使用 `--no-build` 重建服务并重新验证。数据库和数据卷明确位于自动回滚边界之外。

无工作目录的对话不暴露本地工具，仍可处理一般问答。

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

HTTP 服务按请求使用线程。每个运行中的对话有独立取消事件；Agent 会在模型调用和工具调用边界检查。通用命令和 Docker CLI 都使用独立进程组，超时或取消时终止整个进程树。DevOps 服务在 0.2 秒轮询周期内观察取消信号，并以约 0.5 秒间隔上报阶段耗时；进度只保存在内存状态中，避免长构建期间反复写会话文件。所有变更型 DevOps 操作还按工作区、Docker Context 和环境持有进程内互斥锁，冲突请求立即返回 `environment_busy`；取消和异常路径通过 `finally` 释放锁。

## 测试边界

测试覆盖 Agent 工具协议、上下文、文件、Git 与 DevOps 安全边界、WebRuntime、HTTP 边界、持久化和浏览器静态资源。Git 远端测试只使用本地 bare 仓库；Compose 测试使用记录参数数组的假 Docker Runner。自动化测试不调用真实模型 API、Docker Engine 或公共互联网。
