from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.modules.music import MusicPlugin
from yonerai_discord.modules.music.models import MusicActor
from yonerai_discord.modules.music.read_aloud_adapter import MusicReadAloudAdapter
from yonerai_discord.modules.scheduling.focus import (
    FOCUS_TIMER_COMPLETION_MESSAGE,
    FocusDeliveryDisposition,
    FocusReadAloudOverlay,
    FocusTimerBinding,
    FocusTimerCurrentState,
)
from yonerai_discord.modules.voice.models import SynthesizedSpeech
from yonerai_discord.modules.voice.presets import (
    ResolvedVoicePreset,
    VoicePresetScope,
    VoicePresetValues,
)
from yonerai_discord.modules.voice.read_aloud import (
    ReadAloudBatchStatus,
    ReadAloudDictionaryEntry,
    ReadAloudPolicySnapshot,
    ReadAloudRoute,
)


class FakeGuard:
    def __init__(self) -> None:
        self.event_calls: list[tuple[str, dict[str, Any]]] = []
        self.event_enabled = True

    def event_allowed(self, capability_id: str, **kwargs: Any) -> bool:
        self.event_calls.append((capability_id, kwargs))
        return self.event_enabled

    def currently_allowed(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    async def actor(self, _interaction: Any) -> Any:
        return SimpleNamespace(level=0)


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.channels = {channel_id: SimpleNamespace(id=channel_id, guild=self) for channel_id in (20, 30)}

    def get_channel_or_thread(self, channel_id: int) -> Any | None:
        return self.channels.get(channel_id)

    def get_channel(self, channel_id: int) -> Any | None:
        return self.channels.get(channel_id)


class FakeRouteRepository:
    def __init__(self, route: ReadAloudRoute) -> None:
        self.route = route
        self.policy = ReadAloudPolicySnapshot(guild_id=route.guild_id, revision=0)
        self.is_open = True
        self.reads = 0
        self.policy_reads = 0
        self.after_policy_read: Any | None = None

    def get(self, guild_id: int, channel_id: int) -> ReadAloudRoute | None:
        self.reads += 1
        if (guild_id, channel_id) != (self.route.guild_id, self.route.source_channel_id):
            return None
        return self.route

    def get_policy(self, guild_id: int) -> ReadAloudPolicySnapshot:
        self.policy_reads += 1
        policy = self.policy
        if callable(self.after_policy_read):
            self.after_policy_read(self.policy_reads)
        if guild_id != policy.guild_id:
            return ReadAloudPolicySnapshot(guild_id=guild_id, revision=0)
        return policy


class FakePresetRepository:
    def __init__(self) -> None:
        self.is_open = True
        self.presets: dict[int, ResolvedVoicePreset] = {}
        self.reads = 0
        self.after_read: Any | None = None

    def resolve(self, guild_id: int, user_id: int) -> ResolvedVoicePreset:
        self.reads += 1
        preset = self.presets.get(
            user_id,
            ResolvedVoicePreset(guild_id=guild_id, user_id=user_id),
        )
        if callable(self.after_read):
            self.after_read(self.reads)
        return preset


class FakeFocusOverlayStore:
    def __init__(
        self,
        overlay: FocusReadAloudOverlay | None,
        *,
        read_error: Exception | None = None,
    ) -> None:
        self.overlay = overlay
        self.read_error = read_error
        self.is_open = True
        self.reads = 0

    def active_for_source(
        self,
        guild_id: int,
        source_channel_id: int,
    ) -> FocusReadAloudOverlay | None:
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        overlay = self.overlay
        if (
            overlay is None
            or overlay.binding.guild_id != guild_id
            or overlay.binding.source_channel_id != source_channel_id
        ):
            return None
        return overlay


class FakeFocusRuntime:
    def __init__(self, store: FakeFocusOverlayStore) -> None:
        self.overlays = store
        self.allowed = True

    async def authorization_current(
        self,
        binding: FocusTimerBinding,
    ) -> FocusTimerCurrentState | None:
        if not self.allowed:
            return None
        return FocusTimerCurrentState(binding=binding, authorized=True)


class FakeSpeechQueue:
    def __init__(self, *, block: bool = False, failures: int = 0) -> None:
        self.available = True
        self.failures = failures
        self.requests: list[Any] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        if not block:
            self.release.set()

    async def synthesize(self, request: Any, *, current_policy: Any) -> SynthesizedSpeech:
        assert await current_policy() is True
        self.requests.append(request)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if not await current_policy():
            raise RuntimeError("revoked")
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthesis failed")
        return SynthesizedSpeech(b"RIFFread-aloud")


class FakeMusicService:
    def __init__(self) -> None:
        self.available = True
        self.session = object()
        self.voice_channel_id = 30
        self.calls: list[tuple[int, MusicActor, bytes]] = []
        self.before_commit_check: Any | None = None

    def listener_session_identity(self, guild_id: int) -> object | None:
        return self.session if guild_id == 1 else None

    def session_channel_id(self, guild_id: int) -> int | None:
        return self.voice_channel_id if guild_id == 1 else None

    async def add_speech_wav(
        self,
        guild_id: int,
        actor: MusicActor,
        wav: bytes,
        *,
        commit_check: Any,
    ) -> int:
        if callable(self.before_commit_check):
            self.before_commit_check()
        current = await commit_check()
        if current is None:
            raise RuntimeError("revoked")
        self.calls.append((guild_id, current, wav))
        return 1


class FakeBot:
    def __init__(
        self,
        guild: FakeGuild,
        service: FakeMusicService,
        queue: FakeSpeechQueue,
    ) -> None:
        self.guild = guild
        self.capability_guard = FakeGuard()
        self.music_service = service
        self.speech_queue = queue
        self.is_closing = False

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guild if guild_id == self.guild.id else None


def _message(
    *,
    author_id: int = 10,
    voice_channel_id: int = 30,
    content: str = "読み上げ対象の秘密本文",
    bot: bool = False,
    webhook_id: int | None = None,
) -> Any:
    author = SimpleNamespace(
        id=author_id,
        bot=bot,
        voice=SimpleNamespace(channel=SimpleNamespace(id=voice_channel_id)),
    )
    return SimpleNamespace(
        id=100 + author_id,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=20),
        author=author,
        content=content,
        webhook_id=webhook_id,
    )


