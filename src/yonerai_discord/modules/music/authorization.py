from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel

from .models import MusicActor


MusicFreshCheck = Callable[[], Awaitable[MusicActor | None]]


async def build_music_commit_check(
    bot: Any,
    guild: Any,
    user_id: int,
    command_path: str,
    *,
    extra_capability_ids: Sequence[str] = (),
) -> MusicFreshCheck | None:
    """副作用直前にactor・voice state・中央policyを再取得するcallbackを作る。"""

    guard = getattr(bot, "capability_guard", None)
    checker = getattr(guard, "currently_allowed", None)
    actor_provider = getattr(guard, "actor", None)
    fetch_member = getattr(guild, "fetch_member", None)
    capability_id = COMMAND_CAPABILITIES.get(command_path)
    guild_id = getattr(guild, "id", None)
    if (
        not callable(checker)
        or not callable(actor_provider)
        or not callable(fetch_member)
        or capability_id is None
        or not isinstance(guild_id, int)
        or guild_id <= 0
        or user_id <= 0
    ):
        return None

    floor = COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE)
    extra_ids = tuple(dict.fromkeys(str(value).strip() for value in extra_capability_ids if str(value).strip()))

    async def commit_check() -> MusicActor | None:
        if bool(getattr(bot, "is_closing", False)) or getattr(bot, "capability_guard", None) is not guard:
            return None
        try:
            member = await fetch_member(user_id)
            if getattr(bot, "capability_guard", None) is not guard or int(member.id) != user_id:
                return None
            actor_context = await actor_provider(SimpleNamespace(user=member, guild=guild, guild_id=guild_id))
            if bool(getattr(bot, "is_closing", False)) or getattr(bot, "capability_guard", None) is not guard:
                return None
            if not checker(
                capability_id,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=actor_context.level,
                floor=floor,
            ):
                return None
            for extra_id in extra_ids:
                if not checker(
                    extra_id,
                    guild_id=guild_id,
                    user_id=user_id,
                    actor_level=actor_context.level,
                    floor=RbacLevel.EVERYONE,
                ):
                    return None
            if bool(getattr(bot, "is_closing", False)) or getattr(bot, "capability_guard", None) is not guard:
                return None
            channel = getattr(getattr(member, "voice", None), "channel", None)
            channel_id = getattr(channel, "id", None)
            permissions = getattr(member, "guild_permissions", None)
            manage = bool(
                permissions is not None
                and (
                    bool(getattr(permissions, "administrator", False))
                    or bool(getattr(permissions, "manage_guild", False))
                )
            )
            return MusicActor(
                user_id,
                int(channel_id) if isinstance(channel_id, int) and channel_id > 0 else None,
                manage,
            )
        except Exception:
            return None

    return commit_check


__all__ = ["MusicFreshCheck", "build_music_commit_check"]
