from __future__ import annotations

import re
from html.parser import HTMLParser
from importlib.resources import files


class StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: set[str] = set()
        self.labels: list[dict[str, str | None]] = []
        self.scripts: list[dict[str, str | None]] = []
        self.styles = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.tags.append(tag)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "label":
            self.labels.append(values)
        if tag == "script":
            self.scripts.append(values)
        if tag == "style":
            self.styles += 1


def asset(name: str) -> str:
    return files("coding_agent").joinpath("web_assets", name).read_text(encoding="utf-8")


def test_page_has_navigation_conversation_diff_and_native_dialog_landmarks() -> None:
    parser = StructureParser()
    parser.feed(asset("index.html"))

    assert {"nav", "main", "aside", "dialog", "form"}.issubset(parser.tags)
    assert {
        "app",
        "project-list",
        "task-list",
        "transcript",
        "composer-form",
        "diff-panel",
        "approval-dialog",
        "settings-dialog",
    }.issubset(parser.ids)
    assert parser.styles == 0
    assert parser.scripts == [{"src": "/assets/app.js", "defer": None}]


def test_every_form_control_has_an_accessible_label() -> None:
    parser = StructureParser()
    parser.feed(asset("index.html"))

    labelled_controls = {str(label["for"]) for label in parser.labels if label.get("for")}
    assert {"message-input", "workspace-path", "setting-model", "setting-base-url", "setting-api-key"}.issubset(
        labelled_controls
    )


def test_assets_contain_no_remote_urls_or_secret_values() -> None:
    combined = "\n".join(asset(name) for name in ("index.html", "app.css", "app.js"))

    assert "https://" not in combined
    assert "http://" not in combined
    assert re.search(r"\bsk-[A-Za-z0-9_-]{12,}", combined) is None
