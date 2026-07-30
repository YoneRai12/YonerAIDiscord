from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from yonerai_discord.modules.media_pipeline import ArtifactKind, canonicalize_image
from yonerai_discord.modules.media_pipeline.delivery import PreparedMediaAttachment
from yonerai_discord.modules.media_pipeline.discord_delivery import (
    DiscordExactMessageMediaDeliverySink,
    DiscordMediaDeliveryCommitUncertainError,
)
from yonerai_discord.modules.media_pipeline.durable_delivery import (
    MediaDeliveryRejectedError,
    MediaDeliverySinkRequest,
    MediaDeliveryTargetLease,
    MediaDeliveryTransientError,
)


GUILD_ID = 10
CHANNEL_ID = 20
MESSAGE_ID = 30
BOT_USER_ID = 40


class FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeMessage:
    def __init__(
        self,
        *,
        guild_id: int | None = GUILD_ID,
        channel_id: int = CHANNEL_ID,
        message_id: int = MESSAGE_ID,
    ) -> None:
        self.id = message_id
        self.guild = None if guild_id is None else SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=channel_id)
        self.author = SimpleNamespace(id=BOT_USER_ID, bot=True)
        self.content = "処理中"
        self.edited_at = None
        self.attachments = []
        self.edit_calls = 0
        self.edit_error: Exception | None = None
        self.result_override: object | None = None
        self.file_streams = []
        self.file_payloads: list[bytes] = []
        self.file_names: list[str] = []
        self.allowed_mentions = None

    async def edit(self, **kwargs):
        self.edit_calls += 1
        files = kwargs["attachments"]
        self.allowed_mentions = kwargs["allowed_mentions"]
        self.file_streams = [file.fp for file in files]
        self.file_names = [file.filename for file in files]
        self.file_payloads = []
        for file in files:
            file.reset(seek=0)
            self.file_payloads.append(file.fp.read())
        if self.edit_error is not None:
            raise self.edit_error
        if self.result_override is not None:
            return self.result_override
        return _result_message(
            guild_id=None if self.guild is None else self.guild.id,
            channel_id=self.channel.id,
            message_id=self.id,
            filenames=self.file_names,
        )


class FakeTargetResolver:
    def __init__(self, *messages: FakeMessage) -> None:
        self.messages = {
            (
                None if message.guild is None else message.guild.id,
                message.channel.id,
                message.id,
            ): message
            for message in messages
        }
        self.calls: list[tuple[int | None, int, int]] = []

    async def __call__(self, request: MediaDeliverySinkRequest) -> object:
        key = (request.guild_id, request.channel_id, request.message_id)
        self.calls.append(key)
        return self.messages[key]


def _attachment(index: int) -> PreparedMediaAttachment:
    image = Image.new("RGB", (16 + index, 12 + index), (index * 20, 80, 120))
    try:
        canonical = canonicalize_image(image)
    finally:
        image.close()
    return PreparedMediaAttachment(
        filename=f"media-{index:02d}.png",
        data=canonical.data,
        media_type="image/png",
        kind=ArtifactKind.IMAGE,
        width=canonical.width,
        height=canonical.height,
    )


def _document_attachment(index: int) -> PreparedMediaAttachment:
    return PreparedMediaAttachment(
        filename=f"media-{index:02d}.md",
        data=f"# Comparison {index}\n".encode(),
        media_type="text/markdown; charset=utf-8",
        kind=ArtifactKind.DOCUMENT,
        width=0,
        height=0,
    )


def _request(
    *,
    count: int = 1,
    guild_id: int | None = GUILD_ID,
    channel_id: int = CHANNEL_ID,
    message_id: int = MESSAGE_ID,
    user_id: int = 50,
) -> MediaDeliverySinkRequest:
    return MediaDeliverySinkRequest(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        message_id=message_id,
        delivery_digest="a" * 64,
        required_action_ids=("image.qr",),
        required_capabilities=(
            ("cap-run-ai-mention-chat", 0),
            ("cap-run-media-qr-encode", 10),
        ),
        attachments=tuple(_attachment(index) for index in range(1, count + 1)),
    )


def _result_message(
    *,
    guild_id: int | None,
    channel_id: int,
    message_id: int,
    filenames: list[str],
):
    return SimpleNamespace(
        id=message_id,
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(id=channel_id),
        attachments=[
            SimpleNamespace(id=1000 + index, filename=filename) for index, filename in enumerate(filenames, start=1)
        ],
    )


