from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TypeAlias


MAX_SELECTOR_CHARS = 512
MAX_INPUT_TEXT_CHARS = 8_000
MAX_SELECT_VALUE_CHARS = 1_000
MAX_SINGLE_WAIT_MILLISECONDS = 30_000
MAX_SCROLL_DELTA = 20_000


class BrowserSandboxError(RuntimeError):
    """Browser sandbox contract base error."""


class BrowserSandboxUnavailableError(BrowserSandboxError):
    """No isolated browser worker is configured."""


class BrowserPolicyError(BrowserSandboxError):
    """A request was rejected before it reached the browser worker."""


class BrowserResourceLimitError(BrowserSandboxError):
    """A bounded browser session exceeded its resource budget."""


class BrowserAdapterContractError(BrowserSandboxError):
    """An isolated browser adapter returned data outside the contract."""


@dataclass(frozen=True, slots=True)
class CssSelector:
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("selector must be a string")
        if not self.value or self.value != self.value.strip():
            raise ValueError("selector must not be blank or padded")
        if len(self.value) > MAX_SELECTOR_CHARS:
            raise ValueError("selector is too long")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.value):
            raise ValueError("selector contains a control character")


@dataclass(frozen=True, slots=True)
class Navigate:
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise TypeError("url must be a string")


@dataclass(frozen=True, slots=True)
class Click:
    selector: CssSelector

    def __post_init__(self) -> None:
        if not isinstance(self.selector, CssSelector):
            raise TypeError("selector must be a CssSelector")


@dataclass(frozen=True, slots=True)
class TypeText:
    selector: CssSelector
    text: str = field(repr=False)
    clear_first: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.selector, CssSelector):
            raise TypeError("selector must be a CssSelector")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if len(self.text) > MAX_INPUT_TEXT_CHARS:
            raise ValueError("input text is too long")
        if type(self.clear_first) is not bool:
            raise TypeError("clear_first must be a boolean")


@dataclass(frozen=True, slots=True)
class SelectOption:
    selector: CssSelector
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selector, CssSelector):
            raise TypeError("selector must be a CssSelector")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("select value must not be empty")
        if len(self.value) > MAX_SELECT_VALUE_CHARS:
            raise ValueError("select value is too long")


@dataclass(frozen=True, slots=True)
class Scroll:
    delta_x: int = 0
    delta_y: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.delta_x, bool)
            or isinstance(self.delta_y, bool)
            or not isinstance(self.delta_x, int)
            or not isinstance(self.delta_y, int)
        ):
            raise TypeError("scroll deltas must be integers")
        if self.delta_x == 0 and self.delta_y == 0:
            raise ValueError("at least one scroll delta must be non-zero")
        if abs(self.delta_x) > MAX_SCROLL_DELTA or abs(self.delta_y) > MAX_SCROLL_DELTA:
            raise ValueError("scroll delta is outside the allowed range")


@dataclass(frozen=True, slots=True)
class Wait:
    milliseconds: int

    def __post_init__(self) -> None:
        if isinstance(self.milliseconds, bool) or not isinstance(self.milliseconds, int):
            raise TypeError("wait milliseconds must be an integer")
        if not 1 <= self.milliseconds <= MAX_SINGLE_WAIT_MILLISECONDS:
            raise ValueError("wait milliseconds is outside the allowed range")


class ScreenshotFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"


@dataclass(frozen=True, slots=True)
class Screenshot:
    full_page: bool = False
    image_format: ScreenshotFormat = ScreenshotFormat.PNG

    def __post_init__(self) -> None:
        if type(self.full_page) is not bool:
            raise TypeError("full_page must be a boolean")
        if not isinstance(self.image_format, ScreenshotFormat):
            raise TypeError("image_format must be a ScreenshotFormat")


@dataclass(frozen=True, slots=True)
class ExtractText:
    selector: CssSelector | None = None

    def __post_init__(self) -> None:
        if self.selector is not None and not isinstance(self.selector, CssSelector):
            raise TypeError("selector must be a CssSelector or None")


BrowserAction: TypeAlias = Navigate | Click | TypeText | SelectOption | Scroll | Wait | Screenshot | ExtractText

ALLOWED_BROWSER_ACTION_TYPES = (
    Navigate,
    Click,
    TypeText,
    SelectOption,
    Scroll,
    Wait,
    Screenshot,
    ExtractText,
)


@dataclass(frozen=True, slots=True)
class BrowserSessionRequest:
    actions: tuple[BrowserAction, ...]

    def __post_init__(self) -> None:
        if isinstance(self.actions, (str, bytes, bytearray)):
            raise TypeError("actions must contain typed browser actions")
        try:
            actions = tuple(self.actions)
        except TypeError as exc:
            raise TypeError("actions must be iterable") from exc
        if not actions:
            raise ValueError("at least one browser action is required")
        if any(type(action) not in ALLOWED_BROWSER_ACTION_TYPES for action in actions):
            raise TypeError("session contains an unsupported browser action")
        object.__setattr__(self, "actions", actions)


class BrowserOutputKind(StrEnum):
    SCREENSHOT = "screenshot"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class BrowserOutput:
    step_index: int
    kind: BrowserOutputKind
    data: bytes = field(repr=False)
    media_type: str

    def __post_init__(self) -> None:
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int) or self.step_index < 0:
            raise ValueError("step_index must be a non-negative integer")
        if not isinstance(self.kind, BrowserOutputKind):
            raise TypeError("kind must be a BrowserOutputKind")
        if type(self.data) is not bytes:
            raise TypeError("output data must be bytes")
        if not self.data:
            raise ValueError("output data must not be empty")
        expected_media_types = {
            BrowserOutputKind.SCREENSHOT: {"image/png", "image/jpeg"},
            BrowserOutputKind.TEXT: {"text/plain; charset=utf-8"},
        }
        if self.media_type not in expected_media_types[self.kind]:
            raise ValueError("output media type does not match its kind")
        if self.kind is BrowserOutputKind.TEXT:
            try:
                self.data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError("text output must be valid UTF-8") from exc

    @property
    def byte_length(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class BrowserSessionResult:
    outputs: tuple[BrowserOutput, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.outputs, (str, bytes, bytearray)):
            raise TypeError("outputs must contain BrowserOutput values")
        try:
            outputs = tuple(self.outputs)
        except TypeError as exc:
            raise TypeError("outputs must be iterable") from exc
        if any(not isinstance(output, BrowserOutput) for output in outputs):
            raise TypeError("outputs must contain only BrowserOutput values")
        object.__setattr__(self, "outputs", outputs)

    @property
    def byte_length(self) -> int:
        return sum(output.byte_length for output in self.outputs)


@dataclass(frozen=True, slots=True)
class BrowserIsolationContract:
    """Fixed worker constraints; callers cannot request a weaker mode."""

    @property
    def profile_mode(self) -> Literal["ephemeral"]:
        return "ephemeral"

    @property
    def downloads_enabled(self) -> Literal[False]:
        return False

    @property
    def uploads_enabled(self) -> Literal[False]:
        return False

    @property
    def script_evaluation_enabled(self) -> Literal[False]:
        return False

    @property
    def developer_protocol_enabled(self) -> Literal[False]:
        return False

    @property
    def context_reuse_enabled(self) -> Literal[False]:
        return False


BROWSER_ISOLATION_CONTRACT = BrowserIsolationContract()
