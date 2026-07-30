"""Durable Jobs を用いた bot-owned Discord media edit の限定 executor。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.jobs import (
    Attempt,
    DurableJobService,
    ExecutionContext,
    Job,
    Outcome,
    OutcomeKind,
    Receipt,
    Revision,
)

from .artifacts import MAX_DELIVERY_RETENTION_SECONDS, MediaArtifactStore
from .domain import ArtifactKind, ArtifactRef, ArtifactScope, MediaValidationError

if TYPE_CHECKING:
    from .delivery import MediaArtifactDeliveryPreparer, PreparedMediaAttachment


MEDIA_DELIVERY_JOB_KIND = "discord.media_delivery.v1"
MEDIA_DELIVERY_JOB_REVISION = 1
MEDIA_DELIVERY_PAYLOAD_SCHEMA = "yonerai.discord.media-delivery-job.v1"
MEDIA_DELIVERY_RECEIPT_SCHEMA = "yonerai.discord.media-delivery-receipt.v1"
MAX_DURABLE_MEDIA_ATTACHMENTS = 4

_MAX_DISCORD_ID = (1 << 64) - 1
_HEX_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_LEASE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ACTION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CAPABILITY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,255}$")
_RBAC_VALUES = frozenset(int(level) for level in RbacLevel)
PreparerCurrent = Callable[[], object | None]


class DurableMediaDeliveryError(RuntimeError):
    """配送入力または現在権限を安全に確定できない。"""


class MediaDeliveryTransientError(RuntimeError):
    """副作用開始前だけ再試行できる transport failure。"""


class MediaDeliveryRejectedError(RuntimeError):
    """対象が bot-owned でない等、再試行しない transport rejection。"""


@dataclass(frozen=True, slots=True)
class DurableMediaDeliveryPayload:
    scope: ArtifactScope = field(repr=False)
    target_message_id: int
    artifacts: tuple[ArtifactRef, ...] = field(repr=False)
    required_action_ids: tuple[str, ...]
    required_capabilities: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ArtifactScope):
            raise DurableMediaDeliveryError("media delivery scope is unavailable")
        _validate_discord_id(self.target_message_id, "target_message_id")
        if (
            not isinstance(self.artifacts, tuple)
            or not 1 <= len(self.artifacts) <= MAX_DURABLE_MEDIA_ATTACHMENTS
            or any(not isinstance(ref, ArtifactRef) for ref in self.artifacts)
        ):
            raise DurableMediaDeliveryError("media delivery artifacts are unavailable")
        if len({ref.artifact_id for ref in self.artifacts}) != len(self.artifacts):
            raise DurableMediaDeliveryError("media delivery artifacts are unavailable")
        if any(ref.scope_digest != self.scope.digest for ref in self.artifacts):
            raise DurableMediaDeliveryError("media delivery scope is unavailable")
        if (
            not isinstance(self.required_action_ids, tuple)
            or not 1 <= len(self.required_action_ids) <= 20
            or len(set(self.required_action_ids)) != len(self.required_action_ids)
            or any(
                not isinstance(value, str) or _ACTION_ID.fullmatch(value) is None for value in self.required_action_ids
            )
        ):
            raise DurableMediaDeliveryError("media delivery action binding is unavailable")
        if (
            not isinstance(self.required_capabilities, tuple)
            or not 1 <= len(self.required_capabilities) <= 64
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or _CAPABILITY_ID.fullmatch(item[0]) is None
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                or item[1] not in _RBAC_VALUES
                for item in self.required_capabilities
            )
            or len({item[0] for item in self.required_capabilities}) != len(self.required_capabilities)
        ):
            raise DurableMediaDeliveryError("media delivery capability binding is unavailable")

    @property
    def delivery_digest(self) -> str:
        encoded = _canonical_json(self.to_mapping())
        return hashlib.sha256(b"yonerai.discord.media-delivery.v1\0" + encoded).hexdigest()

    @property
    def action_key(self) -> str:
        return f"{MEDIA_DELIVERY_JOB_KIND}:{self.delivery_digest}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifacts": [_artifact_to_mapping(ref) for ref in self.artifacts],
            "required_action_ids": list(self.required_action_ids),
            "required_capabilities": [
                {"capability_id": capability_id, "minimum_level": minimum_level}
                for capability_id, minimum_level in self.required_capabilities
            ],
            "schema": MEDIA_DELIVERY_PAYLOAD_SCHEMA,
            "scope": {
                "channel_id": self.scope.channel_id,
                "guild_id": self.scope.guild_id,
                "request_id": self.scope.request_id,
                "user_id": self.scope.user_id,
            },
            "target_message_id": self.target_message_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DurableMediaDeliveryPayload:
        try:
            _require_exact_keys(
                value,
                {
                    "artifacts",
                    "required_action_ids",
                    "required_capabilities",
                    "schema",
                    "scope",
                    "target_message_id",
                },
            )
            if value["schema"] != MEDIA_DELIVERY_PAYLOAD_SCHEMA:
                raise ValueError
            raw_scope = value["scope"]
            if not isinstance(raw_scope, Mapping):
                raise ValueError
            _require_exact_keys(raw_scope, {"channel_id", "guild_id", "request_id", "user_id"})
            scope = ArtifactScope(
                request_id=_require_string(raw_scope["request_id"]),
                guild_id=_require_optional_discord_id(raw_scope["guild_id"]),
                channel_id=_require_discord_id(raw_scope["channel_id"]),
                user_id=_require_discord_id(raw_scope["user_id"]),
            )
            raw_artifacts = value["artifacts"]
            if not isinstance(raw_artifacts, list):
                raise ValueError
            artifacts = tuple(_artifact_from_mapping(item) for item in raw_artifacts)
            raw_action_ids = value["required_action_ids"]
            if not isinstance(raw_action_ids, list):
                raise ValueError
            raw_capabilities = value["required_capabilities"]
            if not isinstance(raw_capabilities, list):
                raise ValueError
            return cls(
                scope=scope,
                target_message_id=_require_discord_id(value["target_message_id"]),
                artifacts=artifacts,
                required_action_ids=tuple(_require_string(item) for item in raw_action_ids),
                required_capabilities=tuple(_capability_binding_from_mapping(item) for item in raw_capabilities),
            )
        except (KeyError, MediaValidationError, TypeError, ValueError):
            raise DurableMediaDeliveryError("media delivery payload is unavailable") from None


CurrentCheck = Callable[[DurableMediaDeliveryPayload], bool]


@dataclass(frozen=True, slots=True)
class MediaDeliveryTargetLease:
    guild_id: int | None
    channel_id: int
    message_id: int
    bot_owned: bool
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.guild_id is not None:
            _validate_discord_id(self.guild_id, "guild_id")
        _validate_discord_id(self.channel_id, "channel_id")
        _validate_discord_id(self.message_id, "message_id")
        if self.bot_owned is not True:
            raise MediaDeliveryRejectedError("media delivery target is unavailable")
        if not isinstance(self.token, str) or not _LEASE_TOKEN.fullmatch(self.token):
            raise MediaDeliveryRejectedError("media delivery target is unavailable")


@dataclass(frozen=True, slots=True)
class MediaDeliverySinkRequest:
    guild_id: int | None
    channel_id: int
    user_id: int
    message_id: int
    delivery_digest: str
    required_action_ids: tuple[str, ...]
    required_capabilities: tuple[tuple[str, int], ...]
    attachments: tuple[PreparedMediaAttachment, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.guild_id is not None:
            _validate_discord_id(self.guild_id, "guild_id")
        _validate_discord_id(self.channel_id, "channel_id")
        _validate_discord_id(self.user_id, "user_id")
        _validate_discord_id(self.message_id, "message_id")
        _validate_digest(self.delivery_digest)
        if (
            not isinstance(self.required_action_ids, tuple)
            or not 1 <= len(self.required_action_ids) <= 20
            or len(set(self.required_action_ids)) != len(self.required_action_ids)
            or any(
                not isinstance(value, str) or _ACTION_ID.fullmatch(value) is None for value in self.required_action_ids
            )
        ):
            raise DurableMediaDeliveryError("media delivery action binding is unavailable")
        if (
            not isinstance(self.required_capabilities, tuple)
            or not 1 <= len(self.required_capabilities) <= 64
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or _CAPABILITY_ID.fullmatch(item[0]) is None
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                or item[1] not in _RBAC_VALUES
                for item in self.required_capabilities
            )
            or len({item[0] for item in self.required_capabilities}) != len(self.required_capabilities)
        ):
            raise DurableMediaDeliveryError("media delivery capability binding is unavailable")
        if (
            not isinstance(self.attachments, tuple)
            or not 1 <= len(self.attachments) <= MAX_DURABLE_MEDIA_ATTACHMENTS
            or any(not _is_prepared_attachment(item) for item in self.attachments)
        ):
            raise DurableMediaDeliveryError("media delivery attachments are unavailable")


@dataclass(frozen=True, slots=True)
class MediaDeliverySinkReceipt:
    guild_id: int | None
    channel_id: int
    message_id: int
    delivery_digest: str
    attachment_ids: tuple[int, ...]
    received_at: datetime

    def __post_init__(self) -> None:
        if self.guild_id is not None:
            _validate_discord_id(self.guild_id, "guild_id")
        _validate_discord_id(self.channel_id, "channel_id")
        _validate_discord_id(self.message_id, "message_id")
        _validate_digest(self.delivery_digest)
        if (
            not isinstance(self.attachment_ids, tuple)
            or not 1 <= len(self.attachment_ids) <= MAX_DURABLE_MEDIA_ATTACHMENTS
            or any(not _is_discord_id(value) for value in self.attachment_ids)
            or len(set(self.attachment_ids)) != len(self.attachment_ids)
        ):
            raise DurableMediaDeliveryError("media delivery receipt is unavailable")
        if not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise DurableMediaDeliveryError("media delivery receipt is unavailable")


class MediaDeliverySink(Protocol):
    async def preflight(self, request: MediaDeliverySinkRequest) -> MediaDeliveryTargetLease: ...

    async def edit(
        self,
        request: MediaDeliverySinkRequest,
        *,
        lease: MediaDeliveryTargetLease,
    ) -> MediaDeliverySinkReceipt: ...


SinkCurrent = Callable[[], object | None]


class DurableMediaDeliverySubmitter:
    """既存 DurableJobService へ scope-bound media edit を重複なく投入する。"""

    def __init__(
        self,
        jobs: DurableJobService,
        *,
        store: MediaArtifactStore,
        store_current: Callable[[], object | None],
        clock: Callable[[], datetime] | None = None,
        retention_seconds: int = MAX_DELIVERY_RETENTION_SECONDS,
    ) -> None:
        if not isinstance(jobs, DurableJobService):
            raise TypeError("jobs must be a DurableJobService")
        if not isinstance(store, MediaArtifactStore) or not callable(store_current):
            raise TypeError("media delivery retention store is unavailable")
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or not 1 <= retention_seconds <= MAX_DELIVERY_RETENTION_SECONDS
        ):
            raise ValueError("retention_seconds is outside the delivery retention window")
        self._jobs = jobs
        self._store = store
        self._store_current = store_current
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retention_seconds = retention_seconds

    def submit(self, payload: DurableMediaDeliveryPayload, *, max_attempts: int = 5) -> Job:
        if not isinstance(payload, DurableMediaDeliveryPayload):
            raise TypeError("payload must be a DurableMediaDeliveryPayload")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 25:
            raise ValueError("max_attempts must be between 1 and 25")
        if self._store_current() is not self._store:
            raise DurableMediaDeliveryError("media delivery retention store changed")
        expected_payload = payload.to_mapping()
        existing = self._jobs.repository.get_by_action(
            payload.action_key,
            Revision(MEDIA_DELIVERY_JOB_REVISION),
        )
        if existing is not None:
            return _require_exact_delivery_job(
                existing,
                payload=payload,
                expected_payload=expected_payload,
                max_attempts=max_attempts,
            )
        retain_until = int((self._clock() + timedelta(seconds=self._retention_seconds)).timestamp())
        for ref in payload.artifacts:
            if self._store_current() is not self._store:
                raise DurableMediaDeliveryError("media delivery retention store changed")
            self._store.retain(
                ref,
                scope=payload.scope,
                delivery_key=payload.action_key,
                retain_until=retain_until,
            )
        if self._store_current() is not self._store:
            raise DurableMediaDeliveryError("media delivery retention store changed")
        return _require_exact_delivery_job(
            self._jobs.submit(
                action_key=payload.action_key,
                revision=MEDIA_DELIVERY_JOB_REVISION,
                kind=MEDIA_DELIVERY_JOB_KIND,
                payload=expected_payload,
                guild_id=payload.scope.guild_id,
                max_attempts=max_attempts,
            ),
            payload=payload,
            expected_payload=expected_payload,
            max_attempts=max_attempts,
        )


class DurableMediaDeliveryExecutor:
    """準備完了まではretry可、Discord edit開始後は必ずuncertainへ収束する。"""

    def __init__(
        self,
        *,
        preparer: MediaArtifactDeliveryPreparer,
        preparer_current: PreparerCurrent,
        sink: MediaDeliverySink,
        sink_current: SinkCurrent,
        authorization_current: CurrentCheck,
        target_current: CurrentCheck,
        retention_store: MediaArtifactStore,
        retention_store_current: Callable[[], object | None],
    ) -> None:
        from .delivery import MediaArtifactDeliveryPreparer

        if not isinstance(preparer, MediaArtifactDeliveryPreparer):
            raise TypeError("preparer must be a MediaArtifactDeliveryPreparer")
        if not all(
            callable(value)
            for value in (
                preparer_current,
                sink_current,
                authorization_current,
                target_current,
                retention_store_current,
            )
        ):
            raise TypeError("media delivery current checks must be callable")
        if not isinstance(retention_store, MediaArtifactStore):
            raise TypeError("media delivery retention store is unavailable")
        if not callable(getattr(sink, "preflight", None)) or not callable(getattr(sink, "edit", None)):
            raise TypeError("sink must implement the media delivery sink contract")
        self._preparer = preparer
        self._preparer_current = preparer_current
        self._sink = sink
        self._sink_current = sink_current
        self._authorization_current = authorization_current
        self._target_current = target_current
        self._retention_store = retention_store
        self._retention_store_current = retention_store_current

    async def execute(self, job: Job, attempt: Attempt, context: ExecutionContext) -> Outcome:
        from yonerai_discord.modules.ai.orchestration import PlanArtifactOutput

        from .delivery import MediaDeliveryError

        del attempt
        try:
            payload = DurableMediaDeliveryPayload.from_mapping(job.payload)
        except DurableMediaDeliveryError:
            return self._before_side_effect(
                context,
                Outcome.nonretryable_failure("InvalidMediaDeliveryPayload", "media delivery payload is unavailable"),
            )
        if (
            job.kind != MEDIA_DELIVERY_JOB_KIND
            or job.revision.value != MEDIA_DELIVERY_JOB_REVISION
            or job.guild_id != payload.scope.guild_id
            or job.action_key != payload.action_key
        ):
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.nonretryable_failure(
                        "MediaDeliveryBindingMismatch",
                        "media delivery binding is unavailable",
                    ),
                ),
            )
        if not self._all_current(payload):
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.skipped("media delivery authorization changed"),
                ),
            )

        outputs = tuple(
            PlanArtifactOutput(
                step_id=f"delivery-{index:02d}",
                action_id=MEDIA_DELIVERY_JOB_KIND,
                artifact=ref,
            )
            for index, ref in enumerate(payload.artifacts, start=1)
        )
        try:
            attachments = await asyncio.to_thread(
                self._preparer.prepare,
                outputs,
                scope=payload.scope,
                authorization_current=lambda: self._all_current(payload),
            )
        except MediaDeliveryError:
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.nonretryable_failure(
                        "MediaDeliveryPreparationRejected",
                        "media delivery artifact is unavailable",
                    ),
                ),
            )
        if not self._all_current(payload):
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.skipped("media delivery authorization changed"),
                ),
            )

        request = MediaDeliverySinkRequest(
            guild_id=payload.scope.guild_id,
            channel_id=payload.scope.channel_id,
            user_id=payload.scope.user_id,
            message_id=payload.target_message_id,
            delivery_digest=payload.delivery_digest,
            required_action_ids=payload.required_action_ids,
            required_capabilities=payload.required_capabilities,
            attachments=attachments,
        )
        try:
            lease = await self._sink.preflight(request)
        except asyncio.CancelledError:
            raise
        except (MediaDeliveryTransientError, TimeoutError, ConnectionError):
            if not self._all_current(payload):
                return await self._finish(
                    payload,
                    self._before_side_effect(
                        context,
                        Outcome.skipped("media delivery authorization changed"),
                    ),
                )
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.retryable_failure(
                        "MediaDeliveryPreflightTransient",
                        "media delivery target is temporarily unavailable",
                    ),
                ),
            )
        except Exception:
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.nonretryable_failure(
                        "MediaDeliveryPreflightRejected",
                        "media delivery target is unavailable",
                    ),
                ),
            )
        if not self._lease_matches(lease, payload) or not self._all_current(payload):
            return await self._finish(
                payload,
                self._before_side_effect(
                    context,
                    Outcome.nonretryable_failure(
                        "MediaDeliveryPreflightRejected",
                        "media delivery target is unavailable",
                    ),
                ),
            )
        if not context.begin_side_effect():
            return await self._finish(
                payload,
                Outcome.skipped("media delivery authorization changed"),
            )
        if not self._all_current(payload):
            return await self._finish(
                payload,
                Outcome.uncertain(
                    "MediaDeliveryAuthorizationChangedAfterCommit",
                    "media delivery outcome is uncertain",
                ),
            )

        try:
            receipt = await self._sink.edit(request, lease=lease)
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self._finish(
                payload,
                Outcome.uncertain(
                    "MediaDeliveryTransportUncertain",
                    "media delivery outcome is uncertain",
                ),
            )
        if not self._receipt_matches(receipt, request) or not self._all_current(payload):
            return await self._finish(
                payload,
                Outcome.uncertain(
                    "MediaDeliveryReceiptMismatch",
                    "media delivery outcome is uncertain",
                ),
            )

        return await self._finish(
            payload,
            Outcome.succeeded(
                Receipt(
                    external_id=str(receipt.message_id),
                    details={
                        "attachment_ids": list(receipt.attachment_ids),
                        "channel_id": receipt.channel_id,
                        "delivery_digest": receipt.delivery_digest,
                        "guild_id": receipt.guild_id,
                        "message_id": receipt.message_id,
                        "schema": MEDIA_DELIVERY_RECEIPT_SCHEMA,
                    },
                    received_at=receipt.received_at,
                )
            ),
        )

    @staticmethod
    def _before_side_effect(context: ExecutionContext, outcome: Outcome) -> Outcome:
        if context.complete_without_side_effect():
            return outcome
        return Outcome.skipped("media delivery authorization changed")

    def _all_current(self, payload: DurableMediaDeliveryPayload) -> bool:
        try:
            return (
                self._preparer_current() is self._preparer
                and self._sink_current() is self._sink
                and self._retention_store_current() is self._retention_store
                and self._authorization_current(payload) is True
                and self._target_current(payload) is True
            )
        except Exception:
            return False

    async def _finish(self, payload: DurableMediaDeliveryPayload, outcome: Outcome) -> Outcome:
        if outcome.kind not in {OutcomeKind.SUCCEEDED, OutcomeKind.UNCERTAIN}:
            return outcome
        try:
            await asyncio.to_thread(self._release_retention, payload)
        except Exception:
            # The bounded lease expires even when terminal cleanup cannot run.
            pass
        return outcome

    def _release_retention(self, payload: DurableMediaDeliveryPayload) -> None:
        if self._retention_store_current() is not self._retention_store:
            return
        for ref in payload.artifacts:
            self._retention_store.release(
                ref,
                scope=payload.scope,
                delivery_key=payload.action_key,
            )

    @staticmethod
    def _lease_matches(lease: object, payload: DurableMediaDeliveryPayload) -> bool:
        return (
            isinstance(lease, MediaDeliveryTargetLease)
            and lease.guild_id == payload.scope.guild_id
            and lease.channel_id == payload.scope.channel_id
            and lease.message_id == payload.target_message_id
            and lease.bot_owned is True
        )

    @staticmethod
    def _receipt_matches(receipt: object, request: MediaDeliverySinkRequest) -> bool:
        return (
            isinstance(receipt, MediaDeliverySinkReceipt)
            and receipt.guild_id == request.guild_id
            and receipt.channel_id == request.channel_id
            and receipt.message_id == request.message_id
            and receipt.delivery_digest == request.delivery_digest
            and len(receipt.attachment_ids) == len(request.attachments)
        )


def _artifact_to_mapping(ref: ArtifactRef) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "byte_size": ref.byte_size,
        "content_digest": ref.content_digest,
        "height": ref.height,
        "kind": ref.kind.value,
        "recipe_digest": ref.recipe_digest,
        "scope_digest": ref.scope_digest,
        "width": ref.width,
    }


def _artifact_from_mapping(value: object) -> ArtifactRef:
    if not isinstance(value, Mapping):
        raise ValueError
    _require_exact_keys(
        value,
        {
            "artifact_id",
            "byte_size",
            "content_digest",
            "height",
            "kind",
            "recipe_digest",
            "scope_digest",
            "width",
        },
    )
    return ArtifactRef(
        artifact_id=_require_string(value["artifact_id"]),
        scope_digest=_require_digest(value["scope_digest"]),
        recipe_digest=_require_digest(value["recipe_digest"]),
        content_digest=_require_digest(value["content_digest"]),
        kind=ArtifactKind(_require_string(value["kind"])),
        width=_require_int(value["width"]),
        height=_require_int(value["height"]),
        byte_size=_require_int(value["byte_size"]),
    )


def _capability_binding_from_mapping(value: object) -> tuple[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError
    _require_exact_keys(value, {"capability_id", "minimum_level"})
    capability_id = _require_string(value["capability_id"])
    minimum_level = _require_int(value["minimum_level"])
    if _CAPABILITY_ID.fullmatch(capability_id) is None:
        raise ValueError
    RbacLevel(minimum_level)
    return capability_id, minimum_level


def _require_exact_delivery_job(
    job: Job,
    *,
    payload: DurableMediaDeliveryPayload,
    expected_payload: Mapping[str, Any],
    max_attempts: int,
) -> Job:
    if (
        job.kind != MEDIA_DELIVERY_JOB_KIND
        or job.revision.value != MEDIA_DELIVERY_JOB_REVISION
        or job.action_key != payload.action_key
        or job.guild_id != payload.scope.guild_id
        or job.payload != expected_payload
        or job.max_attempts != max_attempts
    ):
        raise DurableMediaDeliveryError("media delivery job binding is unavailable")
    return job


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise DurableMediaDeliveryError("media delivery payload is unavailable") from None


def _require_exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError


def _require_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise ValueError
    return value


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _is_discord_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_DISCORD_ID


def _is_prepared_attachment(value: object) -> bool:
    from .delivery import PreparedMediaAttachment

    return isinstance(value, PreparedMediaAttachment)


def _validate_discord_id(value: object, name: str) -> None:
    if not _is_discord_id(value):
        raise DurableMediaDeliveryError(f"{name} is unavailable")


def _require_discord_id(value: object) -> int:
    _validate_discord_id(value, "Discord identifier")
    return value  # type: ignore[return-value]


def _require_optional_discord_id(value: object) -> int | None:
    if value is None:
        return None
    return _require_discord_id(value)


def _validate_digest(value: object) -> None:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise DurableMediaDeliveryError("media delivery digest is unavailable")


__all__ = [
    "DurableMediaDeliveryError",
    "DurableMediaDeliveryExecutor",
    "DurableMediaDeliveryPayload",
    "DurableMediaDeliverySubmitter",
    "MEDIA_DELIVERY_JOB_KIND",
    "MEDIA_DELIVERY_JOB_REVISION",
    "MEDIA_DELIVERY_PAYLOAD_SCHEMA",
    "MEDIA_DELIVERY_RECEIPT_SCHEMA",
    "MAX_DURABLE_MEDIA_ATTACHMENTS",
    "MediaDeliveryRejectedError",
    "MediaDeliverySink",
    "MediaDeliverySinkReceipt",
    "MediaDeliverySinkRequest",
    "MediaDeliveryTargetLease",
    "MediaDeliveryTransientError",
]
