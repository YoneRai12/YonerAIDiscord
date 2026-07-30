"""公開YouTube URL検査の狭いtyped domain契約。"""

from __future__ import annotations

from dataclasses import dataclass, field


MEDIA_URL_INSPECTION_CAPABILITY_ID = "cap-run-media-url-inspection"
MEDIA_URL_INSPECTION_MODULE_ID = "media.url-inspection"
MEDIA_URL_INSPECTION_PLUGIN_NAME = "media_inspection"
GEMINI_INTERACTIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MEDIA_INSPECTION_MODEL = "gemini-3.6-flash"
MAX_INSPECTION_INSTRUCTION_CHARS = 4_000
MAX_INSPECTION_OUTPUT_CHARS = 8_000


class MediaInspectionError(RuntimeError):
    """秘密値や入力本文を含めない固定エラー。"""


class MediaInspectionInputError(MediaInspectionError, ValueError):
    """URLまたは指示がtyped契約外。"""


class MediaInspectionUnavailableError(MediaInspectionError):
    """remote providerを安全に完了できなかった。"""


class MediaInspectionResponseError(MediaInspectionError):
    """provider応答がshapeまたはsize契約外。"""


@dataclass(frozen=True, slots=True, repr=False)
class MediaInspectionRequest:
    """reprへURL・指示を出さないprovider入力。"""

    video_uri: str = field(repr=False)
    instruction: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class MediaInspectionResult:
    """reprへmodel出力を出さない検査結果。"""

    text: str = field(repr=False)


__all__ = [
    "GEMINI_INTERACTIONS_ENDPOINT",
    "GEMINI_MEDIA_INSPECTION_MODEL",
    "MAX_INSPECTION_INSTRUCTION_CHARS",
    "MAX_INSPECTION_OUTPUT_CHARS",
    "MEDIA_URL_INSPECTION_CAPABILITY_ID",
    "MEDIA_URL_INSPECTION_MODULE_ID",
    "MEDIA_URL_INSPECTION_PLUGIN_NAME",
    "MediaInspectionError",
    "MediaInspectionInputError",
    "MediaInspectionRequest",
    "MediaInspectionResponseError",
    "MediaInspectionResult",
    "MediaInspectionUnavailableError",
]
