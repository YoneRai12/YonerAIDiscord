from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image

from yonerai_discord.modules.ai import discord_renderer as renderer_module
from yonerai_discord.modules.ai.artifacts import ArtifactStore
from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.ai.display_preferences import DisplayMode
from yonerai_discord.modules.ai.site_delivery import PublishedSite
from yonerai_discord.modules.media_pipeline import ArtifactKind, canonicalize_image
from yonerai_discord.modules.media_pipeline.delivery import PreparedMediaAttachment


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=2)


class FakeMessage:
    def __init__(self, *, fail_reply: bool = False) -> None:
        self.channel = FakeChannel()
        self.replies: list[dict] = []
        self.fail_reply = fail_reply

    async def reply(self, **kwargs):
        if self.fail_reply:
            raise RuntimeError("simulated reply failure")
        self.replies.append(kwargs)
        return SimpleNamespace(id=1)


class FakeEditableMessage:
    def __init__(self) -> None:
        self.id = 3
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class FakeFollowup:
    def __init__(self, *, fail: bool = False, fail_once: bool = False) -> None:
        self.sent: list[dict] = []
        self.attempts: list[dict] = []
        self.fail = fail
        self.fail_once = fail_once

    async def send(self, **kwargs):
        self.attempts.append(kwargs)
        if self.fail or (self.fail_once and len(self.attempts) == 1):
            raise RuntimeError("simulated webhook failure")
        self.sent.append(kwargs)
        return SimpleNamespace(id=4)


class FakeReferencedMessage(FakeMessage):
    def __init__(self, *, fail_reply: bool = False) -> None:
        super().__init__(fail_reply=fail_reply)
        self.reference = object()

    def to_reference(self, *, fail_if_not_exists: bool):
        assert fail_if_not_exists is False
        return self.reference


class HangingReplyMessage(FakeMessage):
    async def reply(self, **kwargs):
        await asyncio.Event().wait()


class HangingEditableMessage(FakeEditableMessage):
    async def edit(self, **kwargs):
        await asyncio.Event().wait()


class TimeoutOnceEditableMessage(FakeEditableMessage):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def edit(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await asyncio.Event().wait()
        return await super().edit(**kwargs)


class HangingFollowup(FakeFollowup):
    async def send(self, **kwargs):
        self.attempts.append(kwargs)
        await asyncio.Event().wait()


def assert_no_mentions(kwargs: dict) -> None:
    allowed = kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def _media_attachment(index: int) -> PreparedMediaAttachment:
    image = Image.new("RGB", (index + 1, index + 1), color=(index, index, index))
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


def test_oversized_html_is_not_added_as_a_null_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_module, "_MAX_ATTACHMENT_BYTES", 16)

    prepared = DiscordAIResponseRenderer().prepare_payload(
        "<!doctype html><html><body>too large</body></html>",
        model="gpt-5.6-terra",
    )

    assert "files" not in prepared.kwargs
    assert prepared.attachment_filenames == ()


@pytest.mark.asyncio
async def test_short_text_uses_embed_card_and_never_mentions() -> None:
    message = FakeMessage()
    result = await DiscordAIResponseRenderer().reply(message, "こんにちは @everyone", model="gpt-5.6-terra")

    assert result.primary_message.id == 1
    assert result.full_text == "こんにちは @everyone"
    kwargs = message.replies[0]
    assert kwargs["embed"].description == "こんにちは @everyone"
    assert kwargs["embed"].footer.text == "モデル: gpt-5.6-terra"
    assert_no_mentions(kwargs)


@pytest.mark.asyncio
async def test_hung_reply_times_out_without_risking_duplicate_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_module, "_DISCORD_IO_TIMEOUT_SECONDS", 0.01)
    message = HangingReplyMessage()

    result = await DiscordAIResponseRenderer().reply(message, "応答", model="gpt-5.6-terra")

    assert result.primary_message is None
    assert message.channel.sent == []


