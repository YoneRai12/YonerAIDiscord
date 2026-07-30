"""Thin production adapters for the sealed Search Fabric autonomy task.

The adapters in this module only project already-existing runtime services to
the ports declared by :mod:`yonerai_discord.search_fabric.autonomy`.  They do
not create a second planner, browser, artifact store, or readiness registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from yonerai_discord.browser_sandbox.models import BrowserOutputKind
from yonerai_discord.browser_sandbox.service import BrowserSandboxService
from yonerai_discord.db import Database
from yonerai_discord.modules.media_pipeline.artifacts import (
    CanonicalPng,
    MediaArtifactStore,
    canonicalize_image,
    canonicalize_markdown,
    validate_canonical_markdown,
    validate_canonical_png,
)
from yonerai_discord.modules.media_pipeline.domain import (
    ArtifactKind as MediaArtifactKind,
    ArtifactRef as MediaArtifactRef,
    ArtifactScope as MediaArtifactScope,
    MAX_PNG_BYTES,
    MediaPipelineError,
)
from yonerai_discord.provider_registry.domain import ArtifactKind, ArtifactRef
from PIL import Image, UnidentifiedImageError

from .autonomy import (
    AutonomyAuthorizationError,
    AutonomyBinding,
    BoundedAutonomyService,
    AutonomyContractError,
    BrowserResearchRequest,
    BrowserResearchResult,
    ComparisonTableRequest,
    OfficialPageInspection,
    ScopedAutonomyArtifact,
)
from .composition import LocalSearchFabricRuntime
from .contracts import SearchFetchState, SearchIntent, SearchResultV1, SearchSourceClass
from .orchestrator import SearchOrchestratorOutcome, SearchVerificationState
from .receipts import SearchReceiptV1
from .autonomy_persistence import SqliteAutonomyJournal


_MAX_BROWSER_TEXT_CHARS = 12_000
_MAX_COMPARISON_EXCERPT_CHARS = 1_000
_MAX_COMPARISON_BYTES = 64 * 1024
_MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")

AuthorizationCurrent = Callable[[], Awaitable[bool]]
IdentityCurrent = Callable[[], object | None]
ClosingCurrent = Callable[[], bool]


class AutonomyArtifactWriterPort(Protocol):
    """Persist one bounded artifact without exposing a path or raw handle."""

    async def write(
        self,
        data: bytes,
        *,
        binding: AutonomyBinding,
        plan_digest: str,
        kind: ArtifactKind,
        media_type: str,
    ) -> ArtifactRef: ...


@dataclass(frozen=True, slots=True)
class ComposedAutonomyRuntime:
    """One thin composition over the existing Search, Browser, Media and DB identities."""

    service: BoundedAutonomyService = field(repr=False)
    artifacts: MediaPipelineAutonomyArtifactWriter = field(repr=False)
    _ready_current: Callable[[], bool] = field(repr=False)

    @property
    def ready(self) -> bool:
        try:
            return self._ready_current() is True
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class MediaPipelineAutonomyArtifactWriter:
    """Persist autonomy artifacts in the existing scope-bound Media Pipeline."""

    store: MediaArtifactStore = field(repr=False)
    store_current: IdentityCurrent = field(repr=False)
    closing_current: ClosingCurrent = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.store, MediaArtifactStore):
            raise TypeError("store must be MediaArtifactStore")
        _require_callable(self.store_current, "store_current")
        _require_callable(self.closing_current, "closing_current")

    @property
    def ready(self) -> bool:
        return _identity_is_current(self.store, self.store_current, self.closing_current)

    async def write(
        self,
        data: bytes,
        *,
        binding: AutonomyBinding,
        plan_digest: str,
        kind: ArtifactKind,
        media_type: str,
    ) -> ArtifactRef:
        if type(data) is not bytes or not data:
            raise AutonomyContractError("artifact payload is invalid")
        if not isinstance(binding, AutonomyBinding):
            raise TypeError("binding must be AutonomyBinding")
        if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
            raise ValueError("plan_digest is invalid")
        _require_identity_current(self.store, self.store_current, self.closing_current)
        scope = MediaArtifactScope(
            request_id=binding.task_id,
            guild_id=binding.guild_id,
            channel_id=binding.channel_id,
            user_id=binding.user_id,
        )
        try:
            reference = await asyncio.to_thread(
                self._commit,
                data,
                scope=scope,
                plan_digest=plan_digest,
                kind=kind,
                media_type=media_type,
            )
        except asyncio.CancelledError:
            raise
        except (MediaPipelineError, OSError, UnidentifiedImageError, UnicodeError, ValueError):
            raise AutonomyContractError("artifact persistence failed") from None
        _require_identity_current(self.store, self.store_current, self.closing_current)
        return reference

    async def resolve(
        self,
        value: ScopedAutonomyArtifact,
        *,
        binding: AutonomyBinding,
        plan_digest: str,
    ) -> MediaArtifactRef:
        """Recover the exact internal ref needed by durable Discord delivery."""

        if not isinstance(value, ScopedAutonomyArtifact):
            raise TypeError("value must be ScopedAutonomyArtifact")
        if not isinstance(binding, AutonomyBinding):
            raise TypeError("binding must be AutonomyBinding")
        if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
            raise ValueError("plan_digest is invalid")
        value.require_current(binding, plan_digest)
        provider_ref = value.artifact
        if provider_ref.kind is ArtifactKind.SCREENSHOT and provider_ref.media_type == "image/png":
            media_kind = MediaArtifactKind.IMAGE
        elif provider_ref.kind is ArtifactKind.DOCUMENT and provider_ref.media_type == _MARKDOWN_MEDIA_TYPE:
            media_kind = MediaArtifactKind.DOCUMENT
        else:
            raise AutonomyContractError("artifact kind is outside the autonomy delivery contract")
        if provider_ref.size_bytes is None or provider_ref.sha256 is None:
            raise AutonomyContractError("artifact reference is incomplete")
        _require_identity_current(self.store, self.store_current, self.closing_current)
        scope = MediaArtifactScope(
            request_id=binding.task_id,
            guild_id=binding.guild_id,
            channel_id=binding.channel_id,
            user_id=binding.user_id,
        )
        try:
            resolved = await asyncio.to_thread(
                self.store.resolve_reference,
                provider_ref.artifact_id,
                scope=scope,
                recipe_digest=plan_digest,
                content_digest=provider_ref.sha256,
                kind=media_kind,
                byte_size=provider_ref.size_bytes,
            )
        except asyncio.CancelledError:
            raise
        except (MediaPipelineError, OSError, ValueError):
            raise AutonomyContractError("artifact resolution failed") from None
        _require_identity_current(self.store, self.store_current, self.closing_current)
        return resolved

    def _commit(
        self,
        data: bytes,
        *,
        scope: MediaArtifactScope,
        plan_digest: str,
        kind: ArtifactKind,
        media_type: str,
    ) -> ArtifactRef:
        def commit_check() -> bool:
            return _identity_is_current(self.store, self.store_current, self.closing_current)

        if kind is ArtifactKind.SCREENSHOT and media_type == "image/png":
            canonical = validate_canonical_png(data)
            with Image.open(io.BytesIO(canonical.data)) as opened:
                opened.load()
                stored = self.store.commit_image(
                    opened,
                    scope=scope,
                    recipe_digest=plan_digest,
                    kind=MediaArtifactKind.IMAGE,
                    commit_check=commit_check,
                )
            expected_data = canonical.data
        elif kind is ArtifactKind.DOCUMENT and media_type == _MARKDOWN_MEDIA_TYPE:
            canonical = validate_canonical_markdown(data)
            text = canonical.data.decode("utf-8", errors="strict")
            stored = self.store.commit_markdown(
                text,
                scope=scope,
                recipe_digest=plan_digest,
                commit_check=commit_check,
            )
            expected_data = canonical.data
        else:
            raise AutonomyContractError("artifact kind is outside the autonomy delivery contract")
        if stored.content_digest != hashlib.sha256(expected_data).hexdigest() or stored.byte_size != len(expected_data):
            raise AutonomyContractError("artifact persistence returned an invalid reference")
        return ArtifactRef(
            artifact_id=stored.artifact_id,
            kind=kind,
            media_type=media_type,
            size_bytes=stored.byte_size,
            sha256=stored.content_digest,
        )


@dataclass(frozen=True, slots=True)
class LocalSearchAutonomyAdapter:
    runtime: LocalSearchFabricRuntime = field(repr=False)
    runtime_current: IdentityCurrent = field(repr=False)
    closing_current: ClosingCurrent = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, LocalSearchFabricRuntime):
            raise TypeError("runtime must be LocalSearchFabricRuntime")
        _require_callable(self.runtime_current, "runtime_current")
        _require_callable(self.closing_current, "closing_current")

    @property
    def ready(self) -> bool:
        return self.runtime.ready and _identity_is_current(
            self.runtime,
            self.runtime_current,
            self.closing_current,
        )

    async def search(
        self,
        query: str,
        *,
        request_id: str,
        intent: SearchIntent,
        language: str,
        high_stakes: bool,
        authorization_current: AuthorizationCurrent,
    ) -> SearchOrchestratorOutcome:
        await _require_authorized(authorization_current)
        _require_identity_current(self.runtime, self.runtime_current, self.closing_current)
        if not self.runtime.ready:
            raise AutonomyContractError("Search Fabric runtime is not ready")
        outcome = await self.runtime.search(
            query,
            request_id=request_id,
            intent=intent,
            language=language,
            high_stakes=high_stakes,
            authorization_current=authorization_current,
        )
        await _require_authorized(authorization_current)
        _require_identity_current(self.runtime, self.runtime_current, self.closing_current)
        return _official_fetched_projection(outcome)


@dataclass(frozen=True, slots=True)
class IsolatedBrowserAutonomyAdapter:
    service: BrowserSandboxService = field(repr=False)
    writer: AutonomyArtifactWriterPort = field(repr=False)
    service_current: IdentityCurrent = field(repr=False)
    writer_current: IdentityCurrent = field(repr=False)
    closing_current: ClosingCurrent = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.service, BrowserSandboxService):
            raise TypeError("service must be BrowserSandboxService")
        if not callable(getattr(self.writer, "write", None)):
            raise TypeError("writer must implement write()")
        for label, value in (
            ("service_current", self.service_current),
            ("writer_current", self.writer_current),
            ("closing_current", self.closing_current),
        ):
            _require_callable(value, label)

    @property
    def ready(self) -> bool:
        return (
            self.service.configured
            and _identity_is_current(self.service, self.service_current, self.closing_current)
            and _identity_is_current(self.writer, self.writer_current, self.closing_current)
        )

    async def inspect(
        self,
        request: BrowserResearchRequest,
        *,
        authorization_current: AuthorizationCurrent,
    ) -> BrowserResearchResult:
        if not isinstance(request, BrowserResearchRequest):
            raise TypeError("request must be BrowserResearchRequest")
        await _require_authorized(authorization_current)
        self._require_current()
        if not self.service.configured:
            raise AutonomyContractError("isolated browser runtime is not ready")

        pages: list[OfficialPageInspection] = []
        for index, (url, session) in enumerate(zip(request.urls, request.sessions, strict=True), start=1):
            await _require_authorized(authorization_current)
            self._require_current()
            result = await self.service.execute(session)
            await _require_authorized(authorization_current)
            self._require_current()
            text, screenshot = _browser_outputs(result.outputs)
            canonical = _canonical_browser_png(screenshot)
            await _require_authorized(authorization_current)
            self._require_current()
            reference = await self.writer.write(
                canonical.data,
                binding=request.binding,
                plan_digest=_plan_digest(request),
                kind=ArtifactKind.SCREENSHOT,
                media_type="image/png",
            )
            await _require_authorized(authorization_current)
            self._require_current()
            _require_written_artifact(
                reference,
                data=canonical.data,
                kind=ArtifactKind.SCREENSHOT,
                media_type="image/png",
            )
            pages.append(
                OfficialPageInspection(
                    url=url,
                    title=f"SearXNG official documentation {index}",
                    text=text,
                    screenshot=ScopedAutonomyArtifact.bind(
                        reference,
                        binding=request.binding,
                        plan_digest=_plan_digest(request),
                    ),
                )
            )
        await _require_authorized(authorization_current)
        self._require_current()
        return BrowserResearchResult(tuple(pages))

    def _require_current(self) -> None:
        _require_identity_current(self.service, self.service_current, self.closing_current)
        _require_identity_current(self.writer, self.writer_current, self.closing_current)


@dataclass(frozen=True, slots=True)
class MarkdownComparisonArtifactAdapter:
    writer: AutonomyArtifactWriterPort = field(repr=False)
    writer_current: IdentityCurrent = field(repr=False)
    closing_current: ClosingCurrent = field(repr=False)

    def __post_init__(self) -> None:
        if not callable(getattr(self.writer, "write", None)):
            raise TypeError("writer must implement write()")
        _require_callable(self.writer_current, "writer_current")
        _require_callable(self.closing_current, "closing_current")

    @property
    def ready(self) -> bool:
        return _identity_is_current(self.writer, self.writer_current, self.closing_current)

    async def create(
        self,
        request: ComparisonTableRequest,
        *,
        authorization_current: AuthorizationCurrent,
    ) -> ScopedAutonomyArtifact:
        if not isinstance(request, ComparisonTableRequest):
            raise TypeError("request must be ComparisonTableRequest")
        await _require_authorized(authorization_current)
        _require_identity_current(self.writer, self.writer_current, self.closing_current)
        body = canonicalize_markdown(_comparison_markdown(request).decode("utf-8")).data
        await _require_authorized(authorization_current)
        _require_identity_current(self.writer, self.writer_current, self.closing_current)
        reference = await self.writer.write(
            body,
            binding=request.binding,
            plan_digest=_plan_digest(request),
            kind=ArtifactKind.DOCUMENT,
            media_type=_MARKDOWN_MEDIA_TYPE,
        )
        await _require_authorized(authorization_current)
        _require_identity_current(self.writer, self.writer_current, self.closing_current)
        _require_written_artifact(
            reference,
            data=body,
            kind=ArtifactKind.DOCUMENT,
            media_type=_MARKDOWN_MEDIA_TYPE,
        )
        return ScopedAutonomyArtifact.bind(
            reference,
            binding=request.binding,
            plan_digest=_plan_digest(request),
        )


def compose_bounded_autonomy_runtime(
    *,
    search_runtime: LocalSearchFabricRuntime,
    browser_service: BrowserSandboxService,
    media_store: MediaArtifactStore,
    database: Database,
    search_runtime_current: IdentityCurrent,
    browser_service_current: IdentityCurrent,
    media_store_current: IdentityCurrent,
    database_current: IdentityCurrent,
    closing_current: ClosingCurrent,
) -> ComposedAutonomyRuntime | None:
    """Compose the sealed task only when every existing runtime is current.

    This function deliberately does not construct a browser worker.  A missing
    trusted ``BrowserSandboxService`` adapter remains an unavailable runtime,
    rather than silently falling back to a screenshot or host-browser path.
    """

    if not isinstance(search_runtime, LocalSearchFabricRuntime):
        raise TypeError("search_runtime must be LocalSearchFabricRuntime")
    if not isinstance(browser_service, BrowserSandboxService):
        raise TypeError("browser_service must be BrowserSandboxService")
    if not isinstance(media_store, MediaArtifactStore):
        raise TypeError("media_store must be MediaArtifactStore")
    if not isinstance(database, Database):
        raise TypeError("database must be Database")
    for label, value in (
        ("search_runtime_current", search_runtime_current),
        ("browser_service_current", browser_service_current),
        ("media_store_current", media_store_current),
        ("database_current", database_current),
        ("closing_current", closing_current),
    ):
        _require_callable(value, label)
    if (
        not search_runtime.ready
        or not browser_service.configured
        or not database.is_open
        or not _identity_is_current(search_runtime, search_runtime_current, closing_current)
        or not _identity_is_current(browser_service, browser_service_current, closing_current)
        or not _identity_is_current(media_store, media_store_current, closing_current)
        or not _identity_is_current(database, database_current, closing_current)
    ):
        return None

    writer = MediaPipelineAutonomyArtifactWriter(
        media_store,
        media_store_current,
        closing_current,
    )
    search = LocalSearchAutonomyAdapter(
        search_runtime,
        search_runtime_current,
        closing_current,
    )

    def writer_current() -> object:
        return writer

    browser = IsolatedBrowserAutonomyAdapter(
        browser_service,
        writer,
        browser_service_current,
        writer_current,
        closing_current,
    )
    comparison = MarkdownComparisonArtifactAdapter(
        writer,
        writer_current,
        closing_current,
    )
    journal = SqliteAutonomyJournal(
        database,
        database_current,
        media_store.binding_digest,
    )
    service = BoundedAutonomyService(
        search=search,
        browser=browser,
        artifacts=comparison,
        checkpoints=journal,
        terminal=journal,
    )

    def ready_current() -> bool:
        return search.ready and browser.ready and comparison.ready and writer.ready and journal.ready

    composed = ComposedAutonomyRuntime(service, writer, ready_current)
    return composed if composed.ready else None


def _official_fetched_projection(outcome: SearchOrchestratorOutcome) -> SearchOrchestratorOutcome:
    if not isinstance(outcome, SearchOrchestratorOutcome):
        raise AutonomyContractError("Search Fabric returned an invalid outcome")
    evidence = tuple(
        item
        for item in outcome.result.evidence
        if item.fetch_state is SearchFetchState.FETCHED
        and item.source_class is SearchSourceClass.PRIMARY_OFFICIAL
        and item.content_hash is not None
    )
    if not evidence:
        raise AutonomyContractError("Search Fabric returned no fetched official evidence")
    result = SearchResultV1(
        request_id=outcome.result.request_id,
        query_digest=outcome.result.query_digest,
        intent=outcome.result.intent,
        language=outcome.result.language,
        evidence=evidence,
        backend_ids=outcome.result.backend_ids,
        engine_errors=outcome.result.engine_errors,
        candidate_count=max(len(evidence), outcome.result.candidate_count),
        cache_hits=min(outcome.result.cache_hits, len(evidence)),
        latency_ms=outcome.result.latency_ms,
    )
    evidence_text = json.dumps(
        [
            {
                "source_id": item.source.source_id,
                "title": item.source.title,
                "url": item.source.url,
                "snippet": item.source.snippet,
                "source_class": item.source_class.value,
                "content_hash": item.content_hash,
                "verification_reasons": list(item.verification_reasons),
            }
            for item in evidence
        ],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(evidence_text) > 30_000:
        raise AutonomyContractError("official evidence projection exceeded its limit")
    return SearchOrchestratorOutcome(
        result=result,
        receipt=SearchReceiptV1.from_result(result),
        verification_state=SearchVerificationState.PARTIAL,
        evidence_text=evidence_text,
    )


def _browser_outputs(outputs: tuple[object, ...]) -> tuple[str, bytes]:
    if len(outputs) != 2:
        raise AutonomyContractError("isolated browser returned an unexpected output set")
    by_kind = {getattr(output, "kind", None): output for output in outputs}
    if set(by_kind) != {BrowserOutputKind.TEXT, BrowserOutputKind.SCREENSHOT}:
        raise AutonomyContractError("isolated browser returned an unexpected output set")
    text_output = by_kind[BrowserOutputKind.TEXT]
    screenshot_output = by_kind[BrowserOutputKind.SCREENSHOT]
    if (
        getattr(text_output, "step_index", None) != 1
        or getattr(text_output, "media_type", None) != "text/plain; charset=utf-8"
        or getattr(screenshot_output, "step_index", None) != 2
        or getattr(screenshot_output, "media_type", None) != "image/png"
    ):
        raise AutonomyContractError("isolated browser output binding is invalid")
    try:
        text = text_output.data.decode("utf-8", errors="strict")
    except (AttributeError, UnicodeDecodeError):
        raise AutonomyContractError("isolated browser text output is invalid") from None
    if (
        not text.strip()
        or len(text) > _MAX_BROWSER_TEXT_CHARS
        or any(ord(character) < 32 and character not in "\t\n\r" for character in text)
    ):
        raise AutonomyContractError("isolated browser text output is invalid")
    data = getattr(screenshot_output, "data", None)
    if type(data) is not bytes:
        raise AutonomyContractError("isolated browser screenshot output is invalid")
    return text, data


def _canonical_browser_png(data: bytes) -> CanonicalPng:
    if type(data) is not bytes or not 1 <= len(data) <= MAX_PNG_BYTES:
        raise AutonomyContractError("isolated browser screenshot output is invalid")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            return canonicalize_image(opened)
    except (OSError, UnidentifiedImageError, ValueError):
        raise AutonomyContractError("isolated browser screenshot output is invalid") from None
    except Exception:
        raise AutonomyContractError("isolated browser screenshot output is invalid") from None


def _comparison_markdown(request: ComparisonTableRequest) -> bytes:
    lines = [
        "# SearXNG official documentation comparison",
        "",
        "| official_page | topic | documented_behavior | evidence |",
        "|---|---|---|---|",
    ]
    for page in request.browser.pages:
        excerpt = _markdown_cell(page.text, maximum=_MAX_COMPARISON_EXCERPT_CHARS)
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(page.url, maximum=500),
                    _markdown_cell(page.title, maximum=500),
                    excerpt,
                    "direct browser extraction and canonical PNG screenshot",
                )
            )
            + " |"
        )
    body = ("\n".join(lines) + "\n").encode("utf-8")
    if not body or len(body) > _MAX_COMPARISON_BYTES:
        raise AutonomyContractError("comparison artifact exceeded its limit")
    return body


def _markdown_cell(value: str, *, maximum: int) -> str:
    normalized = " ".join(value.split())
    normalized = html.escape(normalized, quote=False)
    for character in ("\\", "|", "`", "[", "]", "(", ")", "*", "_"):
        normalized = normalized.replace(character, f"\\{character}")
    return normalized[:maximum]


def _plan_digest(request: BrowserResearchRequest | ComparisonTableRequest) -> str:
    from .autonomy import SEARXNG_OFFICIAL_RESEARCH_V1

    return SEARXNG_OFFICIAL_RESEARCH_V1.digest


def _require_written_artifact(
    value: object,
    *,
    data: bytes,
    kind: ArtifactKind,
    media_type: str,
) -> None:
    digest = hashlib.sha256(data).hexdigest()
    if (
        not isinstance(value, ArtifactRef)
        or value.kind is not kind
        or value.media_type != media_type
        or value.size_bytes != len(data)
        or value.sha256 != digest
    ):
        raise AutonomyContractError("artifact writer returned an invalid reference")
    if _DIGEST.fullmatch(digest) is None:
        raise AutonomyContractError("artifact digest is invalid")


async def _require_authorized(callback: AuthorizationCurrent) -> None:
    if not callable(callback):
        raise TypeError("authorization_current must be callable")
    try:
        allowed = await callback()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AutonomyAuthorizationError("autonomy authorization is unavailable") from None
    if allowed is not True:
        raise AutonomyAuthorizationError("autonomy authorization is no longer current")


def _identity_is_current(
    expected: object,
    identity_current: IdentityCurrent,
    closing_current: ClosingCurrent,
) -> bool:
    try:
        return closing_current() is False and identity_current() is expected
    except Exception:
        return False


def _require_identity_current(
    expected: object,
    identity_current: IdentityCurrent,
    closing_current: ClosingCurrent,
) -> None:
    if not _identity_is_current(expected, identity_current, closing_current):
        raise AutonomyAuthorizationError("autonomy runtime identity is no longer current")


def _require_callable(value: object, label: str) -> None:
    if not callable(value):
        raise TypeError(f"{label} must be callable")


__all__ = [
    "AutonomyArtifactWriterPort",
    "ComposedAutonomyRuntime",
    "IsolatedBrowserAutonomyAdapter",
    "LocalSearchAutonomyAdapter",
    "MediaPipelineAutonomyArtifactWriter",
    "MarkdownComparisonArtifactAdapter",
    "compose_bounded_autonomy_runtime",
]