def _sink(
    *targets: FakeMessage,
    bot=None,
    bot_current=None,
    target_resolver=None,
    authorization_current=None,
) -> DiscordExactMessageMediaDeliverySink:
    bound_bot = bot or SimpleNamespace(user=SimpleNamespace(id=BOT_USER_ID, bot=True))
    resolver = target_resolver or FakeTargetResolver(*targets)

    async def allowed(_request):
        return True

    return DiscordExactMessageMediaDeliverySink(
        bot=bound_bot,
        bot_current=bot_current or (lambda: bound_bot),
        target_resolver=resolver,
        authorization_current=authorization_current or allowed,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 4])
async def test_exact_message_edit_delivers_fixed_png_files_once(count: int) -> None:
    target = FakeMessage()
    checked_bindings: list[tuple[int, tuple[str, ...]]] = []

    async def allowed(request):
        checked_bindings.append((request.user_id, request.required_action_ids))
        return True

    sink = _sink(target, authorization_current=allowed)
    request = _request(count=count)

    lease = await sink.preflight(request)
    receipt = await sink.edit(request, lease=lease)

    assert target.edit_calls == 1
    assert target.file_names == [f"media-{index:02d}.png" for index in range(1, count + 1)]
    assert target.file_payloads == [attachment.data for attachment in request.attachments]
    assert target.allowed_mentions.users is False
    assert receipt.guild_id == GUILD_ID
    assert receipt.channel_id == CHANNEL_ID
    assert receipt.message_id == MESSAGE_ID
    assert receipt.delivery_digest == "a" * 64
    assert receipt.attachment_ids == tuple(1000 + index for index in range(1, count + 1))
    assert all(stream.closed for stream in target.file_streams)
    assert not hasattr(target, "reply")
    assert not hasattr(target.channel, "send")
    assert checked_bindings
    assert set(checked_bindings) == {(50, ("image.qr",))}


@pytest.mark.asyncio
async def test_exact_message_edit_delivers_markdown_with_code_owned_filename() -> None:
    target = FakeMessage()
    sink = _sink(target)
    request = MediaDeliverySinkRequest(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=50,
        message_id=MESSAGE_ID,
        delivery_digest="b" * 64,
        required_action_ids=("artifact.table.create",),
        required_capabilities=(("cap-run-ai-mention-chat", 0),),
        attachments=(_document_attachment(1),),
    )

    receipt = await sink.edit(request, lease=await sink.preflight(request))

    assert target.edit_calls == 1
    assert target.file_names == ["media-01.md"]
    assert target.file_payloads == [b"# Comparison 1\n"]
    assert receipt.attachment_ids == (1001,)
    assert all(stream.closed for stream in target.file_streams)


@pytest.mark.asyncio
async def test_preflight_requires_exact_bot_owned_current_target_and_authorization() -> None:
    target = FakeMessage()
    target.author.bot = False
    request = _request()

    with pytest.raises(MediaDeliveryRejectedError, match="target"):
        await _sink(target).preflight(request)
    assert target.edit_calls == 0

    target.author.bot = True
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_USER_ID, bot=True))
    replacement_bot = SimpleNamespace(user=SimpleNamespace(id=BOT_USER_ID, bot=True))
    with pytest.raises(MediaDeliveryRejectedError, match="bot"):
        await _sink(target, bot=bot, bot_current=lambda: replacement_bot).preflight(request)
    assert target.edit_calls == 0

    async def denied(_request):
        return False

    with pytest.raises(MediaDeliveryRejectedError, match="authorization"):
        await _sink(target, authorization_current=denied).preflight(request)
    assert target.edit_calls == 0

    target.attachments = [SimpleNamespace(id=999, filename="existing.png")]
    with pytest.raises(MediaDeliveryRejectedError, match="target"):
        await _sink(target).preflight(request)
    assert target.edit_calls == 0


@pytest.mark.asyncio
async def test_preflight_timeout_is_the_only_retryable_transport_boundary() -> None:
    target = FakeMessage()

    async def unavailable(_request):
        raise TimeoutError

    with pytest.raises(MediaDeliveryTransientError, match="temporarily"):
        await _sink(target, authorization_current=unavailable).preflight(_request())
    assert target.edit_calls == 0


