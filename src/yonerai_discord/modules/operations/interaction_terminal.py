from __future__ import annotations

import asyncio
import inspect
import logging
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Lock
from typing import Any

import discord

from .failure import classify_failure, format_failure_message


logger = logging.getLogger(__name__)

_MAX_TERMINALS = 4_096
_DEFAULT_DELIVERY_TIMEOUT_SECONDS = 10.0
_SURFACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,79}")
_REFERENCE_PATTERN = re.compile(r"ERR-[A-F0-9]{12}")
_BACKGROUND_REVISION_PATTERN = re.compile(r"[a-f0-9]{64}")
_DEFERRED_TYPES = frozenset(
    {
        discord.InteractionResponseType.deferred_channel_message,
        discord.InteractionResponseType.deferred_message_update,
    }
)
_EDITABLE_DEFERRED_TYPES = frozenset({discord.InteractionResponseType.deferred_channel_message})


class InteractionFailureDelivery(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class InteractionFailureReceipt:
    error_code: str
    reference_id: str
    error_type: str
    surface: str
    retryable: bool
    partial_response: bool
    delivery: InteractionFailureDelivery

    def __post_init__(self) -> None:
        if not re.fullmatch(r"discord\.[a-z_]{1,40}", self.error_code):
            raise ValueError("error_code is invalid")
        if not _REFERENCE_PATTERN.fullmatch(self.reference_id):
            raise ValueError("reference_id is invalid")
        if not _SURFACE_PATTERN.fullmatch(self.surface):
            raise ValueError("surface is invalid")
        if not self.error_type or len(self.error_type) > 120:
            raise ValueError("error_type is invalid")
        if type(self.retryable) is not bool or type(self.partial_response) is not bool:
            raise TypeError("receipt flags must be booleans")
        if not isinstance(self.delivery, InteractionFailureDelivery):
            raise TypeError("delivery must be an InteractionFailureDelivery")


@dataclass(slots=True)
class _TerminalEntry:
    receipt: InteractionFailureReceipt
    completed: asyncio.Future[InteractionFailureReceipt]


@dataclass(frozen=True, slots=True)
class _BackgroundFailureKey:
    surface: str
    guild_id: int
    event_id: int
    actor_id: int | None
    revision: str


class InteractionFailureTerminal:
    """Discord interaction failureをcontent-free receiptへ束縛して一度だけ閉じる。"""

    def __init__(
        self,
        *,
        maximum_entries: int = _MAX_TERMINALS,
        delivery_timeout_seconds: float = _DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(maximum_entries, bool) or not isinstance(maximum_entries, int):
            raise TypeError("maximum_entries must be an integer")
        if not 1 <= maximum_entries <= _MAX_TERMINALS:
            raise ValueError("maximum_entries is outside the allowed range")
        if isinstance(delivery_timeout_seconds, bool) or not isinstance(delivery_timeout_seconds, (int, float)):
            raise TypeError("delivery_timeout_seconds must be numeric")
        if not 0.1 <= float(delivery_timeout_seconds) <= 30.0:
            raise ValueError("delivery_timeout_seconds is outside the allowed range")
        self._maximum_entries = maximum_entries
        self._delivery_timeout_seconds = float(delivery_timeout_seconds)
        self._lock = Lock()
        self._receipts: OrderedDict[tuple[str, int], _TerminalEntry] = OrderedDict()
        self._background_receipts: OrderedDict[_BackgroundFailureKey, InteractionFailureReceipt] = OrderedDict()

    async def fail_once(
        self,
        interaction: Any,
        error: BaseException,
        *,
        surface: str,
    ) -> InteractionFailureReceipt:
        normalized_surface = _surface(surface)
        interaction_id = _positive_id(getattr(interaction, "id", None))
        failure = classify_failure(error)
        created = InteractionFailureReceipt(
            error_code=f"discord.{failure.kind.value}",
            reference_id=f"ERR-{secrets.token_hex(6).upper()}",
            error_type=_error_type(failure.error_type),
            surface=normalized_surface,
            retryable=failure.retryable,
            partial_response=False,
            delivery=InteractionFailureDelivery.PENDING,
        )
        if interaction_id is None:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._record(interaction, receipt)
            return receipt
        key = (normalized_surface, interaction_id)
        pending: asyncio.Future[InteractionFailureReceipt] | None = None
        capacity_rejected = False
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                self._receipts.move_to_end(key)
                if existing.receipt.delivery is not InteractionFailureDelivery.PENDING:
                    return existing.receipt
                pending = existing.completed
            else:
                self._prune_terminal_locked(maximum_size=self._maximum_entries - 1)
                if len(self._receipts) >= self._maximum_entries:
                    capacity_rejected = True
                else:
                    completed = asyncio.get_running_loop().create_future()
                    self._receipts[key] = _TerminalEntry(created, completed)
        if pending is not None:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=self._delivery_timeout_seconds + 1.0,
                )
            except asyncio.TimeoutError:
                receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
                self._record(interaction, receipt)
                return receipt
        if capacity_rejected:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._record(interaction, receipt)
            return receipt

        response = getattr(interaction, "response", None)
        response_done = _response_is_done(response)
        response_type = getattr(response, "type", None)
        partial_response = response_done and response_type not in _DEFERRED_TYPES
        created = replace(created, partial_response=partial_response)
        message = format_failure_message(
            failure,
            error_code=created.error_code,
            reference_id=created.reference_id,
        )
        try:
            delivered_partial = await asyncio.wait_for(
                _deliver_failure(interaction, response, response_done, response_type, message),
                timeout=self._delivery_timeout_seconds,
            )
        except asyncio.CancelledError:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._finish(key, receipt)
            self._record(interaction, receipt)
            raise
        except Exception:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._finish(key, receipt)
            self._record(interaction, receipt)
            return receipt

        receipt = replace(
            created,
            partial_response=delivered_partial,
            delivery=InteractionFailureDelivery.DELIVERED,
        )
        self._finish(key, receipt)
        self._record(interaction, receipt)
        return receipt

    async def fail_context_once(
        self,
        context: Any,
        error: BaseException,
        *,
        surface: str = "prefix_command",
    ) -> InteractionFailureReceipt:
        normalized_surface = _surface(surface)
        event_id = _positive_id(getattr(getattr(context, "message", None), "id", None))
        failure = classify_failure(error)
        created = InteractionFailureReceipt(
            error_code=f"discord.{failure.kind.value}",
            reference_id=f"ERR-{secrets.token_hex(6).upper()}",
            error_type=_error_type(failure.error_type),
            surface=normalized_surface,
            retryable=failure.retryable,
            partial_response=False,
            delivery=InteractionFailureDelivery.PENDING,
        )
        if event_id is None:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._record(context, receipt)
            return receipt
        key = (normalized_surface, event_id)
        pending: asyncio.Future[InteractionFailureReceipt] | None = None
        capacity_rejected = False
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                self._receipts.move_to_end(key)
                if existing.receipt.delivery is not InteractionFailureDelivery.PENDING:
                    return existing.receipt
                pending = existing.completed
            else:
                self._prune_terminal_locked(maximum_size=self._maximum_entries - 1)
                if len(self._receipts) >= self._maximum_entries:
                    capacity_rejected = True
                else:
                    completed = asyncio.get_running_loop().create_future()
                    self._receipts[key] = _TerminalEntry(created, completed)
        if pending is not None:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=self._delivery_timeout_seconds + 1.0,
                )
            except asyncio.TimeoutError:
                receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
                self._record(context, receipt)
                return receipt
        if capacity_rejected:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._record(context, receipt)
            return receipt

        sender = getattr(context, "send", None)
        message = format_failure_message(
            failure,
            error_code=created.error_code,
            reference_id=created.reference_id,
        )
        try:
            if not callable(sender):
                raise RuntimeError("prefix response sender is unavailable")
            await asyncio.wait_for(
                sender(message, allowed_mentions=discord.AllowedMentions.none()),
                timeout=self._delivery_timeout_seconds,
            )
        except asyncio.CancelledError:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._finish(key, receipt)
            self._record(context, receipt)
            raise
        except Exception:
            receipt = replace(created, delivery=InteractionFailureDelivery.UNCERTAIN)
            self._finish(key, receipt)
            self._record(context, receipt)
            return receipt

        receipt = replace(created, delivery=InteractionFailureDelivery.DELIVERED)
        self._finish(key, receipt)
        self._record(context, receipt)
        return receipt

    async def fail_background_once(
        self,
        client: Any,
        error: BaseException,
        *,
        surface: str,
        guild_id: int,
        event_id: int,
        actor_id: int | None,
        revision: str,
    ) -> InteractionFailureReceipt:
        """応答先のないDiscord event失敗をexact bindingで一度だけ記録する。"""

        normalized_surface = _surface(surface)
        normalized_guild_id = _positive_id(guild_id)
        normalized_event_id = _positive_id(event_id)
        normalized_actor_id = _positive_id(actor_id)
        if normalized_guild_id is None:
            raise ValueError("guild_id must be positive")
        if normalized_event_id is None:
            raise ValueError("event_id must be positive")
        if actor_id is not None and normalized_actor_id is None:
            raise ValueError("actor_id must be positive when provided")
        if not isinstance(revision, str) or not _BACKGROUND_REVISION_PATTERN.fullmatch(revision):
            raise ValueError("revision must be a sha256 hex digest")

        failure = classify_failure(error)
        created = InteractionFailureReceipt(
            error_code=f"discord.{failure.kind.value}",
            reference_id=f"ERR-{secrets.token_hex(6).upper()}",
            error_type=_error_type(failure.error_type),
            surface=normalized_surface,
            retryable=failure.retryable,
            partial_response=False,
            delivery=InteractionFailureDelivery.UNCERTAIN,
        )
        key = _BackgroundFailureKey(
            surface=normalized_surface,
            guild_id=normalized_guild_id,
            event_id=normalized_event_id,
            actor_id=normalized_actor_id,
            revision=revision,
        )
        with self._lock:
            existing = self._background_receipts.get(key)
            if existing is not None:
                self._background_receipts.move_to_end(key)
                return existing
            self._background_receipts[key] = created
            while len(self._background_receipts) > self._maximum_entries:
                self._background_receipts.popitem(last=False)
        self._record(client, created, background_key=key)
        return created

    def receipt_for(self, *, surface: str, interaction_id: int) -> InteractionFailureReceipt | None:
        normalized_surface = _surface(surface)
        normalized_id = _positive_id(interaction_id)
        if normalized_id is None:
            raise ValueError("interaction_id must be positive")
        with self._lock:
            entry = self._receipts.get((normalized_surface, normalized_id))
            return entry.receipt if entry is not None else None

    def _finish(self, key: tuple[str, int], receipt: InteractionFailureReceipt) -> None:
        with self._lock:
            entry = self._receipts.get(key)
            if entry is None:
                return
            entry.receipt = receipt
            if not entry.completed.done():
                entry.completed.set_result(receipt)
            self._receipts.move_to_end(key)
            self._prune_terminal_locked(maximum_size=self._maximum_entries)

    def _prune_terminal_locked(self, *, maximum_size: int) -> None:
        while len(self._receipts) > maximum_size:
            terminal_key = next(
                (
                    key
                    for key, entry in self._receipts.items()
                    if entry.receipt.delivery is not InteractionFailureDelivery.PENDING
                ),
                None,
            )
            if terminal_key is None:
                return
            self._receipts.pop(terminal_key)

    @staticmethod
    def _record(
        subject: Any,
        receipt: InteractionFailureReceipt,
        *,
        background_key: _BackgroundFailureKey | None = None,
    ) -> None:
        safe_fields = {
            "error_code": receipt.error_code,
            "reference_id": receipt.reference_id,
            "error_type": receipt.error_type,
            "surface": receipt.surface,
            "retryable": receipt.retryable,
            "partial_response": receipt.partial_response,
            "delivery": receipt.delivery.value,
        }
        logger.error(
            "discord_background_delivery_failed" if background_key is not None else "discord_interaction_failed",
            extra=safe_fields,
        )

        client = (
            subject if background_key is not None else getattr(subject, "client", None) or getattr(subject, "bot", None)
        )
        database = getattr(client, "database", None)
        append_audit = getattr(database, "append_audit", None)
        if not callable(append_audit):
            return
        if background_key is not None:
            actor_id = background_key.actor_id
            guild_id = background_key.guild_id
            interaction_id = background_key.event_id
        else:
            actor = getattr(subject, "user", None) or getattr(subject, "author", None)
            actor_id = _positive_id(getattr(actor, "id", None))
            guild_id = _positive_id(getattr(subject, "guild_id", None))
            if guild_id is None:
                guild_id = _positive_id(getattr(getattr(subject, "guild", None), "id", None))
            interaction_id = _positive_id(getattr(subject, "id", None))
            if interaction_id is None:
                interaction_id = _positive_id(getattr(getattr(subject, "message", None), "id", None))
        details = dict(safe_fields)
        if interaction_id is not None:
            details["event_id" if background_key is not None else "interaction_id"] = str(interaction_id)
        try:
            result = append_audit(
                "operations.failure",
                actor_id=actor_id,
                guild_id=guild_id,
                details=details,
            )
            if inspect.iscoroutine(result):
                result.close()
                logger.error(
                    "discord_interaction_failure_audit_rejected",
                    extra={"reference_id": receipt.reference_id, "reason": "async_audit_not_supported"},
                )
            elif isinstance(result, asyncio.Future):
                result.cancel()
                logger.error(
                    "discord_interaction_failure_audit_rejected",
                    extra={"reference_id": receipt.reference_id, "reason": "async_audit_not_supported"},
                )
        except Exception as exc:
            logger.error(
                (
                    "discord_background_failure_audit_failed"
                    if background_key is not None
                    else "discord_interaction_failure_audit_failed"
                ),
                extra={"reference_id": receipt.reference_id, "error_type": type(exc).__name__},
            )


