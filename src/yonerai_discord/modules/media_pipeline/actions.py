from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from yonerai_discord.ai_control.routing import RiskLevel
from yonerai_discord.capabilities import ACTION_CAPABILITIES
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.runtime_manifests.media_pipeline import (
    MEDIA_COMPOSE_GRID_CAPABILITY_ID,
    MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID,
    MEDIA_QR_ENCODE_CAPABILITY_ID,
    MEDIA_QUOTE_CARD_CAPABILITY_ID,
)

from ..ai.action_router import (
    ActionContext,
    ActionEffect,
    ActionRegistry,
    ActionResult,
    ActionSpec,
    ActionStatus,
    NaturalActionRouter,
    PlannerActionContract,
)
from .artifacts import MediaArtifactStore
from .domain import (
    ArtifactKind,
    ArtifactRef,
    ArtifactScope,
    ComposeGridRequest,
    MAX_QR_PAYLOAD_BYTES,
    MAX_RECIPE_INPUTS,
    MediaAuthorizationError,
    MediaPipelineError,
    PlaceOnCanvasRequest,
    QrEncodeRequest,
    RgbColor,
)
from .plugin import MEDIA_PIPELINE_MODULE_ID, MediaPipelinePlugin
from .quote import (
    MAX_BODY_BYTES,
    MAX_BODY_CHARACTERS,
    MAX_SOURCE_LINES,
    QuoteCardRenderer,
)
from .service import MediaPipelineService


_QR_ACTION_ID = "media.qr_encode"
_PLACE_ACTION_ID = "media.place_on_canvas"
_GRID_ACTION_ID = "media.compose_grid"
_QUOTE_ACTION_ID = "media.quote_card"
_QR_ACTION_PATH = "media qr-encode"
_PLACE_ACTION_PATH = "media place-on-canvas"
_GRID_ACTION_PATH = "media compose-grid"
_QUOTE_ACTION_PATH = "media quote-card"
_MAX_QR_CHARS = 512
_MAX_ACTION_CANVAS_DIMENSION = 2_048
_MAX_ACTION_CANVAS_PIXELS = 4_194_304
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_UINT = re.compile(r"[0-9]+\Z")
if (
    ACTION_CAPABILITIES.get(_QR_ACTION_PATH) != MEDIA_QR_ENCODE_CAPABILITY_ID
    or ACTION_CAPABILITIES.get(_PLACE_ACTION_PATH) != MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID
    or ACTION_CAPABILITIES.get(_GRID_ACTION_PATH) != MEDIA_COMPOSE_GRID_CAPABILITY_ID
    or ACTION_CAPABILITIES.get(_QUOTE_ACTION_PATH) != MEDIA_QUOTE_CARD_CAPABILITY_ID
):
    raise RuntimeError("Media planner action capability bindings are inconsistent")


def _never_parse(_: str) -> None:
    """Direct registry parseでは絶対に一致しないplanner-only parser。"""

    return None


class MediaActionRegistration:
    """ActionSpecと、構築後に確定するActionRegistry identityを結ぶ。"""

    def __init__(self, adapter: _MediaActionAdapter, specs: tuple[ActionSpec, ...]) -> None:
        self.adapter = adapter
        self.specs = specs

    def bind_registry(self, registry: ActionRegistry) -> None:
        self.adapter.bind_registry(registry)


