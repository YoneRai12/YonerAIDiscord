"""Discord AI応答のユーザー別表示モード。外部AIを使わず決定する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import time
import unicodedata

from .state_repository import AIStateRepository, StoredDisplayPreference
from .task_routing import AIIntent, AITaskRoute


class DisplayMode(StrEnum):
    AUTO = "auto"
    CARD = "card"
    PLAIN = "plain"


class DisplayPreferenceAction(StrEnum):
    SET = "set"
    SHOW = "show"


@dataclass(frozen=True, slots=True)
class DisplayPreferenceCommand:
    action: DisplayPreferenceAction
    mode: DisplayMode | None = None


_SET_PATTERNS: tuple[tuple[DisplayMode, re.Pattern[str]], ...] = (
    (
        DisplayMode.CARD,
        re.compile(
            r"^(?:カード(?:型(?:の表示)?|表示|形式)?(?:に)?(?:して|切り替えて|変更して|戻して)|"
            r"カードで表示して)[。.!！]?$"
        ),
    ),
    (
        DisplayMode.PLAIN,
        re.compile(
            r"^(?:(?:普通|通常|プレーン|plain)(?:の)?(?:表示|形式)?(?:に)?(?:して|切り替えて|変更して|戻して)|"
            r"(?:普通|通常|プレーン|plain)で表示して)[。.!！]?$",
            re.IGNORECASE,
        ),
    ),
    (
        DisplayMode.AUTO,
        re.compile(
            r"^(?:自動(?:の)?(?:表示|形式)?(?:に)?(?:して|切り替えて|変更して|戻して)|"
            r"自動表示にして|表示(?:設定|モード)を自動に(?:して|戻して))[。.!！]?$"
        ),
    ),
)
_SHOW_PATTERN = re.compile(
    r"^(?:今の)?(?:表示(?:設定|モード)|表示形式)(?:を)?(?:確認して|教えて|見せて|確認)?[。.!！?？]?$"
)
_FENCED_CODE = re.compile(r"```[A-Za-z0-9_+.-]*[ \t]*\r?\n.*?```", re.DOTALL)
_HTML_DOCUMENT = re.compile(r"(?:<!doctype\s+html\b|<html\b)", re.IGNORECASE)


def parse_display_preference_command(prompt: str) -> DisplayPreferenceCommand | None:
    if not isinstance(prompt, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", prompt).strip().split())
    for mode, pattern in _SET_PATTERNS:
        if pattern.fullmatch(normalized):
            return DisplayPreferenceCommand(DisplayPreferenceAction.SET, mode)
    if _SHOW_PATTERN.fullmatch(normalized):
        return DisplayPreferenceCommand(DisplayPreferenceAction.SHOW)
    return None


def effective_display_mode(
    preference: DisplayMode,
    *,
    route: AITaskRoute,
    content: str,
    has_site_result: bool = False,
    has_site_notice: bool = False,
) -> DisplayMode:
    preference = DisplayMode(preference)
    if preference is not DisplayMode.AUTO:
        return preference
    if (
        route.show_progress
        or route.web_search
        or route.uses_tools
        or route.expects_artifact
        or route.intent in {AIIntent.CODE, AIIntent.SITE, AIIntent.MEDIA, AIIntent.SELF_EVOLUTION}
        or has_site_result
        or has_site_notice
        or len(content) > 1_000
        or _FENCED_CODE.search(content)
        or _HTML_DOCUMENT.search(content)
    ):
        return DisplayMode.CARD
    return DisplayMode.PLAIN


class DisplayPreferenceStore:
    def __init__(self, repository: AIStateRepository | None = None) -> None:
        self.repository = repository
        self._memory: dict[int, DisplayMode] = {}

    def get(self, user_id: int) -> DisplayMode:
        normalized_user_id = _user_id(user_id)
        if self.repository is None:
            return self._memory.get(normalized_user_id, DisplayMode.AUTO)
        stored = self.repository.get_display_preference(normalized_user_id)
        return DisplayMode.AUTO if stored is None else DisplayMode(stored.mode)

    def set(self, user_id: int, mode: DisplayMode) -> DisplayMode:
        normalized_user_id = _user_id(user_id)
        normalized_mode = DisplayMode(mode)
        if self.repository is None:
            self._memory[normalized_user_id] = normalized_mode
        else:
            self.repository.upsert_display_preference(
                StoredDisplayPreference(
                    user_id=normalized_user_id,
                    mode=normalized_mode.value,
                    updated_at=time.time(),
                )
            )
        return normalized_mode


def display_mode_label(mode: DisplayMode) -> str:
    return {
        DisplayMode.AUTO: "自動表示",
        DisplayMode.CARD: "カード表示",
        DisplayMode.PLAIN: "普通の表示",
    }[DisplayMode(mode)]


def _user_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("user_id must be a positive integer")
    return value


__all__ = [
    "DisplayMode",
    "DisplayPreferenceAction",
    "DisplayPreferenceCommand",
    "DisplayPreferenceStore",
    "display_mode_label",
    "effective_display_mode",
    "parse_display_preference_command",
]
