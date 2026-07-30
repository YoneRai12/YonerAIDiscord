"""Discord 上で外部 AI 送信の本人同意を受ける短命 View。

元の本文や添付は保持せず、guild / channel / user / source message の
ID scope だけを process memory 上に保持する。外部送信の可否は、確定時に
listener 側が再評価できるよう async callback へ委譲する。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Protocol

import discord


logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
DEFAULT_CONSENT_VIEW_TIMEOUT_SECONDS = 120.0

_CONFIRM_CUSTOM_ID = "ai:remote-consent:confirm"
_CANCEL_CUSTOM_ID = "ai:remote-consent:cancel"
_WRONG_SCOPE_REPLY = "この同意操作は、元のメッセージを送った本人だけが使えます。"
_ALREADY_FINISHED_REPLY = "この同意操作はすでに終了しています。"
_CONFIRMED_REPLY = "同意を確認しました。元のメッセージを処理します。"
_CONFIRM_FAILED_REPLY = "現在の権限または設定を確認できなかったため、外部AIには送信しませんでした。"
_PROMPT_UPDATE_FAILED_REPLY = "同意画面を安全に終了できなかったため、外部AIには送信しませんでした。"
_CANCELLED_REPLY = "送信しませんでした。"


@dataclass(frozen=True, slots=True)
class RemoteConsentScope:
    """同意を適用できる Discord event scope。"""

    guild_id: int | None
    channel_id: int
    user_id: int
    source_message_id: int

    def __post_init__(self) -> None:
        guild_id = self.guild_id
        if guild_id is not None and (isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0):
            raise ValueError("guild_id must be a positive integer or None for a DM")
        for name in ("channel_id", "user_id", "source_message_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class RemoteConsentTerminalState(StrEnum):
    """View の one-shot 終端状態。"""

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"
    FAILED_CLOSED = "failed_closed"


class RemoteConsentConfirmCallback(Protocol):
    """確定直前の policy 再評価と listener 再開を担う callback。"""

    def __call__(
        self,
        interaction: discord.Interaction,
        scope: RemoteConsentScope,
    ) -> Awaitable[bool]: ...


class RemoteConsentTerminalCallback(Protocol):
    """listener が pending View を廃棄するための終端通知。"""

    def __call__(
        self,
        view: RemoteConsentView,
        state: RemoteConsentTerminalState,
    ) -> Awaitable[None] | None: ...


class _ConfirmButton(discord.ui.Button["RemoteConsentView"]):
    def __init__(self) -> None:
        super().__init__(
            label="同意して外部AIへ送信",
            style=discord.ButtonStyle.success,
            custom_id=_CONFIRM_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.confirm(interaction)


class _CancelButton(discord.ui.Button["RemoteConsentView"]):
    def __init__(self) -> None:
        super().__init__(
            label="送信しない",
            style=discord.ButtonStyle.secondary,
            custom_id=_CANCEL_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.cancel(interaction)


class RemoteConsentView(discord.ui.View):
    """本人と event scope に結び付いた、非永続の外部 AI 同意 View。"""

    def __init__(
        self,
        scope: RemoteConsentScope,
        on_confirm: RemoteConsentConfirmCallback,
        *,
        on_terminal: RemoteConsentTerminalCallback | None = None,
        timeout: float = DEFAULT_CONSENT_VIEW_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(on_confirm):
            raise TypeError("on_confirm must be callable")
        if on_terminal is not None and not callable(on_terminal):
            raise TypeError("on_terminal must be callable")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 30 <= timeout <= 300:
            raise ValueError("timeout must be between 30 and 300 seconds")
        super().__init__(timeout=float(timeout))
        self.scope = scope
        self._on_confirm = on_confirm
        self._on_terminal = on_terminal
        self._decision_lock = asyncio.Lock()
        self._terminal_state: RemoteConsentTerminalState | None = None
        self._terminal_notified = False
        self._prompt_message: discord.PartialMessage | None = None
        self._prompt_message_id: int | None = None
        self.add_item(_ConfirmButton())
        self.add_item(_CancelButton())

    @property
    def terminal_state(self) -> RemoteConsentTerminalState | None:
        return self._terminal_state

    @property
    def consumed(self) -> bool:
        return self._terminal_state is not None

    def bind_prompt_message(self, message: discord.Message) -> None:
        """promptのID-only PartialMessageを一度だけViewに結び付ける。"""

        message_id = getattr(message, "id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("prompt message must have a positive integer id")
        if self._prompt_message_id is not None:
            if self._prompt_message_id == message_id:
                return
            raise RuntimeError("prompt message is already bound")

        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel = getattr(message, "channel", None)
        channel_id = getattr(channel, "id", None)
        if guild_id != self.scope.guild_id:
            raise ValueError("prompt message guild does not match consent scope")
        if channel_id is not None and channel_id != self.scope.channel_id:
            raise ValueError("prompt message channel does not match consent scope")
        get_partial_message = getattr(channel, "get_partial_message", None)
        if not callable(get_partial_message):
            raise ValueError("prompt channel cannot create an ID-only partial message")
        try:
            partial_message = get_partial_message(message_id)
        except Exception as exc:
            raise ValueError("prompt channel failed to create an ID-only partial message") from exc
        if partial_message is message:
            raise ValueError("prompt channel returned the full message instead of an ID-only partial message")
        if (
            getattr(partial_message, "id", None) != message_id
            or getattr(getattr(partial_message, "channel", None), "id", None) != self.scope.channel_id
            or not callable(getattr(partial_message, "edit", None))
        ):
            raise ValueError("prompt channel returned an invalid partial message")

        self._prompt_message = partial_message
        self._prompt_message_id = message_id

    async def refresh_prompt_state(self) -> bool:
        """保持しているID-only PartialMessageに現在のbutton状態を再同期する。"""

        async with self._decision_lock:
            return await self._edit_prompt()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._matches_scope(interaction):
            return True
        await _send_ephemeral(interaction, _WRONG_SCOPE_REPLY)
        return False

    async def confirm(self, interaction: discord.Interaction) -> None:
        """本人からの確定を one-shot で受け、listener callback へ委譲する。"""

        if not await self._ensure_scope(interaction):
            return
        await _defer_ephemeral(interaction)

        async with self._decision_lock:
            if self.consumed:
                already_finished = True
                prompt_updated = False
            else:
                already_finished = False
                self._terminal_state = RemoteConsentTerminalState.CONFIRMED
                self._disable_buttons()
                self.stop()
                prompt_updated = await self._edit_prompt()
                if not prompt_updated:
                    self._terminal_state = RemoteConsentTerminalState.FAILED_CLOSED

        if already_finished:
            await _send_ephemeral(interaction, _ALREADY_FINISHED_REPLY)
            return
        if not prompt_updated:
            await self._notify_terminal_once()
            await _send_ephemeral(interaction, _PROMPT_UPDATE_FAILED_REPLY)
            return

        accepted = False
        try:
            accepted = bool(await self._on_confirm(interaction, self.scope))
        except Exception:
            # callback 例外に元本文が含まれる可能性があるため、例外本文は記録しない。
            logger.error("remote_consent_confirm_callback_failed")
        if not accepted:
            self._terminal_state = RemoteConsentTerminalState.FAILED_CLOSED
        else:
            # Keep the disabled prompt visible while the original request is running,
            # then remove it once the answer has been delivered successfully.
            await self._delete_prompt()

        await self._notify_terminal_once()
        await _send_ephemeral(interaction, _CONFIRMED_REPLY if accepted else _CONFIRM_FAILED_REPLY)

    async def cancel(self, interaction: discord.Interaction) -> None:
        """外部送信を行わず View を one-shot 終了する。"""

        if not await self._ensure_scope(interaction):
            return
        await _defer_ephemeral(interaction)

        async with self._decision_lock:
            if self.consumed:
                already_finished = True
            else:
                already_finished = False
                self._terminal_state = RemoteConsentTerminalState.CANCELLED
                self._disable_buttons()
                self.stop()
                await self._edit_prompt()

        if already_finished:
            await _send_ephemeral(interaction, _ALREADY_FINISHED_REPLY)
            return
        await self._delete_prompt()
        await self._notify_terminal_once()
        await _send_ephemeral(interaction, _CANCELLED_REPLY)

    async def close(
        self,
        *,
        state: RemoteConsentTerminalState = RemoteConsentTerminalState.CLOSED,
    ) -> None:
        """shutdown や timeout 時に外部送信なしで幂等に閉じる。"""

        if state not in {RemoteConsentTerminalState.CLOSED, RemoteConsentTerminalState.TIMED_OUT}:
            raise ValueError("close state must be CLOSED or TIMED_OUT")
        async with self._decision_lock:
            if self.consumed:
                return
            self._terminal_state = state
            self._disable_buttons()
            self.stop()
            await self._edit_prompt()
        await self._notify_terminal_once()

    async def on_timeout(self) -> None:
        await self.close(state=RemoteConsentTerminalState.TIMED_OUT)

    async def _ensure_scope(self, interaction: discord.Interaction) -> bool:
        if self._matches_scope(interaction):
            return True
        await _send_ephemeral(interaction, _WRONG_SCOPE_REPLY)
        return False

    def _matches_scope(self, interaction: discord.Interaction) -> bool:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        message_id = getattr(getattr(interaction, "message", None), "id", None)
        return (
            user_id == self.scope.user_id
            and getattr(interaction, "guild_id", None) == self.scope.guild_id
            and getattr(interaction, "channel_id", None) == self.scope.channel_id
            and self._prompt_message_id is not None
            and message_id == self._prompt_message_id
        )

    def _disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _edit_prompt(self) -> bool:
        message = self._prompt_message
        if message is None:
            logger.warning("remote_consent_prompt_not_bound")
            return False
        try:
            await message.edit(view=self)
        except Exception as exc:
            logger.warning(
                "remote_consent_prompt_edit_failed",
                extra={"error_type": type(exc).__name__},
            )
            return False
        return True

    async def _delete_prompt(self) -> bool:
        message = self._prompt_message
        if message is None:
            return False
        delete = getattr(message, "delete", None)
        if not callable(delete):
            logger.warning("remote_consent_prompt_delete_unavailable")
            return False
        try:
            await delete()
        except Exception as exc:
            logger.warning(
                "remote_consent_prompt_delete_failed",
                extra={"error_type": type(exc).__name__},
            )
            return False
        self._prompt_message = None
        return True

    async def _notify_terminal_once(self) -> None:
        if self._terminal_notified:
            return
        self._terminal_notified = True
        callback = self._on_terminal
        state = self._terminal_state
        if callback is None or state is None:
            return
        try:
            result = callback(self, state)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.error("remote_consent_terminal_callback_failed")


def _response_is_done(response: object) -> bool:
    is_done = getattr(response, "is_done", None)
    if not callable(is_done):
        return False
    try:
        return bool(is_done())
    except Exception:
        return True


async def _defer_ephemeral(interaction: discord.Interaction) -> None:
    response = interaction.response
    if not _response_is_done(response):
        await response.defer(ephemeral=True, thinking=True)


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    response = interaction.response
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": NO_MENTIONS,
    }
    if not _response_is_done(response):
        await response.send_message(content, **kwargs)
        return
    await interaction.followup.send(content, **kwargs)


__all__ = [
    "DEFAULT_CONSENT_VIEW_TIMEOUT_SECONDS",
    "RemoteConsentConfirmCallback",
    "RemoteConsentScope",
    "RemoteConsentTerminalCallback",
    "RemoteConsentTerminalState",
    "RemoteConsentView",
]