@pytest.mark.asyncio
async def test_tampered_lease_or_attachment_is_rejected_before_edit() -> None:
    target = FakeMessage()
    sink = _sink(target)
    request = _request()
    lease = await sink.preflight(request)
    changed_lease = MediaDeliveryTargetLease(
        guild_id=lease.guild_id,
        channel_id=lease.channel_id,
        message_id=lease.message_id,
        bot_owned=True,
        token="0" * 64,
    )

    with pytest.raises(MediaDeliveryRejectedError, match="lease"):
        await sink.edit(request, lease=changed_lease)
    assert target.edit_calls == 0

    lease = await sink.preflight(request)
    object.__setattr__(request.attachments[0], "filename", "media-04.png")
    with pytest.raises(MediaDeliveryRejectedError, match="attachment"):
        await sink.edit(request, lease=lease)
    assert target.edit_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        discord.Forbidden(FakeResponse(), "denied"),
        discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing"),
    ],
)
async def test_forbidden_or_not_found_after_edit_start_is_uncertain_and_closes_streams(
    error: Exception,
) -> None:
    target = FakeMessage()
    target.edit_error = error
    sink = _sink(target)
    request = _request(count=2)
    lease = await sink.preflight(request)

    with pytest.raises(DiscordMediaDeliveryCommitUncertainError, match="uncertain") as caught:
        await sink.edit(request, lease=lease)

    assert target.edit_calls == 1
    assert all(stream.closed for stream in target.file_streams)
    assert "denied" not in str(caught.value)
    assert "missing" not in str(caught.value)
    assert request.delivery_digest not in str(caught.value)


@pytest.mark.asyncio
async def test_result_scope_filename_and_positive_attachment_ids_are_exact() -> None:
    target = FakeMessage()
    request = _request()
    target.result_override = SimpleNamespace(
        id=MESSAGE_ID,
        guild=SimpleNamespace(id=GUILD_ID),
        channel=SimpleNamespace(id=CHANNEL_ID),
        attachments=[SimpleNamespace(id=0, filename="leaked-ref.png")],
    )
    sink = _sink(target)
    lease = await sink.preflight(request)

    with pytest.raises(DiscordMediaDeliveryCommitUncertainError, match="receipt"):
        await sink.edit(request, lease=lease)

    assert target.edit_calls == 1
    assert all(stream.closed for stream in target.file_streams)


@pytest.mark.asyncio
async def test_dm_scope_is_supported_without_guild_substitution() -> None:
    target = FakeMessage(guild_id=None)
    request = _request(guild_id=None)
    sink = _sink(target)

    receipt = await sink.edit(request, lease=await sink.preflight(request))

    assert receipt.guild_id is None
    assert receipt.channel_id == CHANNEL_ID
    assert target.edit_calls == 1


@pytest.mark.asyncio
async def test_one_static_sink_resolves_multiple_durable_targets_after_restart() -> None:
    first = FakeMessage(message_id=30)
    second = FakeMessage(message_id=31)
    resolver = FakeTargetResolver(first, second)
    sink = _sink(first, second, target_resolver=resolver)
    first_request = _request(message_id=30, user_id=50)
    second_request = _request(message_id=31, user_id=51)

    first_receipt = await sink.edit(
        first_request,
        lease=await sink.preflight(first_request),
    )
    second_receipt = await sink.edit(
        second_request,
        lease=await sink.preflight(second_request),
    )

    assert first_receipt.message_id == 30
    assert second_receipt.message_id == 31
    assert first.edit_calls == 1
    assert second.edit_calls == 1
    assert resolver.calls == [
        (GUILD_ID, CHANNEL_ID, 30),
        (GUILD_ID, CHANNEL_ID, 30),
        (GUILD_ID, CHANNEL_ID, 31),
        (GUILD_ID, CHANNEL_ID, 31),
    ]


@pytest.mark.asyncio
async def test_target_revision_change_after_preflight_is_rejected_before_edit() -> None:
    target = FakeMessage()
    sink = _sink(target)
    request = _request()
    lease = await sink.preflight(request)
    target.content = "別の処理"

    with pytest.raises(MediaDeliveryRejectedError, match="lease"):
        await sink.edit(request, lease=lease)

    assert target.edit_calls == 0


def test_sink_repr_and_validation_errors_do_not_expose_media_data_or_digest() -> None:
    target = FakeMessage()
    sink = _sink(target)
    request = _request()
    secret_marker = request.attachments[0].data.hex()[:32]

    assert secret_marker not in repr(sink)
    assert request.delivery_digest not in repr(sink)
    with pytest.raises(TypeError) as caught:
        DiscordExactMessageMediaDeliverySink(
            bot=SimpleNamespace(user=SimpleNamespace(id=0, bot=True)),
            bot_current=lambda: target,
            target_resolver=FakeTargetResolver(target),
            authorization_current=lambda _request: True,
        )
    assert secret_marker not in str(caught.value)
    assert request.delivery_digest not in str(caught.value)
