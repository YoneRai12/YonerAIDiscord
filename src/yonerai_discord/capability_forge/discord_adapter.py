"""Recipe Forge Stage 2c の Discord DM 境界。

この層は Stage 2b の digest/revision CAS を変えず、Discord 送信と button
ルーティングだけを担当する。Discord 側の message binding は durable ではない。
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.operations import InteractionFailureTerminal
from yonerai_discord.runtime_manifests.capability_forge import FORGE_OWNER_NOTIFICATION_CAPABILITY_ID

from .lifecycle import CandidateKind
from .owner_notification import (
    OwnerDecisionRequest,
    OwnerDecisionStatus,
    OwnerIdentity,
    OwnerNotificationCard,
)


NO_MENTIONS = discord.AllowedMentions.none()
_DYNAMIC_TEMPLATE = r"^cf1:(?P<action>[krp]):(?P<digest>[a-f0-9]{64}):(?P<revision>[1-9][0-9]{0,15})$"


class DiscordForgeAuthorizer:
    """設定・Discord application owner・現在の control plane を毎回照合する。"""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def resolve_current_bot_owner(self) -> OwnerIdentity | None:
        user = await self.resolve_current_owner_user()
        if user is None:
            return None
        return OwnerIdentity(int(user.id))

    async def resolve_current_owner_user(self) -> Any | None:
        owner_id = self._configured_owner_id()
        if owner_id is None or self._closing():
            return None
        user = await self._owner_user(owner_id)
        if user is None or getattr(user, "id", None) != owner_id:
            return None
        if not await self._discord_owner(user):
            return None
        if not self._capability_allowed(owner_id):
            return None
        return user

    async def authorize_interaction(self, interaction: discord.Interaction) -> bool:
        owner_id = self._configured_owner_id()
        user = getattr(interaction, "user", None)
        if owner_id is None or self._closing() or getattr(user, "id", None) != owner_id:
            return False
        if not await self._discord_owner(user):
            return False
        return self._capability_allowed(owner_id)

    def _configured_owner_id(self) -> int | None:
        settings = getattr(self._bot, "settings", None)
        owner_ids = getattr(settings, "bot_owner_ids", None)
        if not isinstance(owner_ids, (frozenset, set, tuple, list)) or len(owner_ids) != 1:
            return None
        owner_id = next(iter(owner_ids))
        if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
            return None
        return owner_id

    def _closing(self) -> bool:
        if bool(getattr(self._bot, "is_closing", False)):
            return True
        is_closed = getattr(self._bot, "is_closed", None)
        try:
            return bool(is_closed()) if callable(is_closed) else False
        except Exception:
            return True

    async def _owner_user(self, owner_id: int) -> Any | None:
        try:
            user = getattr(self._bot, "get_user", lambda _user_id: None)(owner_id)
            if user is not None:
                return user
            fetch_user = getattr(self._bot, "fetch_user", None)
            if not callable(fetch_user):
                return None
            return await fetch_user(owner_id)
        except Exception:
            return None

    async def _discord_owner(self, user: Any) -> bool:
        is_owner = getattr(self._bot, "is_owner", None)
        if not callable(is_owner):
            return False
        try:
            return (await is_owner(user)) is True
        except Exception:
            return False

    def _capability_allowed(self, owner_id: int) -> bool:
        guard = getattr(self._bot, "capability_guard", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        if not callable(currently_allowed):
            return False
        try:
            return (
                currently_allowed(
                    FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,
                    guild_id=None,
                    user_id=owner_id,
                    actor_level=RbacLevel.BOT_OWNER,
                    floor=RbacLevel.BOT_OWNER,
                )
                is True
            )
        except Exception:
            return False


class DiscordOwnerDmPort:
    """safe card のみを private owner DM に送る port。

    ``idempotency_key`` は Stage 2b から受け渡すが、Discord API に exactly-once
    保証はない。送信直前に認可を再評価する。
    """

    def __init__(self, authorizer: DiscordForgeAuthorizer) -> None:
        self._authorizer = authorizer

    async def send_private_owner_card(
        self,
        *,
        owner_user_id: int,
        card: OwnerNotificationCard,
        idempotency_key: str,
        allowed_mentions: tuple[()],
    ) -> None:
        expected_key = f"forge-owner-notification:v1:{card.recipe_digest}"
        if allowed_mentions != () or idempotency_key != expected_key:
            raise ValueError("owner DM contract is invalid")
        user = await self._authorizer.resolve_current_owner_user()
        if user is None or user.id != owner_user_id:
            raise PermissionError("owner authorization is unavailable")
        await user.send(
            _render_card(card),
            view=_card_view(card),
            allowed_mentions=NO_MENTIONS,
        )


class ForgeDecisionButton(discord.ui.DynamicItem[discord.ui.Button], template=_DYNAMIC_TEMPLATE):
    """再起動後にも strict custom ID だけを復元する persistent button。"""

    adapter: "DiscordForgeAdapter | None" = None

    def __init__(self, custom_id: str, label: str) -> None:
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.secondary, label=label, custom_id=custom_id))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: Any,
        /,
    ) -> "ForgeDecisionButton":
        custom_id = item.custom_id
        actor_id = getattr(getattr(interaction, "user", None), "id", None)
        if not isinstance(custom_id, str) or isinstance(actor_id, bool) or not isinstance(actor_id, int):
            raise ValueError("invalid Forge decision routing")
        request = OwnerDecisionRequest.from_custom_id(custom_id=custom_id, actor_user_id=actor_id)
        return cls(custom_id, _button_label(request.action.value))

    async def callback(self, interaction: discord.Interaction) -> None:
        adapter = type(self).adapter
        if adapter is None:
            await _reply(interaction, "この review は現在利用できません。")
            return
        try:
            await adapter.apply_interaction(interaction, self.item.custom_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            client = getattr(interaction, "client", None)
            expected_client = adapter._authorizer._bot
            terminal = getattr(client, "interaction_failure_terminal", None)
            if client is not expected_client or not isinstance(terminal, InteractionFailureTerminal):
                raise
            await terminal.fail_once(
                interaction,
                exc,
                surface="capability_forge_decision",
            )


class DiscordForgeAdapter:
    def __init__(self, *, authorizer: DiscordForgeAuthorizer, service: Any) -> None:
        self._authorizer = authorizer
        self._service = service
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True

    async def apply_interaction(self, interaction: discord.Interaction, custom_id: str | None) -> None:
        actor_id = getattr(getattr(interaction, "user", None), "id", None)
        if (
            self._closing
            or not isinstance(custom_id, str)
            or isinstance(actor_id, bool)
            or not isinstance(actor_id, int)
        ):
            await _reply(interaction, "この review は現在利用できません。")
            return
        try:
            request = OwnerDecisionRequest.from_custom_id(custom_id=custom_id, actor_user_id=actor_id)
        except ValueError:
            await _reply(interaction, "review 操作が無効です。")
            return
        # lifecycle CAS の直前にも所有者・停止・control plane を fresh に確認する。
        if not await self._authorizer.authorize_interaction(interaction):
            await _reply(interaction, "この review を実行する権限がありません。")
            return
        receipt = await self._service.apply_owner_decision(request)
        if receipt.status is OwnerDecisionStatus.APPLIED:
            await _reply(
                interaction,
                "review を記録しました。これは SQLite 上の proposal のみで、公開・runtime 登録は行いません。",
            )
        elif receipt.status is OwnerDecisionStatus.STALE:
            await _reply(interaction, "この review は既に処理済みか、更新されています。")
        else:
            await _reply(interaction, "review を記録できませんでした。")


def _card_view(card: OwnerNotificationCard) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for button in card.actions:
        view.add_item(ForgeDecisionButton(button.custom_id, _button_label(button.action.value)))
    return view


def _button_label(action: str) -> str:
    return {"keep": "Keep", "reject": "Reject", "promote_requested": "Promote request"}.get(action, "Review")


def _render_card(card: OwnerNotificationCard) -> str:
    templates = ", ".join(f"{item.primitive_id}@{item.revision}" for item in card.templates)
    return "\n".join(
        (
            "Recipe Forge review (proposal only)",
            f"Kind: {_candidate_kind_label(card.candidate_kind)}",
            f"Digest: `{card.recipe_digest}`",
            f"Description: {card.code_owned_description}",
            f"Templates: {templates}",
            "公開・module ON・manifest/source/runtime 登録はこの操作では行いません。",
        )
    )


def _candidate_kind_label(candidate_kind: CandidateKind) -> str:
    labels = {
        CandidateKind.SEALED_RECIPE: "Sealed recipe",
        CandidateKind.SANDBOX_PYTHON_PURE: "Sandbox Python pure",
        CandidateKind.SANDBOX_BROWSER_READONLY: "Sandbox browser read-only",
    }
    try:
        return labels[candidate_kind]
    except (KeyError, TypeError) as exc:
        raise ValueError("candidate kind is invalid") from exc


async def _reply(interaction: discord.Interaction, message: str) -> None:
    kwargs = {"ephemeral": True, "allowed_mentions": NO_MENTIONS}
    if interaction.response.is_done():
        await interaction.followup.send(message, **kwargs)
    else:
        await interaction.response.send_message(message, **kwargs)


__all__ = [
    "DiscordForgeAdapter",
    "DiscordForgeAuthorizer",
    "DiscordOwnerDmPort",
    "FORGE_OWNER_NOTIFICATION_CAPABILITY_ID",
    "ForgeDecisionButton",
]