@pytest.mark.asyncio
async def test_hung_progress_edit_keeps_same_message_without_duplicate_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_module, "_DISCORD_IO_TIMEOUT_SECONDS", 0.01)
    message = FakeMessage()

    result = await DiscordAIResponseRenderer().reply(
        message,
        "応答",
        model="gpt-5.6-terra",
        existing_message=HangingEditableMessage(),
    )

    assert result.primary_message is None
    assert result.reused_message is False
    assert message.replies == []


@pytest.mark.asyncio
async def test_timed_out_progress_edit_retries_only_same_message_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_module, "_DISCORD_IO_TIMEOUT_SECONDS", 0.01)
    message = FakeMessage()
    existing = TimeoutOnceEditableMessage()

    result = await DiscordAIResponseRenderer().reply(
        message,
        "応答",
        model="gpt-5.6-terra",
        existing_message=existing,
    )

    assert result.primary_message is existing
    assert result.reused_message is True
    assert existing.calls == 2
    assert len(existing.edits) == 1
    assert message.replies == []


@pytest.mark.asyncio
async def test_timed_out_progress_edit_does_not_retry_after_authorization_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(renderer_module, "_DISCORD_IO_TIMEOUT_SECONDS", 0.01)
    message = FakeMessage()
    existing = TimeoutOnceEditableMessage()
    authorization_checks = 0

    def send_allowed() -> bool:
        nonlocal authorization_checks
        authorization_checks += 1
        return authorization_checks == 1

    result = await DiscordAIResponseRenderer().reply(
        message,
        "応答",
        model="gpt-5.6-terra",
        existing_message=existing,
        send_allowed=send_allowed,
    )

    assert result.primary_message is None
    assert result.reused_message is False
    assert existing.calls == 1
    assert message.replies == []


@pytest.mark.parametrize("display_mode", [DisplayMode.CARD, DisplayMode.PLAIN])
@pytest.mark.parametrize("count", [1, 4])
@pytest.mark.asyncio
async def test_reply_attaches_prepared_media_without_exposing_bytes(
    display_mode: DisplayMode,
    count: int,
) -> None:
    message = FakeMessage()
    attachments = tuple(_media_attachment(index) for index in range(1, count + 1))

    result = await DiscordAIResponseRenderer().reply(
        message,
        "画像を準備しました。",
        model="gpt-5.6-terra",
        display_mode=display_mode,
        media_attachments=attachments,
        fresh_send_allowed=lambda: True,
    )

    assert result.primary_message.id == 1
    files = message.replies[0]["files"]
    assert [file.filename for file in files] == [item.filename for item in attachments]
    assert [file.fp.read() for file in files] == [item.data for item in attachments]
    assert all(item.data not in repr(result).encode("utf-8") for item in attachments)
    assert_no_mentions(message.replies[0])


def test_prepared_media_obeys_existing_attachment_total_limit() -> None:
    fence = "`" * 3
    content = "\n".join(f"{fence}python\n{'x' * 1_300}\n{fence}" for _ in range(5))
    attachments = tuple(_media_attachment(index) for index in range(1, 5))

    prepared = DiscordAIResponseRenderer().prepare_payload(
        content,
        model="gpt-5.6-sol",
        media_attachments=attachments,
    )

    assert len(prepared.kwargs["files"]) == 10
    assert set(item.filename for item in attachments).issubset(prepared.attachment_filenames)


def test_prepared_media_invalid_or_over_limit_fails_closed() -> None:
    first = _media_attachment(1)
    duplicate = PreparedMediaAttachment(
        filename=first.filename,
        data=first.data,
        media_type=first.media_type,
        kind=first.kind,
        width=first.width,
        height=first.height,
    )
    invalid = _media_attachment(2)
    object.__setattr__(invalid, "data", b"not a PNG")
    renderer = DiscordAIResponseRenderer()

    with pytest.raises(ValueError):
        renderer.prepare_payload("answer", model="gpt-5.6-terra", media_attachments=(first, duplicate))
    with pytest.raises(ValueError):
        renderer.prepare_payload("answer", model="gpt-5.6-terra", media_attachments=(invalid,))
    with pytest.raises(ValueError):
        renderer.prepare_payload(
            "answer",
            model="gpt-5.6-terra",
            media_attachments=tuple(_media_attachment(index) for index in range(1, 6)),
        )


