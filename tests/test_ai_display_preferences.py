from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.ai.display_preferences import (
    DisplayMode,
    DisplayPreferenceAction,
    DisplayPreferenceStore,
    effective_display_mode,
    parse_display_preference_command,
)
from yonerai_discord.modules.ai.site_delivery import PublishedSite
from yonerai_discord.modules.ai.state_repository import AIStateRepository
from yonerai_discord.modules.ai.task_routing import classify_ai_task


class FakeMessage:
    def __init__(self) -> None:
        self.replies = []
        self.channel = SimpleNamespace(send=None)

    async def reply(self, **kwargs):
        self.replies.append(kwargs)
        return SimpleNamespace(id=1)


class FakeEditableMessage:
    def __init__(self) -> None:
        self.id = 2
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


@pytest.mark.parametrize(
    ("text", "action", "mode"),
    [
        ("カード表示にして", DisplayPreferenceAction.SET, DisplayMode.CARD),
        ("カード型の表示にして", DisplayPreferenceAction.SET, DisplayMode.CARD),
        ("カード型に切り替えて", DisplayPreferenceAction.SET, DisplayMode.CARD),
        ("普通の表示にして", DisplayPreferenceAction.SET, DisplayMode.PLAIN),
        ("普通の表示に切り替えて", DisplayPreferenceAction.SET, DisplayMode.PLAIN),
        ("普通のに戻して", DisplayPreferenceAction.SET, DisplayMode.PLAIN),
        ("通常表示に戻して", DisplayPreferenceAction.SET, DisplayMode.PLAIN),
        ("自動表示にして", DisplayPreferenceAction.SET, DisplayMode.AUTO),
        ("今の表示モードを確認して", DisplayPreferenceAction.SHOW, None),
    ],
)
def test_parse_explicit_local_display_commands(text, action, mode) -> None:
    command = parse_display_preference_command(text)
    assert command is not None
    assert (command.action, command.mode) == (action, mode)


def test_display_command_parser_does_not_capture_ordinary_questions() -> None:
    assert parse_display_preference_command("カード表示の実装方法を説明して") is None
    assert parse_display_preference_command("普通の表示とカードを比較して") is None
    assert parse_display_preference_command("サイトをカード型の表示にして公開する方法を説明して") is None


def test_display_preference_persists_by_user(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    first_repository = AIStateRepository(path)
    first = DisplayPreferenceStore(first_repository)
    assert first.get(100) is DisplayMode.AUTO
    first.set(100, DisplayMode.PLAIN)
    first.set(200, DisplayMode.CARD)
    first.set(300, DisplayMode.PLAIN)
    first.set(300, DisplayMode.AUTO)
    first_repository.close()

    second_repository = AIStateRepository(path)
    try:
        second = DisplayPreferenceStore(second_repository)
        assert second.get(100) is DisplayMode.PLAIN
        assert second.get(200) is DisplayMode.CARD
        assert second.get(300) is DisplayMode.AUTO
        assert second.get(400) is DisplayMode.AUTO
    finally:
        second_repository.close()


def test_auto_uses_plain_only_for_short_direct_conversation() -> None:
    short = classify_ai_task("今日は元気？")
    code = classify_ai_task("コードを書いて詳しく分析して")
    web = classify_ai_task("Webで検索して", web_search=True)
    assert effective_display_mode(DisplayMode.AUTO, route=short, content="元気です") is DisplayMode.PLAIN
    assert effective_display_mode(DisplayMode.AUTO, route=code, content="回答") is DisplayMode.CARD
    assert effective_display_mode(DisplayMode.AUTO, route=web, content="回答") is DisplayMode.CARD
    assert (
        effective_display_mode(DisplayMode.AUTO, route=short, content="回答", has_site_result=True) is DisplayMode.CARD
    )
    assert effective_display_mode(DisplayMode.AUTO, route=short, content="回答" * 600) is DisplayMode.CARD
    assert effective_display_mode(DisplayMode.PLAIN, route=web, content="回答") is DisplayMode.PLAIN
    assert effective_display_mode(DisplayMode.CARD, route=short, content="回答") is DisplayMode.CARD


@pytest.mark.asyncio
async def test_plain_renderer_uses_content_without_mentions() -> None:
    message = FakeMessage()
    result = await DiscordAIResponseRenderer().reply(
        message,
        "こんにちは @everyone",
        model="gpt-5.6-terra",
        display_mode=DisplayMode.PLAIN,
    )
    kwargs = message.replies[0]
    assert "embed" not in kwargs
    assert kwargs["content"] == "こんにちは @everyone\n\n-# gpt-5.6-terra"
    assert len(kwargs["content"]) <= 2_000
    assert kwargs["allowed_mentions"].everyone is False
    assert result.full_text == "こんにちは @everyone"


@pytest.mark.asyncio
async def test_plain_long_answer_is_utf8_attachment_and_reuses_progress_message() -> None:
    source = FakeMessage()
    progress = FakeEditableMessage()
    content = "猫" * 2_000
    result = await DiscordAIResponseRenderer().reply(
        source,
        content,
        model="gpt-5.6-sol",
        display_mode=DisplayMode.PLAIN,
        existing_message=progress,
        task_summary="1. 完了",
    )
    edit = progress.edits[0]
    assert edit["embed"] is None
    assert len(edit["content"]) <= 2_000
    assert "全文は UTF-8" in edit["content"]
    assert edit["attachments"][0].filename == "yonerai-answer.md"
    assert edit["attachments"][0].fp.read().decode("utf-8") == content
    assert result.reused_message is True


@pytest.mark.asyncio
async def test_plain_site_shows_full_url_while_sources_stay_numbered_links() -> None:
    message = FakeMessage()
    published = PublishedSite(
        site_id="site1",
        release_id="release1",
        slug="clock",
        site_url="https://publish.example.test/clock/",
        revision=3,
        visibility="unlisted",
        updated=True,
    )
    await DiscordAIResponseRenderer().reply(
        message,
        "出典は [1](https://example.com/docs) です。",
        model="gpt-5.6-sol",
        display_mode=DisplayMode.PLAIN,
        published_site=published,
    )
    content = message.replies[0]["content"]
    assert "出典は [1](https://example.com/docs) です。" in content
    assert "公開サイト: https://publish.example.test/clock/" in content
    assert "公開サイト: [" not in content
    assert "v3" in content