class _MediaActionAdapter:
    def __init__(self, router: NaturalActionRouter) -> None:
        self._router = router
        self._bot = router.bot
        self._plugin = getattr(self._bot, "media_pipeline_plugin", None)
        self._service = getattr(self._bot, "media_pipeline_service", None)
        self._store = getattr(self._bot, "media_pipeline_store", None)
        self._capability_registry = getattr(self._bot, "capability_registry", None)
        self._guard = getattr(self._bot, "capability_guard", None)
        self._semaphore = getattr(self._plugin, "execution_semaphore", None)
        self._quote_renderer = getattr(self._plugin, "quote_renderer", None)
        self._registry: ActionRegistry | None = None
        self._specs: dict[str, ActionSpec] = {}

    def bind_specs(self, specs: tuple[ActionSpec, ...]) -> None:
        if self._specs:
            raise RuntimeError("Media ActionSpec identities are already bound")
        self._specs = {spec.action_id: spec for spec in specs}

    def bind_registry(self, registry: ActionRegistry) -> None:
        if self._registry is not None:
            raise RuntimeError("Media ActionRegistry identity is already bound")
        if not isinstance(registry, ActionRegistry):
            raise TypeError("registry must be an ActionRegistry")
        if any(registry.get(spec.action_id) is not spec for spec in self._specs.values()):
            raise RuntimeError("Media ActionSpec identity changed before registry binding")
        self._registry = registry

    async def qr_encode(self, context: ActionContext, parameters: Mapping[str, Any]) -> ActionResult:
        parsed = _qr_parameters(parameters)
        if parsed is None:
            return _denied()
        payload, scale, border = parsed
        scope = context.artifact_scope
        spec = self._specs.get(_QR_ACTION_ID)
        if not isinstance(scope, ArtifactScope) or spec is None:
            return _denied()
        request = QrEncodeRequest(scope, payload, scale=scale, border=border)
        return await self._execute(
            context,
            spec,
            lambda commit_check: self._service.qr_encode(request, commit_check=commit_check),
        )

    async def place_on_canvas(self, context: ActionContext, parameters: Mapping[str, Any]) -> ActionResult:
        parsed = _place_parameters(parameters)
        if parsed is None:
            return _denied()
        source, width, height, background, x, y = parsed
        scope = context.artifact_scope
        spec = self._specs.get(_PLACE_ACTION_ID)
        if (
            not isinstance(scope, ArtifactScope)
            or spec is None
            or source.scope_digest != scope.digest
            or source.kind not in {ArtifactKind.QR_CODE, ArtifactKind.IMAGE}
        ):
            return _denied()
        try:
            request = PlaceOnCanvasRequest(
                scope,
                source,
                width,
                height,
                background=background,
                x=x,
                y=y,
            )
        except MediaPipelineError:
            return _denied()
        return await self._execute(
            context,
            spec,
            lambda commit_check: self._service.image_place_on_canvas(request, commit_check=commit_check),
            binding_check=lambda: (
                parameters.get("source") is source
                and source.scope_digest == scope.digest
                and source.kind in {ArtifactKind.QR_CODE, ArtifactKind.IMAGE}
            ),
        )

    async def compose_grid(self, context: ActionContext, parameters: Mapping[str, Any]) -> ActionResult:
        parsed = _compose_grid_parameters(parameters)
        if parsed is None:
            return _denied()
        sources, width, height, columns, background, padding, gap = parsed
        scope = context.artifact_scope
        spec = self._specs.get(_GRID_ACTION_ID)
        if (
            not isinstance(scope, ArtifactScope)
            or spec is None
            or any(
                source.scope_digest != scope.digest or source.kind not in {ArtifactKind.QR_CODE, ArtifactKind.IMAGE}
                for source in sources
            )
        ):
            return _denied()
        try:
            request = ComposeGridRequest(
                scope,
                sources,
                width,
                height,
                columns,
                background=background,
                padding=padding,
                gap=gap,
            )
        except MediaPipelineError:
            return _denied()
        return await self._execute(
            context,
            spec,
            lambda commit_check: self._service.image_compose_grid(request, commit_check=commit_check),
            binding_check=lambda: (
                parameters.get("sources") is sources
                and all(
                    source.scope_digest == scope.digest and source.kind in {ArtifactKind.QR_CODE, ArtifactKind.IMAGE}
                    for source in sources
                )
            ),
        )

    async def quote_card(self, context: ActionContext, parameters: Mapping[str, Any]) -> ActionResult:
        body = _quote_parameters(parameters)
        scope = context.artifact_scope
        spec = self._specs.get(_QUOTE_ACTION_ID)
        message = context.message
        author = getattr(message, "author", None)
        display_name = getattr(author, "display_name", None)
        timestamp = getattr(message, "created_at", None)
        if (
            body is None
            or not isinstance(scope, ArtifactScope)
            or spec is None
            or not isinstance(display_name, str)
            or not display_name.strip()
            or not isinstance(timestamp, datetime)
            or timestamp.utcoffset() is None
            or not isinstance(self._quote_renderer, QuoteCardRenderer)
        ):
            return _denied()
        return await self._execute(
            context,
            spec,
            lambda commit_check: self._service.quote_card(
                scope=scope,
                display_name=display_name,
                body=body,
                timestamp=timestamp,
                commit_check=commit_check,
            ),
            binding_check=lambda: (
                parameters.get("body") == body
                and getattr(message, "author", None) is author
                and getattr(author, "display_name", None) == display_name
                and getattr(message, "created_at", None) == timestamp
            ),
        )

    async def _execute(
        self,
        context: ActionContext,
        spec: ActionSpec,
        operation,
        *,
        binding_check=lambda: True,
    ) -> ActionResult:
        scope = context.artifact_scope
        semaphore = self._semaphore
        if not isinstance(scope, ArtifactScope) or not isinstance(semaphore, asyncio.Semaphore):
            return _unavailable()
        active = threading.Event()
        slot_acquired = False
        worker_owns_slot = False
        slot_released = False

        def release_worker_slot(done: asyncio.Task[Any]) -> None:
            nonlocal slot_released
            if slot_released:
                return
            slot_released = True
            semaphore.release()
            if not done.cancelled():
                done.exception()

        try:
            await semaphore.acquire()
            slot_acquired = True
            if not self._current(spec, context, scope) or not binding_check():
                return _denied()
            active.set()

            def commit_check() -> bool:
                return (
                    active.is_set()
                    and context.artifact_scope is scope
                    and binding_check()
                    and self._current(spec, context, scope)
                )

            worker_task = asyncio.create_task(asyncio.to_thread(operation, commit_check))
            worker_task.add_done_callback(release_worker_slot)
            worker_owns_slot = True
            result = await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            active.clear()
            raise
        except MediaAuthorizationError:
            return _denied()
        except (MediaPipelineError, TypeError, ValueError):
            return ActionResult(ActionStatus.FAILED, "画像artifactを安全に生成できませんでした。")
        except Exception:
            return ActionResult(ActionStatus.FAILED, "画像artifactのローカル処理に失敗しました。")
        finally:
            active.clear()
            if slot_acquired and not worker_owns_slot:
                semaphore.release()
        if not self._current(spec, context, scope):
            return _denied()
        return ActionResult(ActionStatus.COMPLETED, "画像artifactを生成しました。", artifact=result.artifact)

    def _current(self, spec: ActionSpec, context: ActionContext, scope: ArtifactScope) -> bool:
        bot = self._bot
        router = self._router
        registry = self._registry
        plugin = self._plugin
        service = self._service
        store = self._store
        capability_registry = self._capability_registry
        guard = self._guard
        message = context.message
        try:
            ids = (
                int(message.guild.id),
                int(message.channel.id),
                int(message.author.id),
            )
            readiness = getattr(bot, "runtime_capability_readiness", None)
            capability = capability_registry.capability(spec.capability_id)
            return (
                context.bot is bot
                and context.artifact_scope is scope
                and ids == (scope.guild_id, scope.channel_id, scope.user_id)
                and context.request.guild_id == scope.guild_id
                and context.request.channel_id == scope.channel_id
                and context.request.user_id == scope.user_id
                and context.actor_level >= RbacLevel.TRUSTED
                and context.actor_level >= spec.rbac_floor
                and not bool(getattr(bot, "is_closing", False))
                and not router.closing
                and getattr(bot, "ai_action_router", None) is router
                and router.registry is registry
                and registry.get(spec.action_id) is spec
                and self._specs.get(spec.action_id) is spec
                and spec.effect is ActionEffect.SIDE_EFFECT
                and isinstance(plugin, MediaPipelinePlugin)
                and getattr(bot, "media_pipeline_plugin", None) is plugin
                and not plugin.closing
                and plugin.service is service
                and plugin.store is store
                and plugin.execution_semaphore is self._semaphore
                and isinstance(service, MediaPipelineService)
                and isinstance(store, MediaArtifactStore)
                and service.artifact_store is store
                and (
                    spec.action_id != _QUOTE_ACTION_ID
                    or (
                        service.quote_renderer is self._quote_renderer
                        and plugin.quote_renderer is self._quote_renderer
                        and isinstance(self._quote_renderer, QuoteCardRenderer)
                    )
                )
                and getattr(bot, "media_pipeline_service", None) is service
                and getattr(bot, "media_pipeline_store", None) is store
                and getattr(bot, "capability_registry", None) is capability_registry
                and getattr(bot, "capability_guard", None) is guard
                and getattr(guard, "registry", None) is capability_registry
                and capability.module_id == MEDIA_PIPELINE_MODULE_ID
                and capability_registry.is_module_enabled(MEDIA_PIPELINE_MODULE_ID, scope.guild_id) is True
                and capability_registry.capability_status(spec.capability_id, scope.guild_id).executable is True
                and capability_registry.runtime_available(spec.capability_id) is True
                and isinstance(readiness, dict)
                and readiness.get(spec.capability_id) is True
                and router._currently_allowed(spec, context)
                and router._mention_currently_allowed(context)
                and context.bindings_are_current()
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False


def build_media_action_registration(router: NaturalActionRouter) -> MediaActionRegistration:
    adapter = _MediaActionAdapter(router)
    qr_spec = ActionSpec(
        _QR_ACTION_ID,
        _QR_ACTION_PATH,
        MEDIA_QR_ENCODE_CAPABILITY_ID,
        RbacLevel.TRUSTED,
        _never_parse,
        adapter.qr_encode,
        effect=ActionEffect.SIDE_EFFECT,
        planner_contract=PlannerActionContract(
            "短いプレーンテキストを、同一request scopeに束縛されたQR画像artifactへ変換する。",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "payload": {"type": "string", "minLength": 1, "maxLength": _MAX_QR_CHARS},
                    "scale": {"type": "string", "pattern": r"^[0-9]{1,2}$", "default": "8"},
                    "border": {"type": "string", "pattern": r"^[0-9]{1,2}$", "default": "4"},
                },
                "required": ["payload", "scale", "border"],
            },
            ("qr", "qrコード", "artifact"),
            ("qr画像を作る", "テキストをqrにする"),
            RiskLevel.HIGH,
            {"type": "artifact_ref", "kind": ArtifactKind.QR_CODE.value},
        ),
    )
    place_spec = ActionSpec(
        _PLACE_ACTION_ID,
        _PLACE_ACTION_PATH,
        MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID,
        RbacLevel.TRUSTED,
        _never_parse,
        adapter.place_on_canvas,
        effect=ActionEffect.SIDE_EFFECT,
        planner_contract=PlannerActionContract(
            "直前stepの単一画像artifactを、同一request scopeの制限付きcanvasへ配置する。",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {
                        "type": "artifact_ref",
                        "accepted_kinds": [ArtifactKind.QR_CODE.value, ArtifactKind.IMAGE.value],
                    },
                    "canvas_width": {"type": "string", "pattern": r"^[1-9][0-9]{0,3}$"},
                    "canvas_height": {"type": "string", "pattern": r"^[1-9][0-9]{0,3}$"},
                    "background": {
                        "type": "string",
                        "pattern": r"^#[0-9A-Fa-f]{6}$",
                        "default": "#FFFFFF",
                    },
                    "x": {
                        "type": "string",
                        "pattern": r"^(?:center|[0-9]{1,4})$",
                        "default": "center",
                    },
                    "y": {
                        "type": "string",
                        "pattern": r"^(?:center|[0-9]{1,4})$",
                        "default": "center",
                    },
                },
                "required": ["source", "canvas_width", "canvas_height", "background", "x", "y"],
            },
            ("canvas", "画像配置", "artifact"),
            ("画像をキャンバスに置く", "qr画像に余白を付ける"),
            RiskLevel.HIGH,
            {"type": "artifact_ref", "kind": ArtifactKind.IMAGE.value},
        ),
    )
    grid_spec = ActionSpec(
        _GRID_ACTION_ID,
        _GRID_ACTION_PATH,
        MEDIA_COMPOSE_GRID_CAPABILITY_ID,
        RbacLevel.TRUSTED,
        _never_parse,
        adapter.compose_grid,
        effect=ActionEffect.SIDE_EFFECT,
        planner_contract=PlannerActionContract(
            "1〜8個の直接依存画像artifactを、同一request scopeの制限付きgrid画像artifactへ合成する。",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sources": {
                        "type": "artifact_ref_list",
                        "accepted_kinds": [ArtifactKind.QR_CODE.value, ArtifactKind.IMAGE.value],
                        "minItems": 1,
                        "maxItems": MAX_RECIPE_INPUTS,
                    },
                    "canvas_width": {"type": "string", "pattern": r"^[1-9][0-9]{0,3}$"},
                    "canvas_height": {"type": "string", "pattern": r"^[1-9][0-9]{0,3}$"},
                    "columns": {"type": "string", "pattern": r"^[1-8]$"},
                    "background": {
                        "type": "string",
                        "pattern": r"^#[0-9A-Fa-f]{6}$",
                        "default": "#FFFFFF",
                    },
                    "padding": {"type": "string", "pattern": r"^[0-9]{1,4}$", "default": "16"},
                    "gap": {"type": "string", "pattern": r"^[0-9]{1,4}$", "default": "16"},
                },
                "required": [
                    "sources",
                    "canvas_width",
                    "canvas_height",
                    "columns",
                    "background",
                    "padding",
                    "gap",
                ],
            },
            ("grid", "画像合成", "artifact"),
            ("画像をグリッドにする", "複数qr画像をまとめる"),
            RiskLevel.HIGH,
            {"type": "artifact_ref", "kind": ArtifactKind.IMAGE.value},
        ),
    )
    quote_spec = ActionSpec(
        _QUOTE_ACTION_ID,
        _QUOTE_ACTION_PATH,
        MEDIA_QUOTE_CARD_CAPABILITY_ID,
        RbacLevel.TRUSTED,
        _never_parse,
        adapter.quote_card,
        effect=ActionEffect.SIDE_EFFECT,
        planner_contract=PlannerActionContract(
            "実行者名と元メッセージ時刻を使い、本文をローカル引用カード画像artifactへ変換する。",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "body": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_BODY_CHARACTERS,
                    },
                },
                "required": ["body"],
            },
            ("引用カード", "quote", "artifact"),
            ("引用カードを作る", "この文章を引用画像にする"),
            RiskLevel.NORMAL,
            {"type": "artifact_ref", "kind": ArtifactKind.IMAGE.value},
        ),
    )
    specs = (qr_spec, place_spec, grid_spec, quote_spec)
    adapter.bind_specs(specs)
    return MediaActionRegistration(adapter, specs)


