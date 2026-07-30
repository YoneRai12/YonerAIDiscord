from __future__ import annotations

import pytest

from yonerai_discord.modules.media_inspection import MediaInspectionInputError, canonicalize_youtube_url


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "https://youtube.com/watch?v=ABCDEFGHIJK&utm_source=test&t=12",
            "https://www.youtube.com/watch?v=ABCDEFGHIJK",
        ),
        (
            "https://www.youtube.com/shorts/ABCDEFGHIJK?feature=share",
            "https://www.youtube.com/shorts/ABCDEFGHIJK",
        ),
        (
            "https://m.youtube.com/shorts/ABCDEFGHIJK/",
            "https://www.youtube.com/shorts/ABCDEFGHIJK",
        ),
        (
            "https://youtu.be/ABCDEFGHIJK?si=tracking",
            "https://youtu.be/ABCDEFGHIJK",
        ),
    ),
)
def test_canonicalize_youtube_url_discards_tracking_query(source: str, expected: str) -> None:
    assert canonicalize_youtube_url(source) == expected


@pytest.mark.parametrize(
    "source",
    (
        "http://youtube.com/watch?v=ABCDEFGHIJK",
        "https://user@youtube.com/watch?v=ABCDEFGHIJK",
        "https://youtube.com:443/watch?v=ABCDEFGHIJK",
        "https://youtube.com/watch?v=ABCDEFGHIJK#fragment",
        "https://evil.example/watch?v=ABCDEFGHIJK",
        "https://youtube.com/embed/ABCDEFGHIJK",
        "https://youtube.com/playlist?list=ABCDEFGHIJK",
        "https://youtube.com/watch",
        "https://youtube.com/watch?v=ABCDEFGHIJK&v=ZYXWVUTSRQP",
        "https://youtube.com/shorts/ABCDEFGHIJK/extra",
        "https://youtu.be/too-short",
        " https://youtu.be/ABCDEFGHIJK",
        "https://youtu.be/ABCDEFGHIJK\n",
    ),
)
def test_canonicalize_youtube_url_rejects_unsupported_or_ambiguous_targets(source: str) -> None:
    with pytest.raises(MediaInspectionInputError, match="video URL"):
        canonicalize_youtube_url(source)


def test_typed_domain_repr_does_not_include_input_or_output() -> None:
    from yonerai_discord.modules.media_inspection import MediaInspectionRequest, MediaInspectionResult

    request = MediaInspectionRequest("https://youtu.be/ABCDEFGHIJK", "private prompt")
    result = MediaInspectionResult("private output")

    assert "ABCDEFGHIJK" not in repr(request)
    assert "private prompt" not in repr(request)
    assert "private output" not in repr(result)
