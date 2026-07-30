from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from yonerai_discord.modules.nasa_apod.domain import ApodItem, parse_apod_payload
from yonerai_discord.modules.nasa_apod.errors import (
    ApodConfigurationError,
    ApodDateError,
    ApodNotFoundError,
    ApodResponseError,
    ApodResponseTooLargeError,
    ApodRateLimitedError,
    ApodTransportError,
)
from yonerai_discord.modules.nasa_apod.service import NasaApodService
from yonerai_discord.modules.nasa_apod.source import (
    MAX_APOD_RESPONSE_BYTES,
    NASA_APOD_ENDPOINT,
    NasaApiApodSource,
    StaticApodSource,
)


TODAY = datetime(2026, 7, 24, tzinfo=UTC)


def payload(**changes: object) -> dict[str, object]:
    return {
        "date": "2026-07-23",
        "title": "A safe title",
        "explanation": "A safe explanation.",
        "media_type": "image",
        "url": "https://apod.nasa.gov/image/example.jpg",
        "hdurl": "https://apod.nasa.gov/image/example-hd.jpg",
        "copyright": "Example author",
        **changes,
    }


class Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, _size: int):
        yield self.body


class Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self.history = ()
        self.headers = {"Content-Type": content_type}
        self.content_length = len(body) if content_length is None else content_length
        self.content = Content(body)
        self.released = False

    def release(self) -> None:
        self.released = True


class Session:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_api_source_uses_only_fixed_get_contract_without_leaking_key() -> None:
    key = "private-nasa-key-value"
    response = Response(json.dumps(payload()).encode())
    session = Session(response)
    source = NasaApiApodSource(key, session=session)

    item = await source.fetch(date(2026, 7, 23))

    assert item.day == date(2026, 7, 23)
    assert session.calls[0][0] == NASA_APOD_ENDPOINT
    kwargs = session.calls[0][1]
    assert kwargs["allow_redirects"] is False
    assert kwargs["params"] == {"api_key": key, "date": "2026-07-23"}
    assert set(kwargs["params"]) == {"api_key", "date"}
    assert key not in repr(source)
    assert response.released is True
    with pytest.raises(ApodConfigurationError):
        NasaApiApodSource("DEMO_KEY", session=session)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_day", [date(1995, 6, 15), date(2999, 1, 1)])
async def test_api_source_rejects_out_of_range_date_before_network(invalid_day: date) -> None:
    session = Session(Response(json.dumps(payload()).encode()))
    source = NasaApiApodSource("valid-key", session=session)

    with pytest.raises(ApodDateError):
        await source.fetch(invalid_day)

    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(b"{}", content_type="text/html"), ApodResponseError),
        (
            Response(b"{}", content_length=MAX_APOD_RESPONSE_BYTES + 1),
            ApodResponseTooLargeError,
        ),
        (Response(b"\xff", content_type="application/json"), ApodResponseError),
        (Response(b"[]", content_type="application/json"), ApodResponseError),
        (Response(b"{}", status=404), ApodNotFoundError),
        (Response(b"{}", status=429), ApodRateLimitedError),
        (Response(b"{}", status=302), ApodTransportError),
    ],
)
async def test_api_source_rejects_representative_unsafe_responses(
    response: Response,
    error: type[Exception],
) -> None:
    source = NasaApiApodSource("valid-key", session=Session(response))
    with pytest.raises(error):
        await source.fetch()


@pytest.mark.asyncio
async def test_transport_and_stream_failures_hide_key_and_do_not_retry() -> None:
    key = "private-nasa-key-value"

    class FailingSession:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, *_args: object, **_kwargs: object) -> Response:
            self.calls += 1
            raise RuntimeError(key)

    session = FailingSession()
    with pytest.raises(ApodTransportError) as captured:
        await NasaApiApodSource(key, session=session).fetch()
    assert session.calls == 1
    assert key not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    response = Response(b"{}")

    class FailingContent:
        async def iter_chunked(self, _size: int):
            raise OSError(key)
            yield b""  # pragma: no cover

    response.content = FailingContent()
    with pytest.raises(ApodTransportError) as stream_error:
        await NasaApiApodSource(key, session=Session(response)).fetch()
    assert key not in str(stream_error.value)
    assert stream_error.value.__cause__ is None
    assert stream_error.value.__context__ is None

    invalid_json = Response(f'{{"api_key":"{key}",'.encode())
    with pytest.raises(ApodResponseError) as json_error:
        await NasaApiApodSource(key, session=Session(invalid_json)).fetch()
    assert key not in str(json_error.value)
    assert json_error.value.__context__ is None


@pytest.mark.asyncio
async def test_response_release_failure_is_suppressed_without_leaking_key() -> None:
    key = "private-nasa-key-value"

    class ReleaseFailingResponse(Response):
        def release(self) -> None:
            raise RuntimeError(key)

    source = NasaApiApodSource(
        key,
        session=Session(ReleaseFailingResponse(json.dumps(payload()).encode())),
    )

    item = await source.fetch()

    assert item.title == "A safe title"


def test_domain_accepts_image_and_video_but_omits_unsafe_urls() -> None:
    image = parse_apod_payload(payload(unknown={"ignored": True}))
    assert image.media_type == "image"
    assert image.url == "https://apod.nasa.gov/image/example.jpg"
    assert image.copyright == "Example author"
    assert image.source_page_url.endswith("/ap260723.html")

    video = parse_apod_payload(
        payload(
            media_type="video",
            url="http://example.invalid/watch",
            hdurl="https://user:password@example.invalid/private",
            thumbnail_url="https://example.invalid/thumb.jpg",
        )
    )
    assert video.media_type == "video"
    assert video.url is None
    assert video.hdurl is None
    assert video.thumbnail_url == "https://example.invalid/thumb.jpg"
    with pytest.raises(ApodResponseError):
        parse_apod_payload(payload(date="2026-W30-4"))
    for unsafe in (
        "https://localhost/private",
        "https://127.0.0.1/private",
        "https://[::1]/private",
        "https://example.com/a b",
        "https://example.com/\\",
    ):
        assert parse_apod_payload(payload(url=unsafe)).url is None


@pytest.mark.asyncio
async def test_static_source_and_service_share_date_contract() -> None:
    item = ApodItem(
        day=date(2026, 7, 23),
        title="Offline item",
        explanation="Validated fixture.",
        media_type="image",
        url="https://example.invalid/image.jpg",
    )
    service = NasaApodService(StaticApodSource((item,)), clock=lambda: TODAY)

    assert await service.get() == item
    assert await service.get("2026-07-23") == item
    with pytest.raises(ApodDateError):
        await service.get("1995-06-15")
    with pytest.raises(ApodDateError):
        await service.get("2026-07-25")
    with pytest.raises(ApodDateError):
        await service.get("2026-W30-4")
    with pytest.raises(ApodNotFoundError):
        await service.get("2026-07-22")


@pytest.mark.asyncio
async def test_static_source_revalidates_direct_items_and_sanitizes_urls() -> None:
    unsafe = ApodItem(
        day=date(2026, 7, 23),
        title="Offline item",
        explanation="Validated fixture.",
        media_type="image",
        url="http://127.0.0.1/private",
    )
    item = await StaticApodSource((unsafe,)).fetch()
    assert item is not unsafe
    assert item.url is None

    invalid = ApodItem(
        day=date(1995, 6, 15),
        title="Too old",
        explanation="Invalid fixture.",
        media_type="image",
        url=None,
    )
    with pytest.raises(ValueError):
        StaticApodSource((invalid,))
