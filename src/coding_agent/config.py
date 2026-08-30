from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .permissions import PERMISSION_MODES, normalize_permission_mode


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _positive_int(value: str | int, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return parsed


@dataclass(frozen=True, slots=True)
class Config:
    api_key: str
    model: str
    base_url: str | None
    workspace: Path
    context_tokens: int = 32_000
    max_steps: int = 20
    approval_mode: str = "risk"
    request_timeout: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_values(
        cls,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        workspace: str | Path | None = None,
        context_tokens: int | str | None = None,
        max_steps: int | str | None = None,
        approval_mode: str = "risk",
    ) -> "Config":
        resolved_key = api_key or os.getenv("CODING_AGENT_API_KEY", "")
        resolved_model = model or os.getenv("CODING_AGENT_MODEL", "")
        resolved_base_url = base_url or os.getenv("CODING_AGENT_BASE_URL") or None
        resolved_context = context_tokens or os.getenv("CODING_AGENT_CONTEXT_TOKENS", "32000")

        if not resolved_key.strip():
            raise ConfigError("缺少 CODING_AGENT_API_KEY 环境变量")
        if not resolved_model.strip():
            raise ConfigError("缺少模型名称，请设置 CODING_AGENT_MODEL 或使用 --model")
        normalized_mode = normalize_permission_mode(approval_mode, default="")
        if normalized_mode not in PERMISSION_MODES:
            raise ConfigError("approval_mode 只能是 request、risk 或 full")

        root = Path(workspace or Path.cwd()).expanduser().resolve()
        if not root.is_dir():
            raise ConfigError(f"工作区不存在或不是目录: {root}")

        return cls(
            api_key=resolved_key,
            model=resolved_model,
            base_url=resolved_base_url,
            workspace=root,
            context_tokens=_positive_int(resolved_context, "context_tokens"),
            max_steps=_positive_int(max_steps or 20, "max_steps"),
            approval_mode=normalized_mode,
        )
