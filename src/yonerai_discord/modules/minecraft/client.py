"""Minecraft Java Edition の read-only Server List Ping クライアント。

RCON やゲーム内コマンドは一切扱わない。接続先は設定ファイルで固定し、
Discord の入力から任意ホストを受け取らないことで内部ネットワーク探索を防ぐ。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import ipaddress
import json
import re
import socket
import struct
from time import monotonic
from typing import Any


_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PRIVATE_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_PRIVATE_V6 = ipaddress.ip_network("fc00::/7")


class MinecraftConfigurationError(ValueError):
    """接続前に検出できる安全でない設定。"""


class MinecraftProtocolError(RuntimeError):
    """上限違反または不正な Server List Ping 応答。"""


class MinecraftUnavailableError(RuntimeError):
    """利用者へ内部ネットワーク情報を漏らさず表現する接続失敗。"""


@dataclass(frozen=True, slots=True)
class MinecraftTarget:
    host: str = "127.0.0.1"
    port: int = 25_565
    timeout_seconds: float = 3.0
    allow_public: bool = False
    max_packet_bytes: int = 32_768

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", normalize_host(self.host))
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65_535:
            raise MinecraftConfigurationError("port must be between 1 and 65535")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise MinecraftConfigurationError("timeout_seconds must be a number")
        if not 0.25 <= float(self.timeout_seconds) <= 10.0:
            raise MinecraftConfigurationError("timeout_seconds must be between 0.25 and 10")
        if not isinstance(self.allow_public, bool):
            raise MinecraftConfigurationError("allow_public must be a boolean")
        if isinstance(self.max_packet_bytes, bool) or not isinstance(self.max_packet_bytes, int):
            raise MinecraftConfigurationError("max_packet_bytes must be an integer")
        if not 1_024 <= self.max_packet_bytes <= 262_144:
            raise MinecraftConfigurationError("max_packet_bytes must be between 1024 and 262144")


@dataclass(frozen=True, slots=True)
class MinecraftStatus:
    version_name: str
    protocol: int | None
    players_online: int
    players_max: int
    description: str
    latency_ms: int


def normalize_host(raw_host: str) -> str:
    """URL・userinfo・port を拒否し、IP または DNS 名だけを返す。"""
    if not isinstance(raw_host, str):
        raise MinecraftConfigurationError("host must be a string")
    host = raw_host.strip()
    if not host or host != raw_host or len(host) > 253:
        raise MinecraftConfigurationError("host is empty, padded, or too long")
    if any(character in host for character in ("/", "\\", "@", "#", "?", "[", "]", "%")):
        raise MinecraftConfigurationError("host must not contain URL syntax")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or host.endswith("."):
            raise MinecraftConfigurationError("host must be a plain IP address or DNS name") from None
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise MinecraftConfigurationError("host IDNA encoding failed") from exc
        labels = ascii_host.split(".")
        if any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise MinecraftConfigurationError("host contains an invalid DNS label")
        return ascii_host.lower()
    return address.compressed


def _normalized_address(raw_address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError as exc:
        raise MinecraftConfigurationError("resolver returned a non-IP address") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def address_is_allowed(raw_address: str, *, allow_public: bool) -> bool:
    """既定では loopback、RFC1918、IPv6 ULA だけを許可する。"""
    address = _normalized_address(raw_address)
    if address.is_loopback:
        return True
    if address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        is_private_network = any(address in network for network in _PRIVATE_V4)
    else:
        is_private_network = address in _PRIVATE_V6
    if is_private_network:
        return True
    return allow_public and address.is_global


def _encode_varint(value: int) -> bytes:
    if not 0 <= value <= 0x7FFFFFFF:
        raise ValueError("VarInt value is outside the supported range")
    encoded = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            part |= 0x80
        encoded.append(part)
        if not value:
            return bytes(encoded)


def _decode_varint(payload: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    for index in range(5):
        position = offset + index
        if position >= len(payload):
            raise MinecraftProtocolError("truncated VarInt")
        current = payload[position]
        result |= (current & 0x7F) << (7 * index)
        if not current & 0x80:
            if result > 0x7FFFFFFF:
                raise MinecraftProtocolError("negative or overflowing VarInt")
            return result, position + 1
    raise MinecraftProtocolError("VarInt exceeds five bytes")


async def _read_stream_varint(reader: asyncio.StreamReader) -> int:
    encoded = bytearray()
    for _ in range(5):
        encoded.extend(await reader.readexactly(1))
        if not encoded[-1] & 0x80:
            value, _ = _decode_varint(bytes(encoded))
            return value
    raise MinecraftProtocolError("VarInt exceeds five bytes")


def _clean_text(value: str, *, limit: int) -> str:
    cleaned = "".join(character if character in "\n\t" or ord(character) >= 32 else " " for character in value)
    return cleaned.replace("@", "＠").replace("`", "ˋ")[:limit].strip()


def _flatten_component(component: Any, *, depth: int = 0) -> str:
    if depth > 12:
        return ""
    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(_flatten_component(item, depth=depth + 1) for item in component[:100])
    if not isinstance(component, dict):
        return ""
    text = component.get("text")
    parts = [text if isinstance(text, str) else ""]
    translate = component.get("translate")
    if not parts[0] and isinstance(translate, str):
        parts.append(translate)
    extra = component.get("extra")
    if isinstance(extra, list):
        parts.append(_flatten_component(extra, depth=depth + 1))
    return "".join(parts)


def _required_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MinecraftProtocolError(f"{field} must be a non-negative integer")
    return value


def _parse_status(payload: bytes, *, latency_ms: int) -> MinecraftStatus:
    packet_id, offset = _decode_varint(payload)
    if packet_id != 0:
        raise MinecraftProtocolError("unexpected status packet id")
    json_length, offset = _decode_varint(payload, offset)
    if json_length != len(payload) - offset:
        raise MinecraftProtocolError("status JSON length does not match packet length")
    try:
        document = json.loads(
            payload[offset:].decode("utf-8", errors="strict"),
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MinecraftProtocolError("status JSON is invalid") from exc
    if not isinstance(document, dict):
        raise MinecraftProtocolError("status JSON must be an object")
    players = document.get("players")
    if not isinstance(players, dict):
        raise MinecraftProtocolError("players object is missing")
    online = _required_nonnegative_int(players.get("online"), "players.online")
    maximum = _required_nonnegative_int(players.get("max"), "players.max")
    version = document.get("version")
    if version is None:
        version = {}
    if not isinstance(version, dict):
        raise MinecraftProtocolError("version must be an object")
    version_name_value = version.get("name", "不明")
    if not isinstance(version_name_value, str):
        raise MinecraftProtocolError("version.name must be a string")
    protocol_value = version.get("protocol")
    if protocol_value is not None and (isinstance(protocol_value, bool) or not isinstance(protocol_value, int)):
        raise MinecraftProtocolError("version.protocol must be an integer")
    description = _clean_text(_flatten_component(document.get("description", "")), limit=500) or "（MOTDなし）"
    return MinecraftStatus(
        version_name=_clean_text(version_name_value, limit=100) or "不明",
        protocol=protocol_value,
        players_online=online,
        players_max=maximum,
        description=description,
        latency_ms=max(0, latency_ms),
    )


class MinecraftStatusClient:
    """検査済み numeric IP にだけ接続する status ping クライアント。"""

    def __init__(self, target: MinecraftTarget) -> None:
        self.target = target

    async def _resolve(self) -> Sequence[tuple[int, str]]:
        try:
            literal = ipaddress.ip_address(self.target.host)
        except ValueError:
            loop = asyncio.get_running_loop()
            results = await loop.getaddrinfo(
                self.target.host,
                self.target.port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            addresses = {(family, sockaddr[0]) for family, _, _, _, sockaddr in results}
        else:
            family = socket.AF_INET if literal.version == 4 else socket.AF_INET6
            addresses = {(family, literal.compressed)}
        if not addresses:
            raise MinecraftConfigurationError("host did not resolve to an address")
        for family, address in addresses:
            normalized = _normalized_address(address)
            expected_family = socket.AF_INET if isinstance(normalized, ipaddress.IPv4Address) else socket.AF_INET6
            if family not in {socket.AF_INET, socket.AF_INET6} or family != expected_family:
                raise MinecraftConfigurationError("resolver returned an inconsistent address family")
        # 一つでも方針外なら全体を拒否する。複数回答を悪用する DNS rebinding 対策。
        if any(not address_is_allowed(address, allow_public=self.target.allow_public) for _, address in addresses):
            raise MinecraftConfigurationError("resolved address is outside the allowed network policy")
        return tuple(sorted(addresses, key=lambda item: (item[0], item[1])))

    async def _connect(self, family: int, address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(
            address,
            self.target.port,
            family=family,
            flags=socket.AI_NUMERICHOST,
        )

    async def query(self) -> MinecraftStatus:
        writer: asyncio.StreamWriter | None = None
        started = monotonic()
        try:
            async with asyncio.timeout(float(self.target.timeout_seconds)):
                addresses = await self._resolve()
                family, address = addresses[0]
                writer_reader = await self._connect(family, address)
                reader, writer = writer_reader
                host_bytes = self.target.host.encode("utf-8", errors="strict")
                handshake_body = b"".join(
                    (
                        b"\x00",  # handshake packet id
                        b"\x00",  # protocol version; status ping ignores it
                        _encode_varint(len(host_bytes)),
                        host_bytes,
                        struct.pack(">H", self.target.port),
                        b"\x01",  # next state: status
                    )
                )
                writer.write(_encode_varint(len(handshake_body)) + handshake_body + b"\x01\x00")
                await writer.drain()
                packet_length = await _read_stream_varint(reader)
                if not 1 <= packet_length <= self.target.max_packet_bytes:
                    raise MinecraftProtocolError("status packet exceeds the configured limit")
                response = await reader.readexactly(packet_length)
                latency_ms = round((monotonic() - started) * 1_000)
                return _parse_status(response, latency_ms=latency_ms)
        except MinecraftProtocolError:
            raise
        except MinecraftConfigurationError:
            raise
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
            raise MinecraftUnavailableError("Minecraft server is unavailable") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
                except (OSError, TimeoutError):
                    pass
