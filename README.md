# Coding Agent：从零实现的本地编程智能体

这是一个个人独立实现的编程智能体。它能与支持 OpenAI Chat Completions tool calling 的模型交互，自主读取和修改工作区文件、执行命令、观察结果并继续迭代，直到完成任务或触发安全终止条件。

项目提供类似 Codex 的双栏桌面界面：左侧管理任务，右侧显示对话、工具执行过程和输入区。对话历史、上下文压缩、工具协议、本地调度、循环控制、安全策略和错误恢复均由本仓库自行实现。

## 依赖边界

本项目严格区分“模型 API 客户端”和“Agent 框架”：

- 使用 PyPI 包 `openai` 中的 `OpenAI` 类，仅调用 `client.chat.completions.create(...)` 发送和接收模型消息。
- 不使用 `openai-agents`、OpenAI Agents SDK、LangChain、LlamaIndex、AutoGen、CrewAI、Claude Agent SDK 或其他 Agent 框架。
- 不使用 Code Interpreter、Files API、File Search、Computer Use 等服务端托管工具。
- 文件访问、命令执行、参数验证、工具结果回传、上下文管理和循环终止全部发生在本机，由本项目代码实现。

因此，`openai` 在这里与普通 HTTP 客户端的角色相同，并不替项目完成 Agent 编排。

## 工作原理

```text
用户任务
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
      tool_call_id 对应的结果消息
          │
          └───────────────────────► 下一轮模型调用
```

核心模块：

- `agent.py`：自主循环、工具回传和终止条件。
- `model.py`：`openai` 基础客户端的薄适配层，不包含 Agent 逻辑。
- `tools.py`：工具 Schema、参数校验和六个本地工具。
- `safety.py`：命令风险分类。
- `context.py`：token 粗略估算、完整轮次摘要和保守裁剪。
- `gui.py`：桌面窗口、任务管理、后台执行、审批弹窗和停止操作。
- `local_settings.py`：被 Git 忽略的本地 GUI 配置。
- `cli.py`：保留的备用终端入口，不是默认产品界面。

## 安装

要求 Python 3.11 或更高版本。建议在虚拟环境中安装：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

macOS 或 Linux 激活虚拟环境时使用：

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

首次启动时会显示“模型连接设置”窗口。连接配置完整并保存后，后续启动会直接进入主界面。API key 可以只在当前会话中使用，也可按需保存到被 Git 忽略的 `.coding-agent/config.json`。

配置也可以通过环境变量预填：

- `CODING_AGENT_API_KEY`：API 凭据，必需。
- `CODING_AGENT_MODEL`：兼容 Chat Completions 和 function calling 的模型名称。
- `CODING_AGENT_BASE_URL`：兼容网关地址，可选；未设置时使用客户端默认地址。
- `CODING_AGENT_CONTEXT_TOKENS`：上下文预算，可选，默认 `32000`。

`.coding-agent/`、`.env` 和虚拟环境均已加入 `.gitignore`。真实凭据不会显示在任务记录中，也不会被提交到仓库。

## 使用

安装后启动桌面应用：

```powershell
coding-agent
```

也可以不依赖脚本入口：

```powershell
python -m coding_agent
```

启动时指定初始工作区：

```powershell
python -m coding_agent --workspace C:\path\to\project
```

桌面界面包含：

- 左侧项目树：一个工作目录对应一个项目，项目下可新建、切换和删除多段独立对话。
- 右侧交互区：用户消息、模型答复以及实时工具执行记录。
- 底部输入框：`Ctrl+Enter` 发送任务。
- 停止按钮：请求中止 Agent 循环，并尝试终止正在运行的本地命令。
- “＋项目”目录选择器：为不同项目自主选择工作目录；重复选择同一路径会定位到已有项目。
- 设置窗口：切换兼容 API、模型、上下文预算和审批模式。
- 命令审批弹窗：执行联网、安装、删除或未知命令前请求确认。
- 项目和对话历史保存在 `.coding-agent/sessions.json`，不会进入 Git；每段对话始终绑定所属项目目录。

备用 CLI 仍可用于自动化和调试：

```powershell
coding-agent-cli --workspace C:\path\to\project "检查并修复测试"
```

## 本地工具

模型可调用以下 function tools：

| 工具 | 功能 | 关键限制 |
| --- | --- | --- |
| `list_files` | 浏览目录 | 跳过 `.git`，限制结果数 |
| `read_file` | 按行读取 UTF-8 文本 | 限制行数和输出长度 |
| `search_text` | 搜索文本 | 优先 `rg`，提供纯 Python 回退 |
| `write_file` | 创建或覆盖文本文件 | 限制单次内容大小 |
| `replace_text` | 精确替换文本 | 默认要求唯一匹配 |
| `run_command` | 执行工作区命令 | 风险分类、确认、超时和输出截断 |

工具参数由项目自己的轻量验证器依据 JSON Schema 校验。非法 JSON、缺少参数、未知参数和未知工具都会成为 `tool` 错误结果返回模型，让模型有机会修正调用。

## 安全模型

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

命令始终以工作区为当前目录，并有最长运行时间和输出上限。但该分类器只是应用层纵深防护，不是操作系统沙箱：shell 语法可以被混淆，启动的进程也可能访问当前账户有权访问的其他资源。请在容器、虚拟机或低权限账户中运行不受信任模型；如需最保守行为，使用 `--approval-mode always`。

## 上下文与终止条件

程序采用与具体模型无关的保守 token 估算。历史达到配置预算约 80% 时：

1. 按用户消息划分完整会话轮次，避免拆开 assistant 的工具调用和对应 tool 结果。
2. 保留系统提示与最近两个完整轮次。
3. 通过一次不提供工具的 Chat Completions 请求摘要旧轮次。
4. 如果摘要失败，插入裁剪说明并保留近期轮次。
5. 必要时进一步截断过长的旧工具输出。

单个任务在以下任一情况终止：模型给出最终文本、达到最大步骤数、连续三次出现相同工具错误、用户按下 Ctrl+C，或模型 API 返回不可恢复错误。限流、连接失败和超时采用有限次数的指数退避；认证和请求参数错误不重试。

## 测试

```powershell
python -m pytest
```

自动化测试使用仓库内的脚本化假模型，不访问任何真实 API。测试覆盖：

- 普通回答、单工具、多工具和工具顺序；
- 非法 JSON、未知工具、失败恢复、重复错误和最大步数；
- 文件读写搜索、UTF-8、替换冲突、输出截断；
- 路径穿越和外部符号链接；
- 命令成功、非零退出、超时、审批与拒绝；
- 上下文完整轮次摘要和摘要失败回退；
- 配置优先级、GUI/CLI 入口与依赖边界。

## 人工端到端演示

选择一个可丢弃的示例项目作为工作区，然后交给 Agent：

> 阅读项目并找到现有测试方式。为其中一个核心函数添加参数校验和测试，运行测试，根据失败继续修复，最后总结修改和验证结果。

演示时应能观察到模型先列出和读取文件，再写入或精确替换内容，运行测试，并把测试结果反馈给模型继续判断。整个过程不依赖服务端代码执行或任何 Agent SDK。

## 已知限制

- 会话仅保存在当前进程内，退出后不会恢复。
- 多个工具调用按模型返回顺序串行执行。
- 只处理 UTF-8 文本文件，不编辑二进制文件。
- 不自动提交 Git、不提供插件系统、网页界面或自主网络搜索。
- 不同兼容网关对 Chat Completions tool calling 的实现程度可能不同。
