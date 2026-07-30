from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.modules.ai.consent_view import (
    DEFAULT_CONSENT_VIEW_TIMEOUT_SECONDS,
    RemoteConsentScope,
    RemoteConsentTerminalState,
    RemoteConsentView,
)


class FakeResponse:
    def __init__(self, events: list[str] | None = None) -> None:
        self.done = False
        self.events = events
        self.defers: list[dict[str, Any]] = []
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: Any) -> None:
        self.done = True
        self.defers.append(kwargs)
        if self.events is not None:
            self.events.append("defer")

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.done = True
        self.messages.append((content, kwargs))
        if self.events is not None:
            self.events.append("response")


class FakeFollowup:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))
        if self.events is not None:
            self.events.append("followup")


class FakePartialPromptMessage:
    def __init__(self, message_id: int, channel: FakePromptChannel) -> None:
        self.id = message_id
        self.channel = channel

    async def edit(self, **kwargs: Any) -> None:
        edits, edit_error, events = self.channel.edit_states[self.id]
        if events is not None:
            events.append("edit")
        if edit_error is not None:
            raise edit_error
        edits.append(kwargs)

    async def delete(self) -> None:
        _, _, events = self.channel.edit_states[self.id]
        self.channel.deleted_ids.append(self.id)
        if events is not None:
            events.append("delete")


class FakePromptChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.edit_states: dict[int, tuple[list[dict[str, Any]], Exception | None, list[str] | None]] = {}
        self.deleted_ids: list[int] = []

    def get_partial_message(self, message_id: int) -> FakePartialPromptMessage:
        return FakePartialPromptMessage(message_id, self)


class FakePromptMessage:
    def __init__(
        self,
        *,
        message_id: int = 900,
        guild_id: int = 10,
        channel_id: int = 30,
        edit_error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.id = message_id
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = FakePromptChannel(channel_id)
        self.edit_error = edit_error
        self.events = events
        self.edits: list[dict[str, Any]] = []
        self.channel.edit_states[self.id] = (self.edits, self.edit_error, self.events)

    async def edit(self, **kwargs: Any) -> None:
        if self.events is not None:
            self.events.append("edit")
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)


def make_interaction(
    prompt: FakePromptMessage,
    *,
    user_id: int = 20,
    guild_id: int = 10,
    channel_id: int = 30,
    events: list[str] | None = None,
) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=channel_id,
        message=prompt,
        response=FakeResponse(events),
        followup=FakeFollowup(events),
    )


def make_scope() -> RemoteConsentScope:
    return RemoteConsentScope(guild_id=10, channel_id=30, user_id=20, source_message_id=40)


def assert_ephemeral_no_mentions(kwargs: dict[str, Any]) -> None:
    assert kwargs["ephemeral"] is True
    allowed_mentions = kwargs["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.replied_user is False


def strongly_reaches(root: object, target: object) -> bool:
    """テストfakeの強参照だけを辿り、weakrefやfunction globalsは辿らない。"""

    pending = [root]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is target:
            return True
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset)):
            pending.extend(current)
        elif not callable(current):
            namespace = getattr(current, "__dict__", None)
            if isinstance(namespace, dict):
                pending.extend(namespace.values())
    return False


