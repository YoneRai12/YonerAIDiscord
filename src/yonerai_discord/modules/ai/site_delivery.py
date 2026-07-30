"""AIのHTML成果物を、権限付きサイト公開サービスへ接続するDiscord adapter。"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord

from yonerai_discord.capabilities import SITE_AUTO_PUBLISH_CAPABILITY_ID
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.discord_policy import determine_rbac_level
from yonerai_discord.modules.site_publish.domain import ActorRole, DiscordMessageBinding, SiteActor, SiteVisibility

from .orchestration_planner import OrchestrationPlanner


logger = logging.getLogger(__name__)

SITE_AUTO_PUBLISH_SURFACE = "site_auto_publish"

_SITE_NOUN = r"(?:web\s*site|website|web\s*page|site|ホームページ|ウェブサイト|webサイト|サイト|ページ)"
_JAPANESE_SITE_CREATE_COMMAND_RE = re.compile(
    rf"(?=[^。.!！?？\r\n]{{0,240}}{_SITE_NOUN})"
    r"[^。.!！?？\r\n]{1,240}(?:作って|つくって|作成(?:して)?|構築(?:して)?|公開(?:して)?|"
    r"デプロイ(?:して)?)(?:ください|くれる|ほしい)?(?:[。.!！])?\Z",
    re.IGNORECASE,
)
_ENGLISH_SITE_CREATE_COMMAND_RE = re.compile(
    r"(?:please\s+)?(?:build|create|make|publish|deploy)\b"
    r"(?=[^?\r\n]{0,240}\b(?:web\s*site|website|web\s*page|site)\b)[^?\r\n]{1,240}(?:[.!])?\Z",
    re.IGNORECASE,
)
_JAPANESE_SITE_EDIT_COMMAND_RE = re.compile(
    r"(?:"
    r"[^。.!！?？\r\n]{1,160}を[^。.!！?？\r\n]{1,80}に(?:して)?"
    r"|[^。.!！?？\r\n]{0,200}(?:(?:追加|削除|変更|修正|更新|置換)(?:して)?|変えて)"
    r"(?:ください|くれる|ほしい|お願い)?"
    r")(?:[。.!！])?\Z"
)
_ENGLISH_SITE_EDIT_COMMAND_RE = re.compile(
    r"(?:please\s+)?(?:add|edit|update|change|replace|remove|delete)\b[^?\r\n]{0,240}(?:[.!])?\Z",
    re.IGNORECASE,
)
_TITLE = re.compile(r"<title\b[^>]*>(?P<title>.*?)</title\s*>", re.IGNORECASE | re.DOTALL)

STRICT_STATIC_SITE_GUIDANCE = (
    "この依頼は安全な静的サイトとして公開されます。回答には完全なHTML文書を1つ含めてください。"
    "CSSはstyle要素へ内包し、script、iframe、form、service worker、外部URLへの通信、外部画像・外部font、"
    "meta refresh、javascript: URLは使わないでください。既存サイトの編集なら、差分だけでなく編集後の完全なHTMLを返してください。"
)


@dataclass(frozen=True, slots=True)
class SiteEditTarget:
    site_id: str
    release_id: str
    slug: str


@dataclass(frozen=True, slots=True)
class PublishedSite:
    site_id: str
    release_id: str
    slug: str
    site_url: str
    revision: int
    visibility: str
    updated: bool


@dataclass(frozen=True, slots=True)
class SiteDeliveryAttempt:
    requested: bool
    published: PublishedSite | None = None
    notice: str = ""


class DiscordAISiteDelivery:
    """Discordの事実を正規化し、site_publish serviceへ限定的に委譲する。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def resolve_edit_target(self, message: Any) -> SiteEditTarget | None:
        service = getattr(self.bot, "site_publish_service", None)
        guard = getattr(self.bot, "capability_guard", None)
        reference = getattr(message, "reference", None)
        message_id = getattr(reference, "message_id", None)
        reference_channel_id = getattr(reference, "channel_id", None)
        channel = getattr(message, "channel", None)
        channel_id = getattr(channel, "id", None)
        if (
            service is None
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
            or not isinstance(reference_channel_id, int)
            or isinstance(reference_channel_id, bool)
            or reference_channel_id <= 0
            or not isinstance(channel_id, int)
            or isinstance(channel_id, bool)
            or channel_id <= 0
            or reference_channel_id != channel_id
        ):
            return None
        actor = await self._actor(message)
        if actor is None:
            return None
        try:
            resolved = await asyncio.to_thread(service.site_for_discord_message, actor, message_id)
        except Exception as exc:
            logger.info("site_edit_target_unavailable", extra={"error_type": type(exc).__name__})
            return None
        if resolved is None:
            return None
        binding, site, release = resolved

        referenced_message = getattr(reference, "resolved", None)
        if referenced_message is None:
            referenced_message = getattr(reference, "cached_message", None)
        stale_reason: str | None = None
        if isinstance(referenced_message, discord.DeletedReferencedMessage):
            stale_reason = "deleted"
        elif referenced_message is None:
            fetch_message = getattr(channel, "fetch_message", None)
            if not callable(fetch_message):
                return None
            try:
                referenced_message = await fetch_message(message_id)
            except discord.NotFound:
                stale_reason = "deleted"
            except discord.Forbidden:
                stale_reason = "forbidden"
            except discord.HTTPException:
                return None
            except Exception:
                return None
        if stale_reason is not None:
            await self._remove_stale_binding(
                message,
                service=service,
                guard=guard,
                actor=actor,
                binding=binding,
                reason=stale_reason,
            )
            return None
        if referenced_message is not None and not _same_referenced_message(
            message,
            referenced_message,
            message_id=message_id,
        ):
            return None
        if getattr(self.bot, "site_publish_service", None) is not service:
            return None
        return SiteEditTarget(site_id=site.site_id, release_id=release.release_id, slug=site.slug)

    async def _remove_stale_binding(
        self,
        message: Any,
        *,
        service: Any,
        guard: Any,
        actor: SiteActor,
        binding: DiscordMessageBinding,
        reason: str,
    ) -> None:
        current = await self._actor_and_level(message, fresh=True)
        if current is None:
            return
        current_actor, current_level = current
        if (
            getattr(self.bot, "site_publish_service", None) is not service
            or getattr(self.bot, "capability_guard", None) is not guard
            or current_actor.actor_id != actor.actor_id
            or current_actor.guild_id != actor.guild_id
            or not self._currently_allowed(message, current_level)
        ):
            return

        def remove_if_current() -> bool:
            if (
                getattr(self.bot, "site_publish_service", None) is not service
                or getattr(self.bot, "capability_guard", None) is not guard
                or not self._currently_allowed(message, current_level)
            ):
                return False
            return bool(
                service.remove_discord_message_binding_if_current(
                    current_actor,
                    binding,
                    reason=reason,
                )
            )

        try:
            await asyncio.to_thread(remove_if_current)
        except Exception as exc:
            logger.info("site_stale_binding_cleanup_failed", extra={"error_type": type(exc).__name__})

    @staticmethod
    def explicitly_requested(prompt: str) -> bool:
        actionable = OrchestrationPlanner.actionable_instruction(prompt)
        if not actionable or not OrchestrationPlanner.instruction_requests_actions(prompt):
            return False
        return bool(
            _JAPANESE_SITE_CREATE_COMMAND_RE.fullmatch(actionable)
            or _ENGLISH_SITE_CREATE_COMMAND_RE.fullmatch(actionable)
        )

    @classmethod
    def wants_delivery(cls, prompt: str, target: SiteEditTarget | None) -> bool:
        if target is None:
            return cls.explicitly_requested(prompt)
        actionable = OrchestrationPlanner.actionable_instruction(prompt)
        if not actionable or not OrchestrationPlanner.instruction_requests_actions(prompt):
            return False
        return bool(
            _JAPANESE_SITE_EDIT_COMMAND_RE.fullmatch(actionable) or _ENGLISH_SITE_EDIT_COMMAND_RE.fullmatch(actionable)
        )

    async def deliver(
        self,
        message: Any,
        *,
        prompt: str,
        html: str | None,
        target: SiteEditTarget | None,
        authorization_current: Callable[[], bool] | None = None,
    ) -> SiteDeliveryAttempt:
        requested = self.wants_delivery(prompt, target)
        if not requested:
            return SiteDeliveryAttempt(False)
        if not _authorization_current(authorization_current):
            return SiteDeliveryAttempt(
                True,
                notice="待機中に権限または機能設定が変更されたため、外部公開を中止しました。",
            )
        if html is None:
            return SiteDeliveryAttempt(
                True,
                notice="完全なHTML文書を生成できなかったため、外部公開は行いませんでした。",
            )

        settings = getattr(self.bot, "settings", None)
        service = getattr(self.bot, "site_publish_service", None)
        if (
            settings is None
            or service is None
            or not bool(getattr(settings, "site_publish_enabled", False))
            or not bool(getattr(settings, "site_publish_auto_enabled", False))
            or not bool(getattr(settings, "site_publish_allow_remote", False))
        ):
            return SiteDeliveryAttempt(
                True,
                notice="専用サイト公開基盤がまだ有効ではないため、HTMLファイルだけを添付しました。",
            )

        actor_and_level = await self._actor_and_level(message)
        if actor_and_level is None:
            return SiteDeliveryAttempt(True, notice="本人・サーバー情報を確認できないため公開しませんでした。")
        actor, actor_level = actor_and_level
        if not self._event_allowed(message, actor_level):
            return SiteDeliveryAttempt(
                True,
                notice="このサーバーまたは利用者にはサイト公開権限がありません。HTMLファイルだけを添付しました。",
            )

        refresh = getattr(self.bot, "site_publish_refresh", None)
        if callable(refresh):
            try:
                await refresh()
            except Exception as exc:
                logger.info("site_auto_publish_readiness_refresh_failed", extra={"error_type": type(exc).__name__})
                return SiteDeliveryAttempt(
                    True,
                    notice="サイト公開の接続確認に失敗したため、外部公開を中止しました。HTMLファイルだけを返します。",
                )
            status = getattr(self.bot, "site_publish_status", None)
            if not bool(getattr(status, "ready", False)):
                return SiteDeliveryAttempt(
                    True,
                    notice="サイト公開先がまだ利用可能ではないため、外部公開を中止しました。HTMLファイルだけを返します。",
                )

        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            return SiteDeliveryAttempt(True, notice="DiscordイベントIDを確認できないため公開しませんでした。")

        async def mutation_commit_check() -> bool:
            if not _authorization_current(authorization_current):
                return False
            current = await self._actor_and_level(message, fresh=True)
            if current is None:
                return False
            if getattr(self.bot, "site_publish_service", None) is not service:
                return False
            if not _authorization_current(authorization_current):
                return False
            current_actor, current_level = current
            if current_actor.actor_id != actor.actor_id or _actor_role_rank(current_actor.role) < _actor_role_rank(
                actor.role
            ):
                return False
            if not self._currently_allowed(message, current_level):
                return False
            if target is None:
                return True
            try:
                service.get_site(current_actor, target.site_id)
            except Exception:
                return False
            return True

        try:
            if target is None:
                display_name = _display_name(html, prompt)
                site, release = await service.create_site_authorized(
                    actor,
                    display_name=display_name,
                    html=html,
                    idempotency_key=f"discord:{message_id}:site-create",
                    commit_check=mutation_commit_check,
                    visibility=SiteVisibility.UNLISTED,
                )
                updated = False
            else:
                site_before_reconcile = service.get_site(actor, target.site_id)
                target_release = service.repository.get_release(target.release_id)
                if target_release is None or target_release.site_id != target.site_id:
                    raise RuntimeError("site edit target release is unavailable")
                if site_before_reconcile.active_release_id != target.release_id:
                    raise RuntimeError("site edit target is no longer the active release")

                async def reconcile_commit_check(_site: Any, _release: Any) -> bool:
                    return await mutation_commit_check()

                await service.reconcile_uncertain(
                    actor,
                    target.site_id,
                    commit_check=reconcile_commit_check,
                )
                site = service.get_site(actor, target.site_id)
                if site.active_release_id is None:
                    raise RuntimeError("site has no active release after reconciliation")
                parent_release = service.repository.get_release(site.active_release_id)
                if parent_release is None or parent_release.site_id != target.site_id:
                    raise RuntimeError("active site release disappeared after reconciliation")

                async def update_commit_check() -> bool:
                    if not await mutation_commit_check():
                        return False
                    current_site = service.get_site(actor, target.site_id)
                    return current_site.active_release_id == parent_release.release_id

                release = await service.update_site_authorized(
                    actor,
                    target.site_id,
                    html=html,
                    idempotency_key=f"discord:{message_id}:site-update",
                    commit_check=update_commit_check,
                    parent_release_id=parent_release.release_id,
                )
                updated = True

            async def publish_commit_check(current_site: Any, current_release: Any) -> bool:
                if current_site.active_release_id != current_release.parent_release_id:
                    return False
                return await mutation_commit_check()

            receipt, _activation = await service.publish_site(
                actor,
                site.site_id,
                release.release_id,
                idempotency_key=f"discord:{message_id}:site-activate",
                commit_check=publish_commit_check,
                reason="Discord AI generated site update" if updated else "Discord AI generated site creation",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("site_auto_publish_failed", extra={"error_type": type(exc).__name__})
            return SiteDeliveryAttempt(
                True,
                notice="公開前の安全検査または配信処理に失敗したため、外部URLは作らずHTMLファイルだけを添付しました。",
            )

        return SiteDeliveryAttempt(
            True,
            published=PublishedSite(
                site_id=site.site_id,
                release_id=release.release_id,
                slug=site.slug,
                site_url=receipt.site_url,
                revision=release.revision,
                visibility=release.visibility.value,
                updated=updated,
            ),
        )

    async def bind_response(
        self,
        source_message: Any,
        response_message: Any,
        published: PublishedSite,
        *,
        authorization_current: Callable[[], bool] | None = None,
    ) -> None:
        response_id = getattr(response_message, "id", None)
        service = getattr(self.bot, "site_publish_service", None)
        if (
            service is None
            or not isinstance(response_id, int)
            or response_id <= 0
            or not _same_discord_scope(source_message, response_message)
            or not _authorization_current(authorization_current)
        ):
            return
        current = await self._actor_and_level(source_message, fresh=True)
        if current is None:
            return
        actor, actor_level = current
        if (
            getattr(self.bot, "site_publish_service", None) is not service
            or not _authorization_current(authorization_current)
            or not self._currently_allowed(source_message, actor_level)
            or not _same_discord_scope(source_message, response_message)
        ):
            return

        def bind_if_current() -> bool:
            if (
                getattr(self.bot, "site_publish_service", None) is not service
                or not _authorization_current(authorization_current)
                or not self._currently_allowed(source_message, actor_level)
                or not _same_discord_scope(source_message, response_message)
            ):
                return False
            service.bind_discord_message(
                actor,
                message_id=response_id,
                site_id=published.site_id,
                release_id=published.release_id,
            )
            return True

        try:
            await asyncio.to_thread(bind_if_current)
        except Exception as exc:
            logger.warning("site_discord_binding_failed", extra={"error_type": type(exc).__name__})

    async def _actor(self, message: Any, *, fresh: bool = False) -> SiteActor | None:
        resolved = await self._actor_and_level(message, fresh=fresh)
        return None if resolved is None else resolved[0]

    async def _actor_and_level(
        self,
        message: Any,
        *,
        fresh: bool = False,
    ) -> tuple[SiteActor, RbacLevel] | None:
        guild = getattr(message, "guild", None)
        member = getattr(message, "author", None)
        settings = getattr(self.bot, "settings", None)
        if guild is None or member is None or settings is None:
            return None
        guild_id = getattr(guild, "id", None)
        user_id = getattr(member, "id", None)
        if not isinstance(guild_id, int) or guild_id <= 0 or not isinstance(user_id, int) or user_id <= 0:
            return None
        if fresh:
            fetch_member = getattr(guild, "fetch_member", None)
            if not callable(fetch_member):
                return None
            try:
                member = await fetch_member(user_id)
            except Exception:
                return None
            if getattr(member, "id", None) != user_id:
                return None
        application_owner = False
        is_owner = getattr(self.bot, "is_owner", None)
        if callable(is_owner):
            try:
                application_owner = bool(await is_owner(member))
            except Exception:
                application_owner = False
        roles = getattr(member, "roles", ()) or ()
        role_ids = frozenset(
            int(role.id) for role in roles if isinstance(getattr(role, "id", None), int) and int(role.id) > 0
        )
        try:
            level = determine_rbac_level(
                user_id=user_id,
                guild_owner_id=(int(guild.owner_id) if isinstance(getattr(guild, "owner_id", None), int) else None),
                permissions=getattr(member, "guild_permissions", None),
                role_ids=role_ids,
                settings=settings,
                application_owner=application_owner,
            )
        except (AttributeError, TypeError, ValueError):
            return None
        role = (
            ActorRole.BOT_OWNER
            if level >= RbacLevel.BOT_OWNER
            else ActorRole.GUILD_ADMIN
            if level >= RbacLevel.GUILD_ADMIN
            else ActorRole.MEMBER
        )
        return SiteActor(user_id, guild_id, role), level

    def _event_allowed(self, message: Any, actor_level: RbacLevel) -> bool:
        guard = getattr(self.bot, "capability_guard", None)
        checker = getattr(guard, "event_allowed", None)
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        if not callable(checker) or guild is None or channel is None or author is None:
            return False
        try:
            return bool(
                checker(
                    SITE_AUTO_PUBLISH_CAPABILITY_ID,
                    surface=SITE_AUTO_PUBLISH_SURFACE,
                    guild_id=int(guild.id),
                    channel_id=int(channel.id),
                    event_id=int(message.id),
                    user_id=int(author.id),
                    author_is_bot=bool(getattr(author, "bot", False)),
                    actor_level=actor_level,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _currently_allowed(self, message: Any, actor_level: RbacLevel) -> bool:
        checker = getattr(getattr(self.bot, "capability_guard", None), "currently_allowed", None)
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if not callable(checker) or guild is None or author is None:
            return False
        try:
            return bool(
                checker(
                    SITE_AUTO_PUBLISH_CAPABILITY_ID,
                    guild_id=int(guild.id),
                    user_id=int(author.id),
                    actor_level=actor_level,
                    floor=RbacLevel.TRUSTED,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False


def _authorization_current(check: Callable[[], bool] | None) -> bool:
    if check is None:
        return True
    try:
        return check() is True
    except Exception:
        return False


def _same_referenced_message(source_message: Any, referenced_message: Any, *, message_id: int) -> bool:
    source_guild_id = getattr(getattr(source_message, "guild", None), "id", None)
    source_channel_id = getattr(getattr(source_message, "channel", None), "id", None)
    referenced_guild_id = getattr(getattr(referenced_message, "guild", None), "id", None)
    referenced_channel_id = getattr(getattr(referenced_message, "channel", None), "id", None)
    referenced_id = getattr(referenced_message, "id", None)
    return bool(
        isinstance(referenced_id, int)
        and not isinstance(referenced_id, bool)
        and referenced_id == message_id
        and isinstance(source_guild_id, int)
        and not isinstance(source_guild_id, bool)
        and source_guild_id > 0
        and referenced_guild_id == source_guild_id
        and isinstance(source_channel_id, int)
        and not isinstance(source_channel_id, bool)
        and source_channel_id > 0
        and referenced_channel_id == source_channel_id
    )


def _same_discord_scope(source_message: Any, response_message: Any) -> bool:
    source_guild_id = getattr(getattr(source_message, "guild", None), "id", None)
    response_guild_id = getattr(getattr(response_message, "guild", None), "id", None)
    source_channel_id = getattr(getattr(source_message, "channel", None), "id", None)
    response_channel_id = getattr(getattr(response_message, "channel", None), "id", None)
    return bool(
        isinstance(source_guild_id, int)
        and not isinstance(source_guild_id, bool)
        and source_guild_id > 0
        and isinstance(response_guild_id, int)
        and not isinstance(response_guild_id, bool)
        and response_guild_id == source_guild_id
        and isinstance(source_channel_id, int)
        and not isinstance(source_channel_id, bool)
        and source_channel_id > 0
        and isinstance(response_channel_id, int)
        and not isinstance(response_channel_id, bool)
        and response_channel_id == source_channel_id
    )


def _actor_role_rank(role: ActorRole) -> int:
    return {
        ActorRole.MEMBER: 0,
        ActorRole.GUILD_ADMIN: 1,
        ActorRole.BOT_OWNER: 2,
    }[ActorRole(role)]


def _display_name(html: str, prompt: str) -> str:
    match = _TITLE.search(html)
    candidate = re.sub(r"<[^>]+>", " ", match.group("title")) if match is not None else prompt
    candidate = " ".join(unicodedata.normalize("NFKC", candidate).strip().split())
    return candidate[:100] or "YonerAI generated site"


__all__ = [
    "DiscordAISiteDelivery",
    "PublishedSite",
    "SITE_AUTO_PUBLISH_CAPABILITY_ID",
    "SITE_AUTO_PUBLISH_SURFACE",
    "STRICT_STATIC_SITE_GUIDANCE",
    "SiteDeliveryAttempt",
    "SiteEditTarget",
]
