from __future__ import annotations

import pytest

from yonerai_discord.discord_payload_budget import (
    DiscordEmbedFieldText,
    DiscordEmbedText,
    DiscordPayloadBudgetError,
    fit_embed_description,
    maximum_embed_description_characters,
    validate_discord_payload_budget,
)


def test_exact_discord_payload_budget_is_accepted() -> None:
    usage = validate_discord_payload_budget(
        content="c" * 2_000,
        embeds=(
            DiscordEmbedText(
                title="t" * 256,
                description="d" * 4_096,
                footer="f" * 1_648,
            ),
        ),
        file_count=10,
    )

    assert usage.content_characters == 2_000
    assert usage.embed_characters == 6_000
    assert usage.embed_count == 1
    assert usage.file_count == 10


@pytest.mark.parametrize(
    ("embeds", "content", "file_count"),
    (
        ((), "x" * 2_001, 0),
        ((DiscordEmbedText(title="x" * 257),), "", 0),
        ((DiscordEmbedText(description="x" * 4_097),), "", 0),
        ((DiscordEmbedText(fields=(DiscordEmbedFieldText("n", "v"),) * 26),), "", 0),
        ((DiscordEmbedText(footer="x" * 2_049),), "", 0),
        ((DiscordEmbedText(author="x" * 257),), "", 0),
        ((DiscordEmbedText(description="x" * 3_001), DiscordEmbedText(description="x" * 3_000)), "", 0),
        ((), "", 11),
    ),
)
def test_discord_payload_budget_rejects_each_hard_limit(
    embeds: tuple[DiscordEmbedText, ...],
    content: str,
    file_count: int,
) -> None:
    with pytest.raises(DiscordPayloadBudgetError):
        validate_discord_payload_budget(
            content=content,
            embeds=embeds,
            file_count=file_count,
        )


def test_description_budget_accounts_for_title_fields_footer_and_author() -> None:
    fields = (
        DiscordEmbedFieldText(name="項", value="値" * 1_000),
        DiscordEmbedFieldText(name="別", value="値" * 1_000),
    )
    maximum = maximum_embed_description_characters(
        title="題" * 200,
        fields=fields,
        footer="脚" * 500,
        author="著" * 100,
    )
    fitted, truncated = fit_embed_description(
        "本" * 4_096,
        title="題" * 200,
        fields=fields,
        footer="脚" * 500,
        author="著" * 100,
    )

    assert maximum == 3_198
    assert len(fitted) == 3_198
    assert truncated is True


def test_description_budget_truncates_only_description_to_total_limit() -> None:
    fields = (
        DiscordEmbedFieldText(name="n" * 256, value="v" * 1_024),
        DiscordEmbedFieldText(name="m" * 256, value="w" * 1_024),
    )
    fitted, truncated = fit_embed_description(
        "d" * 4_096,
        title="t" * 256,
        fields=fields,
        footer="f" * 1_000,
        author="a" * 256,
    )

    assert len(fitted) == 1_928
    assert truncated is True
    validate_discord_payload_budget(
        embeds=(
            DiscordEmbedText(
                title="t" * 256,
                description=fitted,
                fields=fields,
                footer="f" * 1_000,
                author="a" * 256,
            ),
        )
    )