@pytest.mark.asyncio
async def test_confirm_is_scoped_short_lived_and_does_not_embed_source_content() -> None:
    secret_source_content = "SECRET original attachment and body"
    events: list[str] = []
    prompt = FakePromptMessage(events=events)
    scope = make_scope()
    calls: list[tuple[Any, RemoteConsentScope]] = []
    terminal: list[RemoteConsentTerminalState] = []

    async def on_confirm(interaction: Any, callback_scope: RemoteConsentScope) -> bool:
        events.append("confirm_callback")
        calls.append((interaction, callback_scope))
        return True

    def on_terminal(_: RemoteConsentView, state: RemoteConsentTerminalState) -> None:
        terminal.append(state)

    view = RemoteConsentView(scope, on_confirm, on_terminal=on_terminal)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = make_interaction(prompt, events=events)

    labels = [getattr(item, "label", None) for item in view.children]
    custom_ids = [getattr(item, "custom_id", None) for item in view.children]
    assert labels == ["同意して外部AIへ送信", "送信しない"]
    assert view.timeout == DEFAULT_CONSENT_VIEW_TIMEOUT_SECONDS
    assert all(secret_source_content not in str(value) for value in custom_ids)
    assert all(str(scope.source_message_id) not in str(value) for value in custom_ids)

    await view.children[0].callback(interaction)

    assert events == ["defer", "edit", "confirm_callback", "delete", "followup"]
    assert interaction.response.defers == [{"ephemeral": True, "thinking": True}]
    assert len(calls) == 1
    assert calls[0] == (interaction, scope)
    assert view.terminal_state is RemoteConsentTerminalState.CONFIRMED
    assert view.consumed
    assert all(getattr(item, "disabled", False) for item in view.children)
    assert prompt.edits == [{"view": view}]
    assert prompt.channel.deleted_ids == [prompt.id]
    assert terminal == [RemoteConsentTerminalState.CONFIRMED]
    assert "同意を確認" in interaction.followup.messages[0][0]
    assert_ephemeral_no_mentions(interaction.followup.messages[0][1])


@pytest.mark.asyncio
async def test_binding_replaces_full_reply_with_partial_and_drops_resolved_source_reference() -> None:
    source = SimpleNamespace(content="private source body", attachments=[SimpleNamespace(filename="private.txt")])
    prompt = FakePromptMessage()
    prompt.reference = SimpleNamespace(resolved=source)

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        return True

    view = RemoteConsentView(make_scope(), on_confirm)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]

    assert view._prompt_message is not prompt  # noqa: SLF001 - retention boundary regression
    assert isinstance(view._prompt_message, FakePartialPromptMessage)  # noqa: SLF001
    assert not strongly_reaches(view, source)

    await view.close()
    assert prompt.edits == [{"view": view}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override",),
    [
        ({"user_id": 21},),
        ({"guild_id": 11},),
        ({"channel_id": 31},),
    ],
)
async def test_other_user_or_scope_cannot_operate_view(override: dict[str, int]) -> None:
    prompt = FakePromptMessage()
    calls = 0

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        nonlocal calls
        calls += 1
        return True

    view = RemoteConsentView(make_scope(), on_confirm)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = make_interaction(prompt, **override)

    assert not await view.interaction_check(interaction)

    assert calls == 0
    assert not view.consumed
    assert len(prompt.edits) == 0
    assert "本人だけ" in interaction.response.messages[0][0]
    assert_ephemeral_no_mentions(interaction.response.messages[0][1])


@pytest.mark.asyncio
async def test_prompt_message_scope_is_fixed_after_binding() -> None:
    prompt = FakePromptMessage(message_id=900)
    other_prompt = FakePromptMessage(message_id=901)

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        raise AssertionError("must not be called")

    view = RemoteConsentView(make_scope(), on_confirm)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = make_interaction(other_prompt)

    await view.confirm(interaction)

    assert not view.consumed
    assert len(prompt.edits) == 0
    assert "本人だけ" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_repeated_confirm_is_one_shot_even_while_first_callback_is_running() -> None:
    prompt = FakePromptMessage()
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    calls = 0
    terminal: list[RemoteConsentTerminalState] = []

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        nonlocal calls
        calls += 1
        callback_started.set()
        await release_callback.wait()
        return True

    async def on_terminal(_: RemoteConsentView, state: RemoteConsentTerminalState) -> None:
        terminal.append(state)

    view = RemoteConsentView(make_scope(), on_confirm, on_terminal=on_terminal)
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    first = make_interaction(prompt)
    second = make_interaction(prompt)

    first_task = asyncio.create_task(view.confirm(first))
    await callback_started.wait()
    second_task = asyncio.create_task(view.confirm(second))
    await second_task
    release_callback.set()
    await first_task

    assert calls == 1
    assert len(prompt.edits) == 1
    assert terminal == [RemoteConsentTerminalState.CONFIRMED]
    assert "すでに終了" in second.followup.messages[0][0]
    assert second.response.defers == [{"ephemeral": True, "thinking": True}]