def _qr_parameters(parameters: Mapping[str, Any]) -> tuple[str, int, int] | None:
    if not _exact_keys(parameters, required={"payload", "scale", "border"}):
        return None
    payload = parameters.get("payload")
    if not isinstance(payload, str) or not payload.strip() or len(payload) > _MAX_QR_CHARS:
        return None
    try:
        encoded = payload.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if (
        not encoded
        or len(encoded) > MAX_QR_PAYLOAD_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in payload)
    ):
        return None
    scale = _bounded_uint(parameters.get("scale"), 1, 16)
    border = _bounded_uint(parameters.get("border"), 4, 16)
    if scale is None or border is None:
        return None
    return payload, scale, border


def _quote_parameters(parameters: Mapping[str, Any]) -> str | None:
    if not _exact_keys(parameters, required={"body"}):
        return None
    body = parameters.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARACTERS:
        return None
    try:
        encoded = body.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_BODY_BYTES or body.count("\n") + 1 > MAX_SOURCE_LINES:
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in body):
        return None
    return body


def _place_parameters(
    parameters: Mapping[str, Any],
) -> tuple[ArtifactRef, int, int, RgbColor, int | None, int | None] | None:
    if not _exact_keys(
        parameters,
        required={"source", "canvas_width", "canvas_height", "background", "x", "y"},
    ):
        return None
    source = parameters.get("source")
    if not isinstance(source, ArtifactRef):
        return None
    width = _bounded_uint(parameters.get("canvas_width"), 1, _MAX_ACTION_CANVAS_DIMENSION)
    height = _bounded_uint(parameters.get("canvas_height"), 1, _MAX_ACTION_CANVAS_DIMENSION)
    if width is None or height is None or width * height > _MAX_ACTION_CANVAS_PIXELS:
        return None
    background_raw = parameters.get("background")
    if not isinstance(background_raw, str) or _HEX_COLOR.fullmatch(background_raw) is None:
        return None
    background = RgbColor(
        int(background_raw[1:3], 16),
        int(background_raw[3:5], 16),
        int(background_raw[5:7], 16),
    )
    x_raw = parameters.get("x")
    y_raw = parameters.get("y")
    x = None if x_raw == "center" else _bounded_uint(x_raw, 0, width - 1)
    y = None if y_raw == "center" else _bounded_uint(y_raw, 0, height - 1)
    if (x_raw != "center" and x is None) or (y_raw != "center" and y is None):
        return None
    return source, width, height, background, x, y


