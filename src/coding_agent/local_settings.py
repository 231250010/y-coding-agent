from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permissions import normalize_permission_mode


def _integer(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass(slots=True)
class LocalSettings:
    api_key: str = ""
    model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    workspace: str = ""
    context_tokens: int = 32_000
    max_steps: int = 20
    approval_mode: str = "risk"
    remember_key: bool = False

    @property
    def is_complete(self) -> bool:
        return bool(self.api_key.strip() and self.model.strip() and self.base_url.strip())

    @classmethod
    def load(cls, root: Path) -> "LocalSettings":
        path = cls.path(root)
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        return cls(
            api_key=os.getenv("CODING_AGENT_API_KEY") or str(data.get("api_key", "")),
            model=os.getenv("CODING_AGENT_MODEL") or str(data.get("model", "deepseek-v4-pro")),
            base_url=os.getenv("CODING_AGENT_BASE_URL") or str(data.get("base_url", "https://api.deepseek.com")),
            workspace=str(data.get("workspace") or root),
            context_tokens=_integer(os.getenv("CODING_AGENT_CONTEXT_TOKENS") or data.get("context_tokens"), 32_000),
            max_steps=_integer(data.get("max_steps"), 20),
            approval_mode=normalize_permission_mode(data.get("approval_mode", "risk")),
            remember_key=bool(data.get("api_key")),
        )

    def save(self, root: Path) -> None:
        path = self.path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self.model,
            "base_url": self.base_url,
            "workspace": self.workspace,
            "context_tokens": self.context_tokens,
            "max_steps": self.max_steps,
            "approval_mode": normalize_permission_mode(self.approval_mode),
        }
        if self.remember_key:
            payload["api_key"] = self.api_key
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def path(root: Path) -> Path:
        return root / ".coding-agent" / "config.json"