@pytest.mark.asyncio
async def test_long_text_attaches_full_utf8_markdown() -> None:
    message = FakeMessage()
    content = "あ" * 5_000
    result = await DiscordAIResponseRenderer().reply(message, content, model="gpt-5.6-terra")

    kwargs = message.replies[0]
    assert "yonerai-answer.md" in result.attachment_filenames
    file = kwargs["files"][0]
    assert file.filename == "yonerai-answer.md"
    assert file.fp.read().decode("utf-8") == content
    assert "全文は UTF-8" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_card_total_budget_accounts_for_site_notice_and_task_summary() -> None:
    message = FakeMessage()
    # description個別上限内でも、fields/footer込みの総量で退避が必要になる境界。
    content = "界" * 4_096
    site_notice = "公開状態の説明" * 200
    task_summary = "完了した工程" * 200

    result = await DiscordAIResponseRenderer().reply(
        message,
        content,
        model="gpt-5.6-sol",
        site_publish_notice=site_notice,
        task_summary=task_summary,
    )

    kwargs = message.replies[0]
    embed = kwargs["embed"]
    total = (
        len(embed.title)
        + len(embed.description)
        + len(embed.footer.text)
        + sum(len(field.name) + len(field.value) for field in embed.fields)
    )
    assert total <= 6_000
    assert len(embed.description) <= 4_096
    assert [field.name for field in embed.fields] == ["サイト公開", "完了したタスク"]
    markdown = next(file for file in kwargs["files"] if file.filename == "yonerai-answer.md")
    assert markdown.fp.read().decode("utf-8") == content
    assert "yonerai-answer.md" in result.attachment_filenames


def test_long_markdown_failure_closes_existing_attachment_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = DiscordAIResponseRenderer()
    created_files = []
    original_file_from_text = renderer_module._file_from_text

    def tracked_file_from_text(text: str, filename: str):
        file = original_file_from_text(text, filename)
        if file is not None:
            created_files.append(file)
        return file

    monkeypatch.setattr(renderer_module, "_MAX_ATTACHMENT_BYTES", 1_024)
    monkeypatch.setattr(renderer_module, "_file_from_text", tracked_file_from_text)
    content = ("説明" * 3_000) + "\n```html\n<!doctype html><html><body>ok</body></html>\n```"

    with pytest.raises(ValueError, match="full AI response attachment is unavailable"):
        renderer.prepare_payload(content, model="gpt-5.6-sol")

    assert [file.filename for file in created_files] == ["yonerai-web.html"]
    assert created_files[0].fp.closed is True


def test_card_budget_exception_closes_generated_attachment_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = DiscordAIResponseRenderer()
    created_files = []
    original_file_from_text = renderer_module._file_from_text
    published = PublishedSite(
        site_id="site123",
        release_id="release123",
        slug="blue-clock",
        site_url="https://publish.example.test/blue-clock/",
        revision=2,
        visibility="unlisted",
        updated=True,
    )

    def tracked_file_from_text(text: str, filename: str):
        file = original_file_from_text(text, filename)
        if file is not None:
            created_files.append(file)
        return file

    def fail_budget(**kwargs):
        del kwargs
        raise RuntimeError("simulated budget failure")

    monkeypatch.setattr(renderer_module, "_file_from_text", tracked_file_from_text)
    monkeypatch.setattr(renderer_module, "validate_discord_payload_budget", fail_budget)

    with pytest.raises(RuntimeError, match="simulated budget failure"):
        renderer.prepare_payload(
            "<!doctype html><html><body>ok</body></html>",
            model="gpt-5.6-sol",
            published_site=published,
        )

    assert [file.filename for file in created_files] == ["yonerai-web.html"]
    assert created_files[0].fp.closed is True


