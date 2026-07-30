from __future__ import annotations

import json

import pytest

from yonerai_discord.modules.jp_information import (
    CAO_HOLIDAY_CSV_URL,
    JMA_AREA_URL,
    JMA_FORECAST_URL_TEMPLATE,
    CabinetOfficeHolidayClient,
    InvalidPayloadError,
    JmaClient,
    RedirectRejectedError,
    ResponseTooLargeError,
    UnknownRegionError,
)


class FakeContent:
    def __init__(self, chunks) -> None:
        self.chunks = list(chunks)
        self.sizes = []

    async def iter_chunked(self, size):
        self.sizes.append(size)
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, body: bytes, *, status=200, content_length="auto", history=()) -> None:
        self.status = status
        self.history = history
        self.content = FakeContent([body])
        self.content_length = len(body) if content_length == "auto" else content_length
        self.released = False

    def release(self):
        self.released = True


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def encoded(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()


@pytest.mark.asyncio
async def test_jma_uses_fixed_endpoint_and_area_allowlist() -> None:
    session = FakeSession(
        [
            FakeResponse(encoded({"offices": {"130000": {"name": "東京都"}}})),
            FakeResponse(encoded([{"publishingOffice": "気象庁"}])),
        ]
    )
    client = JmaClient(session=session)
    await client.fetch_area_catalog()
    await client.fetch_forecast("130000")

    assert [call[0] for call in session.calls] == [
        JMA_AREA_URL,
        JMA_FORECAST_URL_TEMPLATE.format(code="130000"),
    ]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert session.calls[0][1]["timeout"].connect == 3.0
    assert session.calls[0][1]["timeout"].total == 10.0

    with pytest.raises(UnknownRegionError):
        await client.fetch_forecast("https://attacker.invalid/")
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_redirect_is_rejected_without_reading_body() -> None:
    response = FakeResponse(b"{}", status=302)
    client = JmaClient(session=FakeSession([response]))
    with pytest.raises(RedirectRejectedError):
        await client.fetch_area_catalog()
    assert response.content.sizes == []
    assert response.released is True


@pytest.mark.asyncio
async def test_declared_and_streamed_oversize_are_rejected() -> None:
    declared = FakeResponse(b"{}", content_length=9)
    streamed = FakeResponse(b"123456789", content_length=None)
    session = FakeSession([declared, streamed])
    client = JmaClient(session=session, max_response_bytes=8)
    with pytest.raises(ResponseTooLargeError):
        await client.fetch_area_catalog()
    with pytest.raises(ResponseTooLargeError):
        await client.fetch_area_catalog()
    assert declared.content.sizes == []
    assert declared.released is streamed.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"\xff", b'{"offices":{},"number":NaN}', b'{"number":1e999}'])
async def test_invalid_utf8_and_non_finite_json_are_rejected(body: bytes) -> None:
    client = JmaClient(session=FakeSession([FakeResponse(body)]))
    with pytest.raises(InvalidPayloadError):
        await client.fetch_area_catalog()


@pytest.mark.asyncio
async def test_holiday_csv_accepts_only_safe_known_charset() -> None:
    csv_body = "国民の祝日・休日月日,国民の祝日・休日名称\r\n2026/1/1,元日\r\n".encode("cp932")
    session = FakeSession([FakeResponse(csv_body)])
    client = CabinetOfficeHolidayClient(session=session)
    assert "元日" in await client.fetch_holiday_csv()
    assert session.calls[0][0] == CAO_HOLIDAY_CSV_URL

    invalid = CabinetOfficeHolidayClient(session=FakeSession([FakeResponse(b"\xff\xfeX\x00")]))
    with pytest.raises(InvalidPayloadError):
        await invalid.fetch_holiday_csv()
