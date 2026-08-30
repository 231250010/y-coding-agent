from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt

from .agent import AgentStopped, CodingAgent
from .config import Config, ConfigError
from .context import ContextManager
from .model import ModelError, OpenAIChatModel
from .safety import RiskLevel
from .tools import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="不依赖 Agent 框架的本地编程智能体",
    )
    parser.add_argument("task", nargs="*", help="要完成的单次编程任务；省略则进入交互模式")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="允许操作的工作区")
    parser.add_argument("--model", help="模型名称，覆盖 CODING_AGENT_MODEL")
    parser.add_argument("--base-url", help="OpenAI 兼容 API 地址，覆盖 CODING_AGENT_BASE_URL")
    parser.add_argument("--max-steps", type=int, default=20, help="每个任务的最大模型调用步数")
    parser.add_argument(
        "--approval-mode",
        choices=("request", "risk", "full", "ask", "always"),
        default="risk",
        help="request 修改文件或使用网络时询问；risk 仅风险操作询问；full 完全访问（兼容旧 ask/always）",
    )
    return parser


class TerminalUI:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def approve(self, command: str, risk: RiskLevel, reason: str) -> bool:
        self.console.print(f"\n[yellow]命令需要确认[/yellow] ({risk.value}): {reason}")
        self.console.print(f"[bold]{command}[/bold]")
        return Confirm.ask("允许执行？", default=False, console=self.console)

    def event(self, name: str, data: dict[str, Any]) -> None:
        if name == "model_start":
            self.console.print(f"[dim]模型思考中（步骤 {data['step']}/{data['max_steps']}）…[/dim]")
        elif name == "tool_start":
            try:
                arguments = json.dumps(json.loads(data["arguments"]), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                arguments = data["arguments"]
            self.console.print(f"[cyan]→ {data['name']}[/cyan] [dim]{arguments}[/dim]")
        elif name == "tool_end":
            status = "[green]成功[/green]" if data["ok"] else "[red]失败[/red]"
            detail = data.get("error") or data.get("output") or ""
            if len(detail) > 500:
                detail = detail[:500] + "…"
            self.console.print(f"  {status} {detail}")
        elif name == "summary_start":
            self.console.print("[dim]正在压缩较早的会话上下文…[/dim]")

    def answer(self, content: str) -> None:
        self.console.print(Markdown(content))


def create_agent(config: Config, ui: TerminalUI) -> CodingAgent:
    model = OpenAIChatModel(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )
    tools = ToolRegistry(
        config.workspace,
        approver=ui.approve,
        approval_mode=config.approval_mode,
    )
    return CodingAgent(
        model,
        tools,
        ContextManager(config.context_tokens),
        max_steps=config.max_steps,
        on_event=ui.event,
    )


def _run_task(agent: CodingAgent, ui: TerminalUI, task: str) -> bool:
    try:
        answer = agent.run(task)
        ui.answer(answer)
        return True
    except KeyboardInterrupt:
        ui.console.print("\n[yellow]任务已由用户取消。[/yellow]")
    except (AgentStopped, ModelError, ValueError) as exc:
        ui.console.print(f"[red]任务停止：{exc}[/red]")
    return False


def _interactive(agent: CodingAgent, config: Config, ui: TerminalUI) -> int:
    ui.console.print("[bold]Coding Agent[/bold] 已启动。输入 /help 查看命令。")
    while True:
        try:
            task = Prompt.ask("\n[bold blue]你[/bold blue]", console=ui.console).strip()
        except (EOFError, KeyboardInterrupt):
            ui.console.print("\n再见。")
            return 0
        if not task:
            continue
        if task == "/exit":
            ui.console.print("再见。")
            return 0
        if task == "/help":
            ui.console.print("/help 查看帮助  /status 查看配置  /clear 清空上下文  /exit 退出")
            continue
        if task == "/clear":
            agent.clear()
            ui.console.print("[green]会话上下文已清空。[/green]")
            continue
        if task == "/status":
            endpoint = config.base_url or "OpenAI 默认地址"
            ui.console.print(
                f"模型: {config.model}\nAPI: {endpoint}\n工作区: {config.workspace}\n"
                f"上下文预算: {config.context_tokens}\n最大步骤: {config.max_steps}\n"
                f"审批模式: {config.approval_mode}"
            )
            continue
        _run_task(agent, ui, task)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = TerminalUI()
    try:
        config = Config.from_values(
            model=args.model,
            base_url=args.base_url,
            workspace=args.workspace,
            max_steps=args.max_steps,
            approval_mode=args.approval_mode,
        )
    except ConfigError as exc:
        ui.console.print(f"[red]配置错误：{exc}[/red]")
        return 2

    agent = create_agent(config, ui)
    task = " ".join(args.task).strip()
    if task:
        return 0 if _run_task(agent, ui, task) else 1
    return _interactive(agent, config, ui)


if __name__ == "__main__":
    sys.exit(main())