@pytest.mark.asyncio
async def test_followup_card_keeps_4096_limit_full_utf8_and_no_reply_only_kwargs() -> None:
    followup = FakeFollowup()
    content = "長文😀" * 2_000

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        content,
        model="gpt-5.6-sol",
        display_mode=DisplayMode.CARD,
    )

    assert result.primary_message.id == 4
    kwargs = followup.sent[0]
    assert "mention_author" not in kwargs
    assert kwargs["ephemeral"] is True
    assert_no_mentions(kwargs)
    assert len(kwargs["embed"].description) <= 4_096
    assert kwargs["embed"].footer.text == "モデル: gpt-5.6-sol"
    markdown = next(file for file in kwargs["files"] if file.filename == "yonerai-answer.md")
    assert markdown.fp.read().decode("utf-8") == content


@pytest.mark.asyncio
async def test_followup_plain_keeps_2000_limit_html_attachment_and_numbered_link() -> None:
    followup = FakeFollowup()
    html = "<!doctype html><html><body>猫😀</body></html>"
    content = ("説明 " * 600) + f"\n[1](https://example.com/source)\n```html\n{html}\n```"

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        content,
        model="gpt-5.6-terra",
        display_mode=DisplayMode.PLAIN,
    )

    assert result.primary_message.id == 4
    kwargs = followup.sent[0]
    assert "mention_author" not in kwargs
    assert kwargs["ephemeral"] is True
    assert len(kwargs["content"]) <= 2_000
    assert "[1](https://example.com/source)" in kwargs["content"]
    html_file = next(file for file in kwargs["files"] if file.filename.endswith(".html"))
    assert html_file.fp.read().decode("utf-8") == html
    assert_no_mentions(kwargs)


@pytest.mark.asyncio
async def test_followup_send_failure_returns_safe_empty_primary_without_content_logging(caplog) -> None:
    followup = FakeFollowup(fail=True)
    secret_like_text = "OPENAI_API_KEY=sk-should-never-appear"

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        secret_like_text,
        model="gpt-5.6-terra",
    )

    assert result.primary_message is None
    assert len(followup.attempts) == 2
    assert "OPENAI_API_KEY" not in str(followup.attempts[1])
    assert secret_like_text not in caplog.text


@pytest.mark.asyncio
async def test_hung_followup_times_out_without_duplicate_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_module, "_DISCORD_IO_TIMEOUT_SECONDS", 0.01)
    followup = HangingFollowup()

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        "応答",
        model="gpt-5.6-terra",
    )

    assert result.primary_message is None
    assert len(followup.attempts) == 1


@pytest.mark.asyncio
async def test_followup_rich_payload_failure_sends_safe_terminal_fallback() -> None:
    followup = FakeFollowup(fail_once=True)

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        "private generated answer",
        model="gpt-5.6-terra",
    )

    assert result.primary_message is not None
    assert len(followup.attempts) == 2
    fallback = followup.sent[0]
    assert "Discordへ安全に表示できません" in fallback["content"]
    assert "private generated answer" not in fallback["content"]
    assert "embed" not in fallback
    assert "files" not in fallback
    assert_no_mentions(fallback)


@pytest.mark.asyncio
async def test_followup_accepts_prepared_media_and_closes_it_on_failure() -> None:
    attachment = _media_attachment(1)
    followup = FakeFollowup(fail=True)

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        "answer",
        model="gpt-5.6-terra",
        media_attachments=(attachment,),
        fresh_send_allowed=lambda: True,
    )

    assert result.primary_message is None
    first_file = followup.attempts[0]["files"][0]
    assert first_file.fp.closed is True
    assert "files" not in followup.attempts[1]
    assert_no_mentions(followup.attempts[1])