@pytest.fixture
def adapter_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    route = ReadAloudRoute(
        guild_id=1,
        source_channel_id=20,
        destination_voice_channel_id=30,
        enabled=True,
    )
    repository = FakeRouteRepository(route)
    preset_repository = FakePresetRepository()
    service = FakeMusicService()
    queue = FakeSpeechQueue()
    guild = FakeGuild()
    bot = FakeBot(guild, service, queue)
    bot.music_read_aloud_preset_repository = preset_repository
    state = SimpleNamespace(allowed={10: True, 11: True}, voice={10: 30, 11: 30}, calls=[])

    async def build_check(
        current_bot: Any,
        current_guild: Any,
        user_id: int,
        command_path: str,
        *,
        extra_capability_ids: tuple[str, ...],
    ) -> Any:
        assert current_bot is bot
        assert current_guild is guild
        assert command_path == "music speak"
        assert extra_capability_ids == (
            "cap-run-music-read-aloud-message",
            "cap-run-audio-ducking-core",
        )

        async def current() -> MusicActor | None:
            state.calls.append(user_id)
            if not state.allowed.get(user_id, False):
                return None
            return MusicActor(user_id, state.voice.get(user_id))

        return current

    monkeypatch.setattr(
        "yonerai_discord.modules.music.read_aloud_adapter.build_music_commit_check",
        build_check,
    )
    adapter = MusicReadAloudAdapter(
        bot,
        service,
        repository,  # type: ignore[arg-type]
        preset_repository,  # type: ignore[arg-type]
        queue,
        runtime_current=lambda: True,
        merge_window_seconds=0.05,
    )
    return {
        "route": route,
        "repository": repository,
        "preset_repository": preset_repository,
        "service": service,
        "queue": queue,
        "guild": guild,
        "bot": bot,
        "state": state,
        "adapter": adapter,
    }


