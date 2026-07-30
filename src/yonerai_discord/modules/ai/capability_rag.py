"""Discord actor向けCapability RAG候補の最小権限projection。

このmoduleは検索・順位付けを行わない。code-owned catalogから渡された候補を、
同一のfresh Discord member / actor contextで現在のRegistry・RBACへ投影する。
返却値にはDiscord user/member/role/guild/channel IDを保持しない。
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.discord_policy import actor_context_for_member


MAX_AUTHORIZATION_CANDIDATES = 4_096
MAX_CAPABILITY_ID_BYTES = 256
_MAX_DISCORD_ID = (1 << 63) - 1
_CAPABILITY_ID_ASCII = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


@dataclass(frozen=True, slots=True)
class DiscordCapabilityProjection:
    """Raw Discord identityを含まない、actor固有の候補projection。"""

    allowed_capability_ids: frozenset[str]
    actor_level: RbacLevel | None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_capability_ids, frozenset):
            raise TypeError("allowed_capability_ids must be a frozenset")
        if len(self.allowed_capability_ids) > MAX_AUTHORIZATION_CANDIDATES:
            raise ValueError("allowed_capability_ids contains too many entries")
        if any(not _canonical_capability_id(value) for value in self.allowed_capability_ids):
            raise ValueError("allowed_capability_ids contains an invalid capability ID")
        if self.actor_level is not None and not isinstance(self.actor_level, RbacLevel):
            raise TypeError("actor_level must be RbacLevel or None")
        if self.actor_level is None and self.allowed_capability_ids:
            raise ValueError("allowed capabilities require an actor level")


_EMPTY_PROJECTION = DiscordCapabilityProjection(frozenset(), None)


async def project_authorized_capabilities_for_discord_actor(
    *,
    guard: object,
    guild: object | None,
    channel: object | None,
    user_id: int,
    capability_ids: Collection[str],
    minimum_levels: Mapping[str, RbacLevel] | None = None,
) -> DiscordCapabilityProjection:
    """候補全体をfresh actorの現在権限へ投影する。

    外部入力やmodel出力からCapability IDを生成する用途ではない。候補は
    code-owned snapshotから渡され、さらに ``guard.registry`` への登録を確認する。
    不完全なDiscord scope、権限情報、Registry/Policy状態はすべてfail closedにする。
    """

    validated = _validated_candidates(capability_ids, minimum_levels)
    if validated is None:
        return _EMPTY_PROJECTION
    candidates, floors = validated
    if not candidates:
        return _EMPTY_PROJECTION

    if guild is None or channel is None or _discord_id(user_id) is None:
        return _EMPTY_PROJECTION
    try:
        guild_id = _discord_id(getattr(guild, "id", None))
        scope_matches = guild_id is not None and _scope_matches(channel, guild_id)
    except Exception:
        return _EMPTY_PROJECTION
    if guild_id is None or not scope_matches:
        return _EMPTY_PROJECTION

    try:
        bot = getattr(guard, "bot", None)
        settings = getattr(guard, "settings", None)
        registry = getattr(guard, "registry", None)
        policy = getattr(guard, "policy", None)
        fetch_member = getattr(guild, "fetch_member", None)
        permissions_for = getattr(channel, "permissions_for", None)
        capability = getattr(registry, "capability", None)
        evaluate = getattr(policy, "evaluate", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        closing = bool(getattr(bot, "is_closing", False))
    except Exception:
        return _EMPTY_PROJECTION
    if (
        bot is None
        or settings is None
        or closing
        or not callable(fetch_member)
        or not callable(permissions_for)
        or not callable(capability)
        or not callable(evaluate)
        or not callable(currently_allowed)
    ):
        return _EMPTY_PROJECTION

    try:
        member = await fetch_member(user_id)
        if _discord_id(getattr(member, "id", None)) != user_id or bool(getattr(member, "bot", False)):
            return _EMPTY_PROJECTION
        if not _optional_scope_matches(getattr(member, "guild", None), guild_id):
            return _EMPTY_PROJECTION
        permissions = permissions_for(member)
        if (
            getattr(permissions, "view_channel", False) is not True
            or getattr(permissions, "read_message_history", False) is not True
        ):
            return _EMPTY_PROJECTION
        actor = await actor_context_for_member(
            member=member,
            guild=guild,
            guild_id=guild_id,
            bot=bot,
            settings=settings,
        )
        actor_level = RbacLevel.parse(getattr(actor, "level", None))
        if str(getattr(actor, "actor_id", "")) != str(user_id) or str(getattr(actor, "guild_id", "")) != str(guild_id):
            return _EMPTY_PROJECTION
    except asyncio.CancelledError:
        raise
    except Exception:
        return _EMPTY_PROJECTION

    allowed: set[str] = set()
    for capability_id in candidates:
        floor = floors[capability_id]
        try:
            spec = capability(capability_id)
            if getattr(spec, "capability_id", None) != capability_id:
                continue
            decision = evaluate(capability_id, actor)
            if (
                getattr(decision, "allowed", False) is not True
                or actor_level < floor
                or currently_allowed(
                    capability_id,
                    guild_id=guild_id,
                    user_id=user_id,
                    actor_level=actor_level,
                    floor=floor,
                )
                is not True
                or bool(getattr(bot, "is_closing", False))
            ):
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        allowed.add(capability_id)
    try:
        if bool(getattr(bot, "is_closing", False)):
            return _EMPTY_PROJECTION
    except Exception:
        return _EMPTY_PROJECTION
    return DiscordCapabilityProjection(frozenset(allowed), actor_level)


async def authorized_capability_ids_for_discord_actor(
    *,
    guard: object,
    guild: object | None,
    channel: object | None,
    user_id: int,
    capability_ids: Collection[str],
    minimum_levels: Mapping[str, RbacLevel] | None = None,
) -> frozenset[str]:
    """Projectionから許可済みCapability IDだけを返す簡便wrapper。"""

    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=user_id,
        capability_ids=capability_ids,
        minimum_levels=minimum_levels,
    )
    return projection.allowed_capability_ids


def _validated_candidates(
    capability_ids: object,
    minimum_levels: object,
) -> tuple[tuple[str, ...], dict[str, RbacLevel]] | None:
    if isinstance(capability_ids, (str, bytes, bytearray, Mapping)) or not isinstance(capability_ids, Collection):
        return None
    try:
        if len(capability_ids) > MAX_AUTHORIZATION_CANDIDATES:
            return None
        normalized: set[str] = set()
        for index, value in enumerate(capability_ids):
            if index >= MAX_AUTHORIZATION_CANDIDATES:
                return None
            if not _canonical_capability_id(value):
                return None
            normalized.add(value)
    except Exception:
        return None

    if minimum_levels is None:
        return tuple(sorted(normalized)), dict.fromkeys(normalized, RbacLevel.EVERYONE)
    if not isinstance(minimum_levels, Mapping):
        return None

    floors = dict.fromkeys(normalized, RbacLevel.EVERYONE)
    try:
        if len(minimum_levels) > MAX_AUTHORIZATION_CANDIDATES:
            return None
        for index, (capability_id, floor) in enumerate(minimum_levels.items()):
            if index >= MAX_AUTHORIZATION_CANDIDATES:
                return None
            if (
                capability_id not in normalized
                or not _canonical_capability_id(capability_id)
                or not isinstance(floor, RbacLevel)
            ):
                return None
            floors[capability_id] = floor
    except Exception:
        return None
    return tuple(sorted(normalized)), floors


def _canonical_capability_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_CAPABILITY_ID_BYTES
        and value == value.strip().lower()
        and all(character in _CAPABILITY_ID_ASCII for character in value)
    )


def _discord_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DISCORD_ID:
        return None
    return value


def _scope_matches(channel: object, guild_id: int) -> bool:
    channel_guild = getattr(channel, "guild", None)
    channel_guild_id = getattr(channel_guild, "id", None)
    if channel_guild_id is None:
        channel_guild_id = getattr(channel, "guild_id", None)
    return channel_guild_id is None or _discord_id(channel_guild_id) == guild_id


def _optional_scope_matches(scoped: object | None, guild_id: int) -> bool:
    if scoped is None:
        return True
    return _discord_id(getattr(scoped, "id", None)) == guild_id


__all__ = [
    "DiscordCapabilityProjection",
    "MAX_AUTHORIZATION_CANDIDATES",
    "MAX_CAPABILITY_ID_BYTES",
    "authorized_capability_ids_for_discord_actor",
    "project_authorized_capabilities_for_discord_actor",
]