def _compose_grid_parameters(
    parameters: Mapping[str, Any],
) -> tuple[tuple[ArtifactRef, ...], int, int, int, RgbColor, int, int] | None:
    if not _exact_keys(
        parameters,
        required={"sources", "canvas_width", "canvas_height", "columns", "background", "padding", "gap"},
    ):
        return None
    sources = parameters.get("sources")
    if (
        not isinstance(sources, tuple)
        or not 1 <= len(sources) <= MAX_RECIPE_INPUTS
        or any(not isinstance(source, ArtifactRef) for source in sources)
        or sum(source.width * source.height for source in sources) > _MAX_ACTION_CANVAS_PIXELS
    ):
        return None
    width = _bounded_uint(parameters.get("canvas_width"), 1, _MAX_ACTION_CANVAS_DIMENSION)
    height = _bounded_uint(parameters.get("canvas_height"), 1, _MAX_ACTION_CANVAS_DIMENSION)
    if width is None or height is None or width * height > _MAX_ACTION_CANVAS_PIXELS:
        return None
    columns = _bounded_uint(parameters.get("columns"), 1, len(sources))
    padding = _bounded_uint(parameters.get("padding"), 0, _MAX_ACTION_CANVAS_DIMENSION)
    gap = _bounded_uint(parameters.get("gap"), 0, _MAX_ACTION_CANVAS_DIMENSION)
    background_raw = parameters.get("background")
    if (
        columns is None
        or padding is None
        or gap is None
        or not isinstance(background_raw, str)
        or _HEX_COLOR.fullmatch(background_raw) is None
    ):
        return None
    background = RgbColor(
        int(background_raw[1:3], 16),
        int(background_raw[3:5], 16),
        int(background_raw[5:7], 16),
    )
    return sources, width, height, columns, background, padding, gap


def _exact_keys(parameters: Mapping[str, Any], *, required: set[str]) -> bool:
    if not isinstance(parameters, Mapping):
        return False
    keys = tuple(parameters)
    return all(isinstance(key, str) for key in keys) and len(keys) == len(set(keys)) and set(keys) == required


def _bounded_uint(value: Any, minimum: int, maximum: int) -> int | None:
    if not isinstance(value, str) or _UINT.fullmatch(value) is None or len(value) > 5:
        return None
    parsed = int(value)
    return parsed if minimum <= parsed <= maximum else None


def _denied() -> ActionResult:
    return ActionResult(ActionStatus.DENIED, "画像artifactの入力または現在の実行権限を確認できませんでした。")


def _unavailable() -> ActionResult:
    return ActionResult(ActionStatus.UNAVAILABLE, "ローカルMedia Pipelineは現在利用できません。")


__all__ = ["MediaActionRegistration", "build_media_action_registration"]
