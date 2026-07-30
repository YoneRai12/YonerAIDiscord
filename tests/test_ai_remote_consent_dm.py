from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.modules.ai.consent_view import RemoteConsentScope, RemoteConsentView
from yonerai_discord.modules.ai.remote_consent import RemoteConsentStore


class _Response:
    def __init__(self) -> None:
        self.done = False
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **_: Any) -> None:
        self.done = True

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.done = True
        self.messages.append((content, kwargs))


class _Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))


class _PartialPrompt:
    def __init__(self, message_id: int, channel: _DMChannel) -> None:
        self.id = message_id
        self.channel = channel

    async def edit(self, **kwargs: Any) -> None:
        self.channel.edits.append(kwargs)

    async def delete(self) -> None:
        self.channel.deleted.append(self.id)


class _DMChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.edits: list[dict[str, Any]] = []
        self.deleted: list[int] = []

    def get_partial_message(self, message_id: int) -> _PartialPrompt:
        return _PartialPrompt(message_id, self)


class _Prompt:
    def __init__(self, *, message_id: int, channel_id: int) -> None:
        self.id = message_id
        self.guild = None
        self.channel = _DMChannel(channel_id)


@pytest.mark.asyncio
async def test_dm_view_confirm_grants_without_guild_id_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    store = RemoteConsentStore(database_path=path)
    scope = RemoteConsentScope(guild_id=None, channel_id=30, user_id=20, source_message_id=40)
    prompt = _Prompt(message_id=50, channel_id=30)

    async def grant(_: Any, confirmed_scope: RemoteConsentScope) -> bool:
        store.grant(
            guild_id=confirmed_scope.guild_id,
            channel_id=confirmed_scope.channel_id,
            user_id=confirmed_scope.user_id,
        )
        return True

    view = RemoteConsentView(scope, grant)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=20),
        guild_id=None,
        channel_id=30,
        message=prompt,
        response=_Response(),
        followup=_Followup(),
    )

    await view.confirm(interaction)

    assert store.active(guild_id=None, channel_id=30, user_id=20) is True
    assert store.active(guild_id=None, channel_id=31, user_id=20) is True
    assert prompt.channel.deleted == [50]
    store.close()

    reopened = RemoteConsentStore(database_path=path)
    assert reopened.active(guild_id=None, channel_id=30, user_id=20) is True
    assert reopened.revoke(guild_id=None, channel_id=31, user_id=20) is True
    assert reopened.active(guild_id=None, channel_id=30, user_id=20) is False
    reopened.close()

    final = RemoteConsentStore(database_path=path)
    assert final.active(guild_id=None, channel_id=30, user_id=20) is False
    final.close()


def test_one_user_consent_is_shared_across_guild_and_dm_surfaces(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    first = RemoteConsentStore(database_path=path)
    first.grant(guild_id=30, channel_id=99, user_id=20)
    first.close()

    reopened = RemoteConsentStore(database_path=path)
    assert reopened.active(guild_id=30, channel_id=99, user_id=20) is True
    assert reopened.active(guild_id=None, channel_id=30, user_id=20) is True
    assert reopened.revoke(guild_id=None, channel_id=30, user_id=20) is True
    assert reopened.active(guild_id=30, channel_id=99, user_id=20) is False
    reopened.close()


@pytest.mark.asyncio
async def test_failed_dm_confirmation_keeps_disabled_prompt_for_diagnosis(tmp_path) -> None:
    store = RemoteConsentStore(database_path=tmp_path / "suite.sqlite3")
    scope = RemoteConsentScope(guild_id=None, channel_id=30, user_id=20, source_message_id=40)
    prompt = _Prompt(message_id=50, channel_id=30)

    async def reject(_: Any, __: RemoteConsentScope) -> bool:
        return False

    view = RemoteConsentView(scope, reject)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=20),
        guild_id=None,
        channel_id=30,
        message=prompt,
        response=_Response(),
        followup=_Followup(),
    )

    await view.confirm(interaction)

    assert prompt.channel.deleted == []
    assert prompt.channel.edits
    assert store.active(guild_id=None, channel_id=30, user_id=20) is False
    store.close()