@pytest.mark.parametrize("fresh_send_allowed", [None, lambda: False])
@pytest.mark.asyncio
async def test_followup_media_requires_fresh_authorization(
    fresh_send_allowed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    followup = FakeFollowup()
    created_files = []
    original_file = renderer_module._file_from_media_attachment

    def capture_file(item):
        file = original_file(item)
        created_files.append(file)
        return file

    monkeypatch.setattr(renderer_module, "_file_from_media_attachment", capture_file)

    result = await DiscordAIResponseRenderer().send_followup(
        followup,
        "answer",
        model="gpt-5.6-terra",
        media_attachments=(_media_attachment(1),),
        fresh_send_allowed=fresh_send_allowed,
    )

    assert result.primary_message is None
    assert followup.attempts == []
    assert len(created_files) == 1
    assert created_files[0].fp.closed is True


@pytest.mark.asyncio
async def test_html_is_attached_and_stored_without_publication(tmp_path) -> None:
    message = FakeMessage()
    renderer = DiscordAIResponseRenderer(
        artifact_store=ArtifactStore(tmp_path / "data" / "artifacts"), artifact_scope="guild-1"
    )
    html = "```html\n<!doctype html><html><body>猫</body></html>\n```"
    result = await renderer.reply(message, html, model="gpt-5.6-sol", prompt="猫カフェ WEBを作って")

    assert result.artifact is not None
    assert result.artifact.path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert result.artifact.filename in result.attachment_filenames
    assert any(file.filename.endswith(".html") for file in message.replies[0]["files"])
    description = message.replies[0]["embed"].description
    assert description == "HTML成果物を生成しました。\n\n関連ファイルを添付しました。"
    assert "<!doctype html>" not in description


@pytest.mark.asyncio
async def test_multiple_or_long_code_becomes_utf8_file() -> None:
    message = FakeMessage()
    answer = "```python\n" + ("print('x')\n" * 200) + "```\n```json\n{}\n```"
    result = await DiscordAIResponseRenderer().reply(message, answer, model="gpt-5.6-sol")

    assert {"yonerai-code-1.py", "yonerai-code-2.json"}.issubset(result.attachment_filenames)
    assert_no_mentions(message.replies[0])


def test_attachment_candidates_are_capped_at_ten_with_full_answer_preserved() -> None:
    fence = "`" * 3
    content = "\n".join(f"{fence}python\n# block {index}\n{'x' * 1_300}\n{fence}" for index in range(11))

    prepared = DiscordAIResponseRenderer().prepare_payload(
        content,
        model="gpt-5.6-sol",
        display_mode=DisplayMode.CARD,
    )

    files = prepared.kwargs["files"]
    assert len(files) == 10
    assert prepared.attachment_filenames[0] == "yonerai-answer.md"
    assert len(prepared.attachment_filenames) == 10
    assert files[0].fp.read().decode("utf-8") == content


@pytest.mark.parametrize("display_mode", [DisplayMode.CARD, DisplayMode.PLAIN])
@pytest.mark.asyncio
async def test_reply_failure_uses_channel_fallback_and_keeps_full_text(display_mode: DisplayMode) -> None:
    message = FakeMessage(fail_reply=True)
    result = await DiscordAIResponseRenderer().reply(
        message,
        "回答",
        model="gpt-5.6-terra",
        display_mode=display_mode,
    )

    assert result.primary_message.id == 2
    assert result.full_text == "回答"
    assert_no_mentions(message.channel.sent[0])


@pytest.mark.asyncio
async def test_reply_failure_rechecks_authorization_before_channel_fallback() -> None:
    allowed = [True]

    class RevokingMessage(FakeMessage):
        async def reply(self, **kwargs):
            del kwargs
            allowed[0] = False
            raise RuntimeError("simulated reply failure")

    message = RevokingMessage()
    result = await DiscordAIResponseRenderer().reply(
        message,
        "private generated answer",
        model="gpt-5.6-terra",
        send_allowed=lambda: allowed[0],
    )

    assert result.primary_message is None
    assert message.channel.sent == []


@pytest.mark.asyncio
async def test_reply_rechecks_async_authorization_immediately_before_final_delivery() -> None:
    source = FakeMessage()
    allowed = [True]

    async def fresh_allowed() -> bool:
        return allowed[0]

    allowed[0] = False
    result = await DiscordAIResponseRenderer().reply(
        source,
        "private generated answer",
        model="gpt-5.6-terra",
        fresh_send_allowed=fresh_allowed,
    )

    assert result.primary_message is None
    assert source.replies == []


@pytest.mark.asyncio
async def test_fresh_revoke_closes_prepared_media_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FakeMessage()
    attachment = _media_attachment(1)
    created_files = []

    async def fresh_allowed() -> bool:
        return False

    original_file = renderer_module._file_from_media_attachment

    def capture_file(item):
        file = original_file(item)
        created_files.append(file)
        return file

    monkeypatch.setattr(renderer_module, "_file_from_media_attachment", capture_file)
    result = await DiscordAIResponseRenderer().reply(
        source,
        "private generated answer",
        model="gpt-5.6-terra",
        fresh_send_allowed=fresh_allowed,
        media_attachments=(attachment,),
    )

    assert result.primary_message is None
    assert source.replies == []
    assert len(created_files) == 1
    assert created_files[0].fp.closed is True


@pytest.mark.asyncio
async def test_reply_failure_rewinds_media_before_channel_fallback() -> None:
    attachment = _media_attachment(1)

    class ConsumingFailureMessage(FakeMessage):
        async def reply(self, **kwargs):
            for file in kwargs["files"]:
                assert file.fp.read() == attachment.data
            raise RuntimeError("simulated reply failure")

    source = ConsumingFailureMessage()
    result = await DiscordAIResponseRenderer().reply(
        source,
        "answer",
        model="gpt-5.6-terra",
        media_attachments=(attachment,),
        fresh_send_allowed=lambda: True,
    )

    assert result.primary_message.id == 2
    file = source.channel.sent[0]["files"][0]
    assert file.fp.read() == attachment.data
    assert_no_mentions(source.channel.sent[0])


@pytest.mark.asyncio
async def test_all_delivery_failures_close_prepared_media_streams() -> None:
    attachment = _media_attachment(1)

    class FailingChannel(FakeChannel):
        async def send(self, **kwargs):
            self.sent.append(kwargs)
            raise RuntimeError("simulated channel failure")

    class FailingMessage(FakeMessage):
        def __init__(self) -> None:
            super().__init__(fail_reply=True)
            self.channel = FailingChannel()

    source = FailingMessage()
    result = await DiscordAIResponseRenderer().reply(
        source,
        "answer",
        model="gpt-5.6-terra",
        media_attachments=(attachment,),
        fresh_send_allowed=lambda: True,
    )

    assert result.primary_message is None
    file = source.channel.sent[0]["files"][0]
    assert file.fp.closed is True


@pytest.mark.asyncio
async def test_final_answer_reuses_progress_message_and_keeps_task_summary() -> None:
    source = FakeMessage()
    progress = FakeEditableMessage()

    result = await DiscordAIResponseRenderer().reply(
        source,
        "完成しました。",
        model="gpt-5.6-sol",
        existing_message=progress,
        task_summary="1. ✅ **依頼を解析**\n2. ✅ **回答を生成**",
    )

    assert result.primary_message is progress
    assert result.reused_message is True
    assert source.replies == []
    final_embed = progress.edits[0]["embed"]
    assert final_embed.description == "完成しました。"
    assert final_embed.fields[0].name == "完了したタスク"
    assert "回答を生成" in final_embed.fields[0].value


@pytest.mark.asyncio
async def test_existing_message_edit_accepts_prepared_media_attachment() -> None:
    source = FakeMessage()
    progress = FakeEditableMessage()
    attachment = _media_attachment(1)

    result = await DiscordAIResponseRenderer().reply(
        source,
        "画像を準備しました。",
        model="gpt-5.6-sol",
        existing_message=progress,
        media_attachments=(attachment,),
        fresh_send_allowed=lambda: True,
    )

    assert result.reused_message is True
    assert source.replies == []
    file = progress.edits[0]["attachments"][0]
    assert file.filename == attachment.filename
    assert file.fp.read() == attachment.data


@pytest.mark.asyncio
async def test_reply_failure_keeps_reply_chain_on_channel_fallback() -> None:
    message = FakeReferencedMessage(fail_reply=True)

    result = await DiscordAIResponseRenderer().reply(message, "回答", model="gpt-5.6-terra")

    assert result.primary_message.id == 2
    assert message.channel.sent[0]["reference"] is message.reference


@pytest.mark.asyncio
async def test_existing_message_edit_accepts_new_html_attachment(tmp_path) -> None:
    source = FakeMessage()
    progress = FakeEditableMessage()
    renderer = DiscordAIResponseRenderer(artifact_store=ArtifactStore(tmp_path / "artifacts"))

    result = await renderer.reply(
        source,
        "```html\n<!doctype html><html><body>完成</body></html>\n```",
        model="gpt-5.6-sol",
        existing_message=progress,
    )

    assert result.reused_message is True
    attachments = progress.edits[0]["attachments"]
    assert len(attachments) == 1
    assert attachments[0].filename.endswith(".html")


@pytest.mark.asyncio
async def test_published_site_card_contains_version_url_and_reply_edit_guidance() -> None:
    message = FakeMessage()
    published = PublishedSite(
        site_id="site123",
        release_id="release123",
        slug="blue-clock",
        site_url="https://publish.example.test/blue-clock/",
        revision=2,
        visibility="unlisted",
        updated=True,
    )

    result = await DiscordAIResponseRenderer().reply(
        message,
        "<!doctype html><html><body>clock</body></html>",
        model="gpt-5.6-sol",
        published_site=published,
    )

    field = message.replies[0]["embed"].fields[0]
    assert field.name == "サイトを更新しました"
    assert f"公開サイト: {published.site_url}" in field.value
    assert "v2" in field.value
    assert "このカードへ返信" in field.value
    assert result.published_site is published


@pytest.mark.asyncio
async def test_published_site_card_shows_full_url_after_description_sources() -> None:
    message = FakeMessage()
    published = PublishedSite(
        site_id="site123",
        release_id="release123",
        slug="blue-clock",
        site_url="https://publish.example.test/blue-clock/",
        revision=2,
        visibility="unlisted",
        updated=False,
    )

    await DiscordAIResponseRenderer().reply(
        message,
        "参照: [1](https://example.com/one) [3](https://example.com/three)",
        model="gpt-5.6-sol",
        published_site=published,
    )

    field = message.replies[0]["embed"].fields[0]
    assert f"公開サイト: {published.site_url}" in field.value
    assert "公開サイト: [" not in field.value


@pytest.mark.asyncio
async def test_published_site_card_shows_full_url_with_late_description_sources() -> None:
    message = FakeMessage()
    published = PublishedSite(
        site_id="site123",
        release_id="release123",
        slug="blue-clock",
        site_url="https://publish.example.test/blue-clock/",
        revision=2,
        visibility="unlisted",
        updated=False,
    )

    await DiscordAIResponseRenderer().reply(
        message,
        ("本文" * 2_000) + " [9](https://example.com/late-source)",
        model="gpt-5.6-sol",
        published_site=published,
    )

    field = message.replies[0]["embed"].fields[0]
    assert f"公開サイト: {published.site_url}" in field.value
