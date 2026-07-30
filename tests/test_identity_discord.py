from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.identity import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengePurpose,
    ClaimedChallenge,
    DiscordVerificationRoleGranter,
    SQLiteChallengeRepository,
    UnsafeVerificationRole,
    validate_verification_role,
)


NOW = datetime(2026, 7, 21, tzinfo=UTC)


def _permissions(**enabled: bool) -> SimpleNamespace:
    defaults = {
        "administrator": False,
        "manage_guild": False,
        "manage_roles": False,
        "manage_channels": False,
        "manage_webhooks": False,
        "manage_messages": False,
        "moderate_members": False,
        "kick_members": False,
        "ban_members": False,
        "mention_everyone": False,
    }
    defaults.update(enabled)
    return SimpleNamespace(**defaults)


class Role:
    def __init__(self, role_id: int, position: int, *, managed: bool = False, **permissions: bool) -> None:
        self.id = role_id
        self.position = position
        self.managed = managed
        self.permissions = _permissions(**permissions)

    def is_default(self) -> bool:
        return False


class Member:
    def __init__(self, member_id: int, top_position: int, roles=()) -> None:
        self.id = member_id
        self.top_role = SimpleNamespace(position=top_position)
        self.roles = list(roles)
        self.added: list[tuple[Role, str]] = []

    async def add_roles(self, role: Role, *, reason: str) -> None:
        self.added.append((role, reason))
        self.roles.append(role)


class Guild:
    def __init__(self, guild_id: int, bot_member: Member, member: Member, role: Role) -> None:
        self.id = guild_id
        self.me = bot_member
        self.me.guild_permissions = _permissions(manage_roles=True)
        self._member = member
        self._role = role
        self.channels = []
        self.owner_id = 500

    def get_member(self, member_id: int):
        return self._member if self._member.id == member_id else None

    async def fetch_member(self, member_id: int):
        return self.get_member(member_id)

    def get_role(self, role_id: int):
        return self._role if self._role.id == role_id else None


class Bot:
    def __init__(self, guild: Guild) -> None:
        self.guild = guild
        self.capability_registry = SimpleNamespace(
            is_capability_enabled=lambda capability_id, guild_id: (
                capability_id == "cap-run-verify-start" and guild_id == guild.id
            )
        )

    def get_guild(self, guild_id: int):
        return self.guild if self.guild.id == guild_id else None


def _repository(path: Path) -> SQLiteChallengeRepository:
    repository = SQLiteChallengeRepository(path)
    repository.open()
    repository.configure_guild(1, 20, True, 999, now=NOW)
    return repository


@pytest.mark.parametrize(
    "role",
    (
        Role(20, 2, managed=True),
        Role(20, 2, administrator=True),
        Role(20, 2, manage_roles=True),
        Role(20, 2, view_audit_log=True),
        Role(20, 2, manage_threads=True),
        Role(20, 11),
    ),
)
def test_privileged_managed_and_high_roles_are_rejected(role: Role) -> None:
    member = Member(2, 1)
    guild = Guild(1, Member(999, 10), member, role)
    with pytest.raises(UnsafeVerificationRole):
        validate_verification_role(guild, role, target=member)


def test_bot_without_manage_roles_and_high_target_are_rejected() -> None:
    role = Role(20, 2)
    member = Member(2, 10)
    guild = Guild(1, Member(999, 10), member, role)
    with pytest.raises(UnsafeVerificationRole):
        validate_verification_role(guild, role, target=member)


def test_configuring_actor_needs_manage_roles_and_higher_role() -> None:
    role = Role(20, 5)
    member = Member(2, 1)
    guild = Guild(1, Member(999, 10), member, role)
    actor = Member(300, 6)
    actor.guild_permissions = _permissions(manage_guild=True)
    with pytest.raises(UnsafeVerificationRole):
        validate_verification_role(guild, role, actor=actor)

    actor.guild_permissions = _permissions(manage_guild=True, manage_roles=True)
    validate_verification_role(guild, role, actor=actor)
    actor.top_role.position = 5
    with pytest.raises(UnsafeVerificationRole):
        validate_verification_role(guild, role, actor=actor)


def test_privileged_channel_overwrite_is_rejected() -> None:
    role = Role(20, 2)
    member = Member(2, 1)
    guild = Guild(1, Member(999, 10), member, role)
    channel = SimpleNamespace(overwrites_for=lambda selected: _permissions(manage_webhooks=selected.id == role.id))
    guild.channels = [channel]
    with pytest.raises(UnsafeVerificationRole):
        validate_verification_role(guild, role)
    guild.me.guild_permissions = _permissions()
    member.top_role.position = 1
    with pytest.raises(UnsafeVerificationRole):
        validate_verification_role(guild, role, target=member)


async def test_granter_uses_claim_binding_and_rechecks_live_hierarchy(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "identity.sqlite3")
    role = Role(20, 2)
    member = Member(2, 1)
    guild = Guild(1, Member(999, 10), member, role)
    granter = DiscordVerificationRoleGranter(Bot(guild), repository)
    claimed = ClaimedChallenge(
        claim_id="opaque-claim",
        purpose=ChallengePurpose.VERIFICATION,
        binding=ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        expires_at=NOW + timedelta(minutes=5),
    )
    assert await granter.grant(claimed)
    assert member.added == [(role, "YonerAI member verification completed")]
    assert "1" not in member.added[0][1] and "2" not in member.added[0][1]

    # Discord側role hierarchyが変わった場合は、同じclaimでも直前再検査で停止する。
    member.roles.clear()
    role.position = 10
    assert not await granter.grant(claimed)
    assert len(member.added) == 1
    repository.close()


async def test_granter_stops_when_central_capability_is_disabled(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "identity.sqlite3")
    role = Role(20, 2)
    member = Member(2, 1)
    guild = Guild(1, Member(999, 10), member, role)
    bot = Bot(guild)
    bot.capability_registry.is_capability_enabled = lambda capability_id, guild_id: False
    granter = DiscordVerificationRoleGranter(bot, repository)
    claimed = ClaimedChallenge(
        claim_id="opaque-claim",
        purpose=ChallengePurpose.VERIFICATION,
        binding=ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        expires_at=NOW + timedelta(minutes=5),
    )

    assert not await granter.grant(claimed)
    assert member.added == []
    repository.close()


async def test_granter_rejects_wrong_purpose_or_missing_guild(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "identity.sqlite3")
    role = Role(20, 2)
    member = Member(2, 1)
    guild = Guild(1, Member(999, 10), member, role)
    granter = DiscordVerificationRoleGranter(Bot(guild), repository)
    wrong = ClaimedChallenge(
        claim_id="opaque",
        purpose=ChallengePurpose.STATE,
        binding=ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        expires_at=NOW + timedelta(minutes=5),
    )
    assert not await granter.grant(wrong)
    missing = ClaimedChallenge(
        claim_id="opaque-2",
        purpose=ChallengePurpose.VERIFICATION,
        binding=ChallengeBinding(999, 2, ChallengeIntent.VERIFY_MEMBER),
        expires_at=NOW + timedelta(minutes=5),
    )
    assert not await granter.grant(missing)
    repository.close()
