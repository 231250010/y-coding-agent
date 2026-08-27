from __future__ import annotations

from coding_agent import gui


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted((_relative_luminance(foreground), _relative_luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_core_text_palette_keeps_readable_contrast() -> None:
    assert _contrast_ratio(gui.TEXT, gui.CANVAS) >= 7
    assert _contrast_ratio(gui.SIDEBAR_TEXT, gui.SIDEBAR) >= 7
    assert _contrast_ratio(gui.MUTED, gui.CANVAS) >= 4.5
    assert _contrast_ratio(gui.TOOL_STATUS, gui.CANVAS) >= 4.5


def test_empty_state_copy_has_a_friendly_coding_assistant_voice() -> None:
    assert gui.APP_NAME == "小码"
    assert gui.ASSISTANT_LABEL == gui.APP_NAME
    assert gui.EMPTY_STATE == (
        "今天想让小码做点什么？",
        "说清目标，剩下的交给我慢慢理顺。",
        (
            "修复失败的测试，并解释原因",
            "读懂这个项目，告诉我从哪里开始",
            "优化当前代码，但不要改变功能",
        ),
    )


def test_composer_stays_compact_at_the_default_window_size() -> None:
    assert gui.COMPOSER_LINES == 3


def test_composer_reserves_space_at_the_bottom_of_small_windows() -> None:
    class PackRecorder:
        def __init__(self) -> None:
            self.options: dict[str, object] = {}

        def pack(self, **options: object) -> None:
            self.options = options

    composer = PackRecorder()
    transcript = object()

    gui._pack_composer(composer, transcript)

    assert composer.options == {"fill": "x", "side": "bottom", "before": transcript}
