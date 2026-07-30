"""Discord transportへ渡す前のscope-bound media artifact準備だけを担う。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .artifacts import (
    MARKDOWN_MEDIA_TYPE,
    MediaArtifactStore,
    PNG_MEDIA_TYPE,
    validate_canonical_markdown,
    validate_canonical_png,
)
from .domain import (
    ArtifactKind,
    ArtifactScope,
    MAX_MARKDOWN_BYTES,
    MAX_PNG_BYTES,
    MediaAuthorizationError,
    MediaIntegrityError,
    MediaValidationError,
)

if TYPE_CHECKING:
    from yonerai_discord.modules.ai.orchestration import PlanArtifactOutput


MAX_PREPARED_ATTACHMENTS = 4
MAX_PREPARED_MEDIA_BYTES = 24 * 1024 * 1024
AuthorizationCurrent = Callable[[], bool]
StoreCurrent = Callable[[], MediaArtifactStore | None]
_FILENAME = re.compile(r"media-[0-9]{2}\.(?:png|md)\Z")


class MediaDeliveryError(RuntimeError):
    """配送準備を安全に完了できなかった。"""


@dataclass(frozen=True, slots=True)
class PreparedMediaAttachment:
    """transport非依存でDiscord attachmentへ変換できるcanonical PNG。"""

    filename: str
    data: bytes = field(repr=False)
    media_type: str
    kind: ArtifactKind
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str) or not _FILENAME.fullmatch(self.filename):
            raise ValueError("prepared media filename is outside the code-owned contract")
        if not isinstance(self.kind, ArtifactKind):
            raise ValueError("prepared media type is invalid")
        if self.kind is ArtifactKind.DOCUMENT:
            if not self.filename.endswith(".md") or self.media_type != MARKDOWN_MEDIA_TYPE:
                raise ValueError("prepared media type is invalid")
            if not isinstance(self.data, bytes) or not 1 <= len(self.data) <= MAX_MARKDOWN_BYTES:
                raise ValueError("prepared media data is outside the Markdown limit")
            try:
                validate_canonical_markdown(self.data)
            except (MediaIntegrityError, MediaValidationError):
                raise ValueError("prepared media data is not canonical") from None
            if self.width != 0 or self.height != 0:
                raise ValueError("prepared media dimensions do not match content")
        else:
            if not self.filename.endswith(".png") or self.media_type != PNG_MEDIA_TYPE:
                raise ValueError("prepared media type is invalid")
            if not isinstance(self.data, bytes) or not 1 <= len(self.data) <= MAX_PNG_BYTES:
                raise ValueError("prepared media data is outside the PNG limit")
            try:
                canonical = validate_canonical_png(self.data)
            except (MediaIntegrityError, MediaValidationError):
                raise ValueError("prepared media data is not canonical") from None
            if canonical.width != self.width or canonical.height != self.height:
                raise ValueError("prepared media dimensions do not match content")


class MediaArtifactDeliveryPreparer:
    """opaque refを現在scopeで読んで、transportに安全なbytesだけへ投影する。"""

    def __init__(self, store: MediaArtifactStore, *, store_current: StoreCurrent) -> None:
        if not isinstance(store, MediaArtifactStore) or not callable(store_current):
            raise TypeError("delivery preparer requires a MediaArtifactStore and identity callback")
        self._store = store
        self._store_current = store_current

    def prepare(
        self,
        outputs: tuple[PlanArtifactOutput, ...],
        *,
        scope: ArtifactScope,
        authorization_current: AuthorizationCurrent,
    ) -> tuple[PreparedMediaAttachment, ...]:
        # Importing the AI package while this neutral media module is being
        # initialized creates a cycle through core_artifact_delivery.  Keep the
        # runtime identity check exact, but resolve it only at the call boundary.
        from yonerai_discord.modules.ai.orchestration import PlanArtifactOutput

        if not isinstance(outputs, tuple) or not 1 <= len(outputs) <= MAX_PREPARED_ATTACHMENTS:
            raise MediaDeliveryError("media delivery output count is unavailable")
        if not isinstance(scope, ArtifactScope) or not callable(authorization_current):
            raise MediaDeliveryError("media delivery context is unavailable")
        if any(not isinstance(output, PlanArtifactOutput) for output in outputs):
            raise MediaDeliveryError("media delivery output is unavailable")

        refs = tuple(output.artifact for output in outputs)
        if len({ref.artifact_id for ref in refs}) != len(refs):
            raise MediaDeliveryError("media delivery output is unavailable")
        if any(ref.scope_digest != scope.digest for ref in refs):
            raise MediaDeliveryError("media delivery scope changed")

        prepared: list[PreparedMediaAttachment] = []
        total_bytes = 0
        for index, ref in enumerate(refs, start=1):
            self._require_current(authorization_current)
            try:
                data = (
                    self._store.read_markdown(ref, scope=scope)
                    if ref.kind is ArtifactKind.DOCUMENT
                    else self._store.read_png(ref, scope=scope)
                )
            except (MediaAuthorizationError, MediaIntegrityError, MediaValidationError, OSError, ValueError):
                raise MediaDeliveryError("media delivery artifact is unavailable") from None
            self._require_current(authorization_current)
            self._validate_read(ref, data)
            total_bytes += len(data)
            if total_bytes > MAX_PREPARED_MEDIA_BYTES:
                raise MediaDeliveryError("media delivery total is unavailable")
            prepared.append(
                PreparedMediaAttachment(
                    filename=f"media-{index:02d}{_attachment_suffix(ref.kind)}",
                    data=data,
                    media_type=_attachment_media_type(ref.kind),
                    kind=ref.kind,
                    width=ref.width,
                    height=ref.height,
                )
            )

        self._require_current(authorization_current)
        return tuple(prepared)

    def _require_current(self, authorization_current: AuthorizationCurrent) -> None:
        try:
            store_is_current = self._store_current() is self._store
            authorized = authorization_current()
        except Exception:
            raise MediaDeliveryError("media delivery authorization changed") from None
        if store_is_current is not True or authorized is not True:
            raise MediaDeliveryError("media delivery authorization changed")

    @staticmethod
    def _validate_read(ref: object, data: object) -> None:
        try:
            if not isinstance(data, bytes):
                raise MediaIntegrityError("artifact bytes are unavailable")
            if len(data) != ref.byte_size or hashlib.sha256(data).hexdigest() != ref.content_digest:
                raise MediaIntegrityError("artifact content changed")
            if ref.kind is ArtifactKind.DOCUMENT:
                canonical = validate_canonical_markdown(data)
                if canonical.data != data or ref.width != 0 or ref.height != 0:
                    raise MediaIntegrityError("artifact content changed")
            else:
                canonical = validate_canonical_png(data)
                if canonical.data != data or canonical.width != ref.width or canonical.height != ref.height:
                    raise MediaIntegrityError("artifact content changed")
        except (MediaIntegrityError, MediaValidationError, AttributeError, TypeError, ValueError):
            raise MediaDeliveryError("media delivery artifact is unavailable") from None


def _attachment_suffix(kind: ArtifactKind) -> str:
    return ".md" if kind is ArtifactKind.DOCUMENT else ".png"


def _attachment_media_type(kind: ArtifactKind) -> str:
    return MARKDOWN_MEDIA_TYPE if kind is ArtifactKind.DOCUMENT else PNG_MEDIA_TYPE


__all__ = [
    "MAX_PREPARED_ATTACHMENTS",
    "MAX_PREPARED_MEDIA_BYTES",
    "MediaArtifactDeliveryPreparer",
    "MediaDeliveryError",
    "PreparedMediaAttachment",
]