@pytest.mark.asyncio
async def test_read_aloud_uses_existing_synthesis_and_ducking_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    body = "読み上げ対象の秘密本文"

    assert await adapter.handle_message(_message(content=body))
    await adapter.wait_idle()

    queue = adapter_fixture["queue"]
    service = adapter_fixture["service"]
    assert len(queue.requests) == 1
    assert queue.requests[0].speaker_id == 3
    assert service.calls == [(1, MusicActor(10, 30), b"RIFFread-aloud")]
    receipt = adapter.receipts()[-1]
    assert receipt.status is ReadAloudBatchStatus.DELIVERED
    assert body not in repr(queue.requests[0])
    assert body not in repr(receipt)
    assert body not in repr(adapter)
    assert adapter_fixture["bot"].capability_guard.event_calls[0][0] == ("cap-run-music-read-aloud-message")


@pytest.mark.asyncio
async def test_active_focus_overlay_temporarily_overrides_persistent_route(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["repository"]
    repository.route = ReadAloudRoute(
        guild_id=1,
        source_channel_id=20,
        destination_voice_channel_id=31,
        enabled=True,
    )
    overlay = FocusReadAloudOverlay(
        binding=FocusTimerBinding(
            timer_id="focus-20",
            owner_id=99,
            guild_id=1,
            source_channel_id=20,
            destination_channel_id=30,
            revision=7,
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store = FakeFocusOverlayStore(overlay)
    runtime = FakeFocusRuntime(store)
    bot = adapter_fixture["bot"]
    bot.scheduling_focus_overlay_store = store
    bot.scheduling_focus_timer_runtime = runtime
    bot.scheduling_plugin = SimpleNamespace(
        focus_overlay_store=store,
        focus_runtime=runtime,
        closing=False,
    )

    assert await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()

    assert adapter_fixture["service"].calls == [(1, MusicActor(10, 30), b"RIFFread-aloud")]
    assert store.reads >= 3


@pytest.mark.asyncio
async def test_focus_overlay_identity_swap_revokes_before_speech_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    queue = FakeSpeechQueue(block=True)
    adapter_fixture["queue"] = queue
    bot = adapter_fixture["bot"]
    bot.speech_queue = queue
    adapter = MusicReadAloudAdapter(
        bot,
        adapter_fixture["service"],
        adapter_fixture["repository"],  # type: ignore[arg-type]
        adapter_fixture["preset_repository"],  # type: ignore[arg-type]
        queue,
        runtime_current=lambda: True,
        merge_window_seconds=0.01,
    )
    overlay = FocusReadAloudOverlay(
        binding=FocusTimerBinding(
            timer_id="focus-20",
            owner_id=99,
            guild_id=1,
            source_channel_id=20,
            destination_channel_id=30,
            revision=8,
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store = FakeFocusOverlayStore(overlay)
    runtime = FakeFocusRuntime(store)
    bot.scheduling_focus_overlay_store = store
    bot.scheduling_focus_timer_runtime = runtime
    bot.scheduling_plugin = SimpleNamespace(
        focus_overlay_store=store,
        focus_runtime=runtime,
        closing=False,
    )

    assert await adapter.handle_message(_message())
    await queue.entered.wait()
    replacement = FakeFocusOverlayStore(overlay)
    replacement_runtime = FakeFocusRuntime(replacement)
    bot.scheduling_focus_overlay_store = replacement
    bot.scheduling_focus_timer_runtime = replacement_runtime
    bot.scheduling_plugin.focus_overlay_store = replacement
    bot.scheduling_plugin.focus_runtime = replacement_runtime
    queue.release.set()
    await adapter.wait_idle()

    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_invalid_focus_runtime_never_falls_back_to_persistent_route(
    adapter_fixture: dict[str, Any],
) -> None:
    overlay = FocusReadAloudOverlay(
        binding=FocusTimerBinding(
            timer_id="focus-20",
            owner_id=99,
            guild_id=1,
            source_channel_id=20,
            destination_channel_id=30,
            revision=9,
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    bot = adapter_fixture["bot"]
    store = FakeFocusOverlayStore(overlay)
    runtime = FakeFocusRuntime(store)
    bot.scheduling_focus_overlay_store = store
    bot.scheduling_focus_timer_runtime = runtime
    bot.scheduling_plugin = SimpleNamespace(
        focus_overlay_store=FakeFocusOverlayStore(overlay),
        focus_runtime=runtime,
        closing=False,
    )

    assert not await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()

    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_focus_overlay_resolution_failure_never_uses_persistent_route(
    adapter_fixture: dict[str, Any],
) -> None:
    bot = adapter_fixture["bot"]
    store = FakeFocusOverlayStore(
        None,
        read_error=RuntimeError("fixed ambiguous overlay"),
    )
    runtime = FakeFocusRuntime(store)
    bot.scheduling_focus_overlay_store = store
    bot.scheduling_focus_timer_runtime = runtime
    bot.scheduling_plugin = SimpleNamespace(
        focus_overlay_store=store,
        focus_runtime=runtime,
        closing=False,
    )

    assert not await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()

    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_focus_completion_voice_reuses_synthesis_and_ducking_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    store = FakeFocusOverlayStore(None)
    runtime = FakeFocusRuntime(store)
    bot = adapter_fixture["bot"]
    bot.scheduling_focus_timer_runtime = runtime
    bot.scheduling_plugin = SimpleNamespace(
        focus_runtime=runtime,
        closing=False,
    )
    binding = FocusTimerBinding(
        timer_id="focus-20",
        owner_id=10,
        guild_id=1,
        source_channel_id=20,
        destination_channel_id=30,
        revision=10,
    )

    result = await adapter_fixture["adapter"].speak(
        binding,
        message_code="focus_timer.completed",
        idempotency_key="a" * 64,
    )

    assert result is FocusDeliveryDisposition.DELIVERED
    assert adapter_fixture["queue"].requests[-1].text == FOCUS_TIMER_COMPLETION_MESSAGE
    assert adapter_fixture["service"].calls[-1] == (
        1,
        MusicActor(10, 30),
        b"RIFFread-aloud",
    )


@pytest.mark.asyncio
async def test_focus_completion_revoke_during_synthesis_keeps_sink_zero(
    adapter_fixture: dict[str, Any],
) -> None:
    queue = FakeSpeechQueue(block=True)
    adapter_fixture["adapter"]._speech_queue = queue
    adapter_fixture["bot"].speech_queue = queue
    store = FakeFocusOverlayStore(None)
    runtime = FakeFocusRuntime(store)
    bot = adapter_fixture["bot"]
    bot.scheduling_focus_timer_runtime = runtime
    bot.scheduling_plugin = SimpleNamespace(
        focus_runtime=runtime,
        closing=False,
    )
    binding = FocusTimerBinding(
        timer_id="focus-20",
        owner_id=10,
        guild_id=1,
        source_channel_id=20,
        destination_channel_id=30,
        revision=11,
    )

    task = asyncio.create_task(
        adapter_fixture["adapter"].speak(
            binding,
            message_code="focus_timer.completed",
            idempotency_key="b" * 64,
        )
    )
    await queue.entered.wait()
    runtime.allowed = False
    queue.release.set()
    with pytest.raises(RuntimeError, match="revoked"):
        await task

    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_dictionary_is_applied_and_exclusion_never_calls_provider_or_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    repository = adapter_fixture["repository"]
    repository.policy = ReadAloudPolicySnapshot(
        guild_id=1,
        revision=1,
        dictionary=(ReadAloudDictionaryEntry("YonerAI", "よねらい"),),
        exclusions=("秘密",),
    )

    assert await adapter.handle_message(_message(content="YonerAIです"))
    await adapter.wait_idle()
    assert adapter_fixture["queue"].requests[-1].text == "よねらいです"
    provider_calls = len(adapter_fixture["queue"].requests)
    sink_calls = len(adapter_fixture["service"].calls)

    assert not await adapter.handle_message(_message(content="秘密の本文"))
    await adapter.wait_idle()
    assert len(adapter_fixture["queue"].requests) == provider_calls
    assert len(adapter_fixture["service"].calls) == sink_calls


@pytest.mark.asyncio
async def test_policy_revision_race_stops_before_submit_provider_and_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["repository"]
    repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=1)

    def replace_after_initial_read(reads: int) -> None:
        if reads == 1:
            repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=2)

    repository.after_policy_read = replace_after_initial_read
    assert not await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()
    assert repository.policy_reads == 2
    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_policy_change_during_merge_window_revokes_batch_before_provider_and_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["repository"]
    repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=1)

    assert await adapter_fixture["adapter"].handle_message(_message(content="秘密の本文"))
    repository.policy = ReadAloudPolicySnapshot(
        guild_id=1,
        revision=2,
        exclusions=("秘密",),
    )
    await adapter_fixture["adapter"].wait_idle()

    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []
    assert adapter_fixture["adapter"].receipts()[-1].status is ReadAloudBatchStatus.REVOKED


@pytest.mark.asyncio
async def test_user_preset_values_reach_existing_synthesis_queue(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["preset_repository"]
    repository.presets[10] = ResolvedVoicePreset(
        guild_id=1,
        user_id=10,
        values=VoicePresetValues(speed_milli=1_500, volume_milli=600),
        source=VoicePresetScope.USER,
        revision=1,
    )

    assert await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()

    request = adapter_fixture["queue"].requests[0]
    assert request.speed_scale == 1.5
    assert request.volume_scale == 0.6
    assert len(adapter_fixture["service"].calls) == 1


@pytest.mark.asyncio
async def test_preset_change_during_merge_window_revokes_before_provider_and_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["preset_repository"]
    initial = ResolvedVoicePreset(
        guild_id=1,
        user_id=10,
        values=VoicePresetValues(speed_milli=1_100, volume_milli=900),
        source=VoicePresetScope.USER,
        revision=1,
    )
    repository.presets[10] = initial

    assert await adapter_fixture["adapter"].handle_message(_message())
    repository.presets[10] = ResolvedVoicePreset(
        guild_id=1,
        user_id=10,
        values=initial.values,
        source=VoicePresetScope.USER,
        revision=2,
    )
    await adapter_fixture["adapter"].wait_idle()

    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []
    assert adapter_fixture["adapter"].receipts()[-1].status is ReadAloudBatchStatus.REVOKED


@pytest.mark.asyncio
async def test_preset_change_during_provider_blocks_delivery(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    repository = adapter_fixture["preset_repository"]
    repository.presets[10] = ResolvedVoicePreset(
        guild_id=1,
        user_id=10,
        values=VoicePresetValues(speed_milli=1_100, volume_milli=900),
        source=VoicePresetScope.USER,
        revision=1,
    )
    queue = FakeSpeechQueue(block=True)
    adapter_fixture["queue"] = queue
    adapter._speech_queue = queue
    adapter_fixture["bot"].speech_queue = queue

    assert await adapter.handle_message(_message())
    await asyncio.wait_for(queue.entered.wait(), timeout=1)
    repository.presets[10] = ResolvedVoicePreset(
        guild_id=1,
        user_id=10,
        values=VoicePresetValues(speed_milli=1_200, volume_milli=900),
        source=VoicePresetScope.USER,
        revision=2,
    )
    queue.release.set()
    await adapter.wait_idle()

    assert len(queue.requests) == 1
    assert adapter_fixture["service"].calls == []
    assert adapter.receipts()[-1].status is not ReadAloudBatchStatus.DELIVERED


@pytest.mark.asyncio
async def test_preset_change_inside_delivery_commit_check_stops_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["preset_repository"]
    initial = ResolvedVoicePreset(
        guild_id=1,
        user_id=10,
        values=VoicePresetValues(speed_milli=1_100, volume_milli=900),
        source=VoicePresetScope.USER,
        revision=1,
    )
    repository.presets[10] = initial

    def replace_preset() -> None:
        repository.presets[10] = ResolvedVoicePreset(
            guild_id=1,
            user_id=10,
            values=initial.values,
            source=VoicePresetScope.USER,
            revision=2,
        )

    adapter_fixture["service"].before_commit_check = replace_preset
    assert await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()

    assert len(adapter_fixture["queue"].requests) == 1
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_different_policy_revisions_never_mix_in_one_pending_burst(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["repository"]
    repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=1)

    assert await adapter_fixture["adapter"].handle_message(_message(author_id=10, content="一件目"))
    repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=2)
    assert not await adapter_fixture["adapter"].handle_message(_message(author_id=11, content="二件目"))
    await adapter_fixture["adapter"].wait_idle()

    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_policy_change_inside_synthesis_fresh_check_stops_before_provider(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    repository = adapter_fixture["repository"]
    repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=1)
    original = adapter._fresh_actors
    calls = 0

    async def replace_during_fresh_check(
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        *,
        claim_session: bool = True,
    ) -> Any:
        nonlocal calls
        calls += 1
        result = await original(route, author_ids, claim_session=claim_session)
        if calls == 3:
            repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=2)
        return result

    adapter._fresh_actors = replace_during_fresh_check
    assert await adapter.handle_message(_message())
    await adapter.wait_idle()

    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_policy_change_inside_delivery_commit_check_stops_sink(
    adapter_fixture: dict[str, Any],
) -> None:
    repository = adapter_fixture["repository"]
    repository.policy = ReadAloudPolicySnapshot(guild_id=1, revision=1)
    adapter_fixture["service"].before_commit_check = lambda: setattr(
        repository,
        "policy",
        ReadAloudPolicySnapshot(guild_id=1, revision=2),
    )

    assert await adapter_fixture["adapter"].handle_message(_message())
    await adapter_fixture["adapter"].wait_idle()

    assert len(adapter_fixture["queue"].requests) == 1
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_all_merged_authors_are_rechecked_after_synthesis(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    queue = FakeSpeechQueue(block=True)
    adapter_fixture["queue"] = queue
    adapter._speech_queue = queue
    adapter_fixture["bot"].speech_queue = queue

    assert await adapter.handle_message(_message(author_id=10, content="一件目"))
    assert await adapter.handle_message(_message(author_id=11, content="二件目"))
    await asyncio.wait_for(queue.entered.wait(), timeout=1)
    adapter_fixture["state"].allowed[11] = False
    queue.release.set()
    await adapter.wait_idle()

    assert adapter_fixture["service"].calls == []
    assert 10 in adapter_fixture["state"].calls
    assert 11 in adapter_fixture["state"].calls
    assert adapter.receipts()[-1].status is not ReadAloudBatchStatus.DELIVERED


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["service", "queue", "session", "channel"])
async def test_runtime_identity_replacement_prevents_delivery(
    adapter_fixture: dict[str, Any],
    replacement: str,
) -> None:
    adapter = adapter_fixture["adapter"]
    queue = FakeSpeechQueue(block=True)
    adapter_fixture["queue"] = queue
    adapter._speech_queue = queue
    adapter_fixture["bot"].speech_queue = queue

    assert await adapter.handle_message(_message())
    await asyncio.wait_for(queue.entered.wait(), timeout=1)
    if replacement == "service":
        adapter_fixture["bot"].music_service = object()
    elif replacement == "queue":
        adapter_fixture["bot"].speech_queue = object()
    elif replacement == "channel":
        adapter_fixture["guild"].channels.pop(20)
    else:
        adapter_fixture["service"].session = object()
    queue.release.set()
    await adapter.wait_idle()

    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_close_cancels_inflight_synthesis_without_delivery(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    queue = FakeSpeechQueue(block=True)
    adapter._speech_queue = queue
    adapter_fixture["bot"].speech_queue = queue

    assert await adapter.handle_message(_message())
    await asyncio.wait_for(queue.entered.wait(), timeout=1)
    await adapter.close()

    assert queue.cancelled
    assert adapter_fixture["service"].calls == []
    assert adapter.receipts()[-1].status is ReadAloudBatchStatus.CANCELLED


@pytest.mark.asyncio
async def test_failed_batch_releases_session_lease_for_next_session(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]
    queue = FakeSpeechQueue(failures=1)
    adapter._speech_queue = queue
    adapter_fixture["bot"].speech_queue = queue

    assert await adapter.handle_message(_message(content="失敗する一件目"))
    await adapter.wait_idle()
    assert adapter.receipts()[-1].status is ReadAloudBatchStatus.FAILED
    assert adapter_fixture["service"].calls == []

    adapter_fixture["service"].session = object()
    assert await adapter.handle_message(_message(content="新sessionの二件目"))
    await adapter.wait_idle()

    assert adapter.receipts()[-1].status is ReadAloudBatchStatus.DELIVERED
    assert len(adapter_fixture["service"].calls) == 1


@pytest.mark.asyncio
async def test_invalid_submit_never_claims_session_lease(
    adapter_fixture: dict[str, Any],
) -> None:
    adapter = adapter_fixture["adapter"]

    assert not await adapter.handle_message(_message(content=" \t "))
    adapter_fixture["service"].session = object()
    assert await adapter.handle_message(_message(content="有効な二件目"))
    await adapter.wait_idle()

    assert adapter.receipts()[-1].status is ReadAloudBatchStatus.DELIVERED
    assert len(adapter_fixture["service"].calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "voice_override"),
    [
        (_message(bot=True), None),
        (_message(webhook_id=1), None),
        (_message(voice_channel_id=31), 31),
    ],
)
async def test_automated_or_non_destination_voice_message_is_ignored(
    adapter_fixture: dict[str, Any],
    message: Any,
    voice_override: int | None,
) -> None:
    if voice_override is not None:
        adapter_fixture["state"].voice[10] = voice_override
    assert not await adapter_fixture["adapter"].handle_message(message)
    await adapter_fixture["adapter"].wait_idle()
    assert adapter_fixture["queue"].requests == []
    assert adapter_fixture["service"].calls == []


@pytest.mark.asyncio
async def test_plugin_read_aloud_lifecycle_is_default_off_and_exactly_owned(
    tmp_path: Any,
) -> None:
    service = FakeMusicService()
    queue = FakeSpeechQueue()
    guild = FakeGuild()
    listeners: list[tuple[Any, str]] = []
    bot = FakeBot(guild, service, queue)
    bot.settings = SimpleNamespace(
        music_read_aloud_enabled=False,
        music_database_path=tmp_path / "music.sqlite3",
    )
    bot.add_listener = lambda listener, name: listeners.append((listener, name))

    def remove_listener(listener: Any, name: str) -> None:
        listeners.remove((listener, name))

    bot.remove_listener = remove_listener
    plugin = MusicPlugin()
    plugin.bot = bot
    plugin.service = service  # type: ignore[assignment]
    plugin.repository = object()  # type: ignore[assignment]
    plugin._read_aloud_accepting_starts = True

    assert not await plugin._start_read_aloud(bot)
    assert not hasattr(bot, "music_read_aloud_service")
    assert not hasattr(bot, "music_read_aloud_preset_repository")

    bot.settings.music_read_aloud_enabled = True
    assert await asyncio.gather(
        plugin._start_read_aloud(bot),
        plugin._start_read_aloud(bot),
    ) == [True, True]
    adapter = plugin.read_aloud_adapter
    repository = plugin.read_aloud_repository
    preset_repository = plugin.read_aloud_preset_repository
    assert adapter is not None
    assert repository is not None
    assert preset_repository is not None
    assert bot.music_read_aloud_service is adapter
    assert bot.music_read_aloud_repository is repository
    assert bot.music_read_aloud_preset_repository is preset_repository
    assert listeners == [(adapter.on_message, "on_message")]

    await asyncio.gather(
        plugin._disable_read_aloud(bot),
        plugin._disable_read_aloud(bot),
    )
    assert not hasattr(bot, "music_read_aloud_service")
    assert not hasattr(bot, "music_read_aloud_repository")
    assert not hasattr(bot, "music_read_aloud_preset_repository")
    assert listeners == []
    assert not repository.is_open
    assert not preset_repository.is_open
    plugin._read_aloud_accepting_starts = False
    assert not await plugin._start_read_aloud(bot)
    assert listeners == []


@pytest.mark.asyncio
async def test_plugin_closes_preset_repository_when_listener_registration_is_unavailable(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []

    class TrackingPresetRepository:
        def __init__(self, _path: Any) -> None:
            self.is_open = False
            instances.append(self)

        def open(self) -> None:
            self.is_open = True

        def close(self) -> None:
            self.is_open = False

    monkeypatch.setattr(
        "yonerai_discord.modules.music.SqliteVoicePresetRepository",
        TrackingPresetRepository,
    )
    service = FakeMusicService()
    queue = FakeSpeechQueue()
    bot = FakeBot(FakeGuild(), service, queue)
    bot.settings = SimpleNamespace(
        music_read_aloud_enabled=True,
        music_database_path=tmp_path / "music.sqlite3",
    )
    plugin = MusicPlugin()
    plugin.bot = bot
    plugin.service = service  # type: ignore[assignment]
    plugin.repository = object()  # type: ignore[assignment]
    plugin._read_aloud_accepting_starts = True

    assert not await plugin._start_read_aloud(bot)
    assert len(instances) == 1
    assert instances[0].is_open is False
    assert plugin.read_aloud_preset_repository is None
    assert not hasattr(bot, "music_read_aloud_preset_repository")