@pytest.mark.asyncio
async def test_cancel_disables_view_without_calling_confirm() -> None:
    prompt = FakePromptMessage()
    calls = 0
    terminal: list[RemoteConsentTerminalState] = []

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        nonlocal calls
        calls += 1
        return True

    view = RemoteConsentView(
        make_scope(),
        on_confirm,
        on_terminal=lambda _, state: terminal.append(state),
    )
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = make_interaction(prompt)

    await view.children[1].callback(interaction)

    assert calls == 0
    assert view.terminal_state is RemoteConsentTerminalState.CANCELLED
    assert all(getattr(item, "disabled", False) for item in view.children)
    assert prompt.edits == [{"view": view}]
    assert prompt.channel.deleted_ids == [prompt.id]
    assert terminal == [RemoteConsentTerminalState.CANCELLED]
    assert interaction.followup.messages[0][0] == "送信しませんでした。"
    assert_ephemeral_no_mentions(interaction.followup.messages[0][1])


@pytest.mark.asyncio
async def test_timeout_and_repeated_close_are_fail_closed_and_idempotent() -> None:
    prompt = FakePromptMessage()
    calls = 0
    terminal: list[RemoteConsentTerminalState] = []

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        nonlocal calls
        calls += 1
        return True

    view = RemoteConsentView(
        make_scope(),
        on_confirm,
        on_terminal=lambda _, state: terminal.append(state),
    )
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]

    await view.on_timeout()
    await view.close()

    assert calls == 0
    assert view.terminal_state is RemoteConsentTerminalState.TIMED_OUT
    assert all(getattr(item, "disabled", False) for item in view.children)
    assert prompt.edits == [{"view": view}]
    assert terminal == [RemoteConsentTerminalState.TIMED_OUT]


@pytest.mark.asyncio
async def test_prompt_edit_failure_never_calls_confirm_or_logs_source_body(caplog: pytest.LogCaptureFixture) -> None:
    secret_source_content = "DO-NOT-LOG original private body"
    prompt = FakePromptMessage(edit_error=RuntimeError(secret_source_content))
    calls = 0
    terminal: list[RemoteConsentTerminalState] = []

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        nonlocal calls
        calls += 1
        return True

    view = RemoteConsentView(
        make_scope(),
        on_confirm,
        on_terminal=lambda _, state: terminal.append(state),
    )
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    interaction = make_interaction(prompt)

    await view.confirm(interaction)

    assert calls == 0
    assert view.terminal_state is RemoteConsentTerminalState.FAILED_CLOSED
    assert terminal == [RemoteConsentTerminalState.FAILED_CLOSED]
    assert "外部AIには送信しません" in interaction.followup.messages[0][0]
    assert "remote_consent_prompt_edit_failed" in caplog.text
    assert secret_source_content not in caplog.text


def test_scope_and_binding_reject_invalid_or_changed_identifiers() -> None:
    with pytest.raises(ValueError):
        RemoteConsentScope(guild_id=0, channel_id=30, user_id=20, source_message_id=40)

    async def on_confirm(_: Any, __: RemoteConsentScope) -> bool:
        return True

    view = RemoteConsentView(make_scope(), on_confirm)
    prompt = FakePromptMessage()
    view.bind_prompt_message(prompt)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        view.bind_prompt_message(FakePromptMessage(message_id=901))  # type: ignore[arg-type]

    unavailable = FakePromptMessage(message_id=902)
    unavailable.channel = SimpleNamespace(id=30)
    unbound_view = RemoteConsentView(make_scope(), on_confirm)
    with pytest.raises(ValueError, match="ID-only partial"):
        unbound_view.bind_prompt_message(unavailable)  # type: ignore[arg-type]
