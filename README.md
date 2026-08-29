# Coding Agent：从零实现的本地编程智能体

这是一个个人独立设计并实现的编程智能体。它在本机启动网页工作台，通过支持 OpenAI Chat Completions tool calling 的模型自主读取与修改工作区文件、执行命令、观察结果并继续迭代，直到完成任务或触发安全终止条件。

浏览器只承担展示和输入。对话历史、上下文压缩、工具协议、本地调度、模型输出解析、循环控制、安全策略和错误恢复均由本仓库代码实现。

## 依赖边界

本项目严格区分“模型 API 客户端”和“Agent 框架”：

- 允许使用 PyPI 包 `openai` 中的基础客户端 `OpenAI`，只调用 `client.chat.completions.create(...)`。
- `openai` 只发送 HTTP 请求和解析 Chat Completions 响应，不参与 Agent 编排。
- 不使用 `openai-agents`、OpenAI Agents SDK、LangChain、LlamaIndex、AutoGen、CrewAI、Claude Agent SDK 或其他 Agent 框架。
- 不在 Claude Code、Codex、OpenCode 等现成 Agent 产品上封装界面。
- 不使用 Code Interpreter、Files API、File Search、Computer Use 等服务端托管工具。
- 文件访问、命令执行、参数验证、tool call 回传、上下文管理和循环终止全部发生在本机。

网页服务也没有引入 Web 框架：它使用 Python 标准库 `ThreadingHTTPServer`，前端使用原生 HTML、CSS 和 JavaScript。运行依赖仍只有 `openai` 和 `rich`。

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
- `safety.py`：命令风险分类。
- `context.py`：token 粗略估算、完整轮次摘要和保守裁剪。
- `web_runtime.py`：项目、对话、后台任务、审批和会话持久化。
- `web.py`：仅监听回环地址的标准库 HTTP 服务和 JSON 路由。
- `web_assets/`：浏览器界面，不执行文件或命令工具。
- `changes.py`：对话级文件快照、变更追踪和 Diff 数据。
- `session_store.py`：被 Git 忽略的本机会话存储。
- `cli.py`：备用终端入口，不是默认产品界面。

## 安装

要求 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

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

备用 CLI 仍可用于自动化和调试：

```powershell
coding-agent-cli --workspace C:\path\to\project "检查并修复测试"
```

## 网页工作台

- 左侧项目树：一个工作目录对应一个项目，项目下包含该目录中的全部对话。
- 中间交互区：展示用户消息、模型答复、工具进度和输入框。
- 工作目录：项目可以输入本机绝对目录；不同对话可以绑定不同目录。
- 文件改动：每轮任务结束后只显示一次“本轮改动”，不会为每次工具调用重复生成入口。
- 右侧 Diff：点击改动文件后展开，显示 `+/-` 行数、旧/新行号和颜色高亮。
- 命令审批：联网、安装、删除或未知命令在网页中请求允许或拒绝。
- 停止：取消 Agent 循环，并尝试终止当前本地命令。
- 本机会话：项目、对话和 Diff 追踪保存在 `.coding-agent/sessions.json`，不会进入 Git。

浏览器不能像桌面目录选择器一样直接向后端暴露任意本机路径，因此工作目录使用绝对路径输入。这一限制避免了额外桌面桥接程序，保证产品本身是普通网页。

## 本地工具

| 工具 | 功能 | 关键限制 |
| --- | --- | --- |
| `list_files` | 浏览目录 | 跳过 `.git`，限制结果数 |
| `read_file` | 按行读取 UTF-8 文本 | 限制行数和输出长度 |
| `search_text` | 搜索文本 | 优先 `rg`，提供纯 Python 回退 |
| `write_file` | 创建或覆盖文本文件 | 限制单次内容大小 |
| `replace_text` | 精确替换文本 | 默认要求唯一匹配 |
| `run_command` | 执行工作区命令 | 风险分类、确认、超时和输出截断 |

工具参数由项目自己的轻量验证器依据 JSON Schema 校验。非法 JSON、缺少参数、未知参数和未知工具都会成为结构化 `tool` 错误结果返回模型，让模型有机会修正调用。

## 安全模型

### Web 边界

- 服务只监听 `127.0.0.1`。
- HTTP `Host` 只接受回环主机，写请求拒绝跨站 `Origin`。
- 静态资源使用固定映射，不允许任意路径读取。
- JSON 请求限制大小，所有错误返回不包含凭据或堆栈。
- API 状态只暴露“凭据已配置/未配置”，不返回 API Key。

### 文件边界

每个文件路径都会相对于工作区解析，并使用规范化后的真实路径再次检查。以下访问会被拒绝：

- 使用 `..` 逃逸工作区；
- 指向工作区之外的绝对路径；
- 工作区内指向外部目标的符号链接。

### 命令边界

命令分为三级：

1. `safe`：已识别的只读、测试或构建命令，在默认模式下自动执行。
2. `review`：联网、安装、修改、删除或未识别命令，需要用户确认。
3. `deny`：提权、系统控制、根目录删除、破坏 Git 历史等命令直接拒绝。

命令始终以工作区为当前目录，并有最长运行时间和输出上限。但该分类器只是应用层纵深防护，不是操作系统沙箱。请在容器、虚拟机或低权限账户中运行不受信任模型；最保守时使用 `approval_mode=always`。

## 上下文与终止条件

历史达到配置预算约 80% 时，项目代码会：

1. 按用户消息划分完整轮次，避免拆开 assistant 的工具调用和对应 tool 结果。
2. 保留系统提示与最近两个完整轮次。
3. 通过一次不提供工具的 Chat Completions 请求摘要旧轮次。
4. 摘要失败时插入裁剪说明并保留近期轮次。
5. 必要时进一步截断过长的旧工具输出。

单个任务在以下任一情况终止：模型给出最终文本、达到最大步骤数、连续三次出现相同工具错误、用户停止，或模型 API 返回不可恢复错误。限流、连接失败和超时采用有限次数指数退避；认证和请求参数错误不重试。

## 测试

```powershell
python -m pytest
```

自动化测试使用脚本化假模型，不访问真实 API。覆盖 Agent 循环、工具协议、文件与命令安全、上下文压缩、会话持久化、本机 HTTP 边界、网页状态机、项目/对话管理和 Diff 展示。

## 人工端到端演示

选择一个可丢弃的示例目录，在网页中交给 Agent：

> 阅读项目并找到现有测试方式。为其中一个核心函数添加参数校验和测试，运行测试，根据失败继续修复，最后总结修改和验证结果。

应能观察到模型先列出和读取文件，再写入或精确替换内容，运行测试，并把测试结果反馈给模型继续判断。任务结束后只出现一次改动汇总，点击文件可在右侧审查 Diff。整个过程不依赖服务端代码执行或任何 Agent SDK。

## 已知限制

- 服务只供本机单用户使用，不提供身份认证或远程部署。
- 多个工具调用按模型返回顺序串行执行。
- 只处理 UTF-8 文本文件，不编辑二进制文件。
- 不自动提交 Git、不提供插件系统或自主网络搜索。
- 不同兼容网关对 Chat Completions tool calling 的实现程度可能不同。
