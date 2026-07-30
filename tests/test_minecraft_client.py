from __future__ import annotations

import asyncio
import json
import socket
from types import MethodType

import pytest

from yonerai_discord.modules.minecraft.client import (
    MinecraftConfigurationError,
    MinecraftProtocolError,
    MinecraftStatusClient,
    MinecraftTarget,
    address_is_allowed,
    normalize_host,
)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            part |= 0x80
        encoded.append(part)
        if not value:
            return bytes(encoded)


def _status_stream(document: object) -> asyncio.StreamReader:
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = b"\x00" + _varint(len(encoded)) + encoded
    reader = asyncio.StreamReader()
    reader.feed_data(_varint(len(payload)) + payload)
    reader.feed_eof()
    return reader


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_host_and_target_validation_rejects_url_and_unbounded_values() -> None:
    assert normalize_host("Example.COM") == "example.com"
    assert normalize_host("::1") == "::1"
    for invalid in (" https://example.com", "https://example.com", "example.com:25565", "user@example.com"):
        with pytest.raises(MinecraftConfigurationError):
            normalize_host(invalid)
    with pytest.raises(MinecraftConfigurationError):
        MinecraftTarget(port=0)
    with pytest.raises(MinecraftConfigurationError):
        MinecraftTarget(timeout_seconds=60)
    with pytest.raises(MinecraftConfigurationError):
        MinecraftTarget(allow_public="false")  # type: ignore[arg-type]
    with pytest.raises(MinecraftConfigurationError):
        MinecraftTarget(max_packet_bytes=999)


def test_private_network_policy_is_explicit() -> None:
    for allowed in ("127.0.0.1", "::1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "fd00::1"):
        assert address_is_allowed(allowed, allow_public=False)
    for denied in ("169.254.1.1", "fe80::1", "0.0.0.0", "224.0.0.1", "8.8.8.8"):
        assert not address_is_allowed(denied, allow_public=False)
    assert address_is_allowed("8.8.8.8", allow_public=True)


@pytest.mark.asyncio
async def test_resolver_rejects_mixed_private_and_public_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()

    async def mixed_answers(*args, **kwargs):
        del args, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 25_565)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 25_565)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", mixed_answers)
    client = MinecraftStatusClient(MinecraftTarget(host="minecraft.example"))
    with pytest.raises(MinecraftConfigurationError, match="outside"):
        await client._resolve()


@pytest.mark.asyncio
async def test_query_uses_numeric_approved_address_and_parses_utf8_status() -> None:
    document = {
        "version": {"name": "1.21.8", "protocol": 772},
        "players": {"online": 3, "max": 20},
        "description": {"text": "ようこそ＠everyone", "extra": [{"text": "！"}]},
    }
    reader = _status_stream(document)
    writer = _Writer()
    client = MinecraftStatusClient(MinecraftTarget(host="mc.internal", port=25_566))
    connect_calls: list[tuple[int, str]] = []

    async def resolve(_self):
        return ((socket.AF_INET, "10.0.0.9"),)

    async def connect(_self, family: int, address: str):
        connect_calls.append((family, address))
        return reader, writer

    client._resolve = MethodType(resolve, client)
    client._connect = MethodType(connect, client)

    status = await client.query()

    assert connect_calls == [(socket.AF_INET, "10.0.0.9")]
    assert status.version_name == "1.21.8"
    assert status.protocol == 772
    assert (status.players_online, status.players_max) == (3, 20)
    assert status.description == "ようこそ＠everyone！"
    assert writer.closed
    sent = b"".join(writer.writes)
    assert b"mc.internal" in sent
    assert struct_port(sent, 25_566)


def struct_port(payload: bytes, port: int) -> bool:
    return port.to_bytes(2, "big") in payload


@pytest.mark.asyncio
async def test_query_rejects_oversized_packet_before_reading_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(_varint(32_769))
    reader.feed_eof()
    writer = _Writer()
    client = MinecraftStatusClient(MinecraftTarget())

    async def resolve(_self):
        return ((socket.AF_INET, "127.0.0.1"),)

    async def connect(_self, family: int, address: str):
        del family, address
        return reader, writer

    client._resolve = MethodType(resolve, client)
    client._connect = MethodType(connect, client)
    with pytest.raises(MinecraftProtocolError, match="exceeds"):
        await client.query()
    assert writer.closed


@pytest.mark.asyncio
async def test_query_rejects_malformed_utf8_json() -> None:
    payload = b"\x00\x01\xff"
    reader = asyncio.StreamReader()
    reader.feed_data(_varint(len(payload)) + payload)
    reader.feed_eof()
    writer = _Writer()
    client = MinecraftStatusClient(MinecraftTarget())

    async def resolve(_self):
        return ((socket.AF_INET, "127.0.0.1"),)

    async def connect(_self, family: int, address: str):
        del family, address
        return reader, writer

    client._resolve = MethodType(resolve, client)
    client._connect = MethodType(connect, client)
    with pytest.raises(MinecraftProtocolError, match="JSON"):
        await client.query()