async def _deliver_failure(
    interaction: Any,
    response: Any,
    response_done: bool,
    response_type: object,
    message: str,
) -> bool:
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if not response_done:
        sender = getattr(response, "send_message", None)
        if not callable(sender):
            raise RuntimeError("interaction response sender is unavailable")
        try:
            await sender(message, **kwargs)
        except discord.InteractionResponded:
            followup = getattr(interaction, "followup", None)
            followup_sender = getattr(followup, "send", None)
            if not callable(followup_sender):
                raise
            await followup_sender(message, **kwargs)
            current_type = getattr(response, "type", None)
            return current_type not in _DEFERRED_TYPES
        return False
    if response_type in _EDITABLE_DEFERRED_TYPES:
        editor = getattr(interaction, "edit_original_response", None)
        if not callable(editor):
            raise RuntimeError("deferred interaction editor is unavailable")
        await editor(content=message, allowed_mentions=discord.AllowedMentions.none())
        return False
    followup = getattr(interaction, "followup", None)
    sender = getattr(followup, "send", None)
    if not callable(sender):
        raise RuntimeError("interaction followup sender is unavailable")
    await sender(message, **kwargs)
    return response_type not in _DEFERRED_TYPES


def _response_is_done(response: Any) -> bool:
    checker = getattr(response, "is_done", None)
    if not callable(checker):
        return False
    try:
        return checker() is True
    except Exception:
        return True


def _surface(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if _SURFACE_PATTERN.fullmatch(normalized) else "discord.interaction"


def _error_type(value: object) -> str:
    name = str(value) if value is not None else "UnknownError"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", name):
        return name
    return "UnknownError"


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
