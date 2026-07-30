from __future__ import annotations

from collections.abc import AsyncIterable


class YonerAIResponseLimitError(RuntimeError):
    """上流応答が宣言値または実測値で安全上限を超えた。"""


async def read_bounded_response(
    chunks: AsyncIterable[bytes],
    *,
    max_response_bytes: int,
    declared_length: int | None = None,
) -> bytes:
    """将来の公式API adapter向け、streaming応答の共通サイズ制限。

    URLや本文を例外へ含めないため、ログへそのまま渡しても秘密値やqueryを
    漏らさない。HTTP schemaやendpointは公式contract確定後に別adapterで定義する。
    """
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
        raise TypeError("max_response_bytes must be an integer")
    if max_response_bytes < 1:
        raise ValueError("max_response_bytes must be positive")
    if declared_length is not None:
        if isinstance(declared_length, bool) or not isinstance(declared_length, int) or declared_length < 0:
            raise ValueError("declared_length is invalid")
        if declared_length > max_response_bytes:
            raise YonerAIResponseLimitError("YonerAI readiness response exceeds the configured limit")

    payload = bytearray()
    async for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("response chunks must be bytes")
        if len(payload) + len(chunk) > max_response_bytes:
            raise YonerAIResponseLimitError("YonerAI readiness response exceeds the configured limit")
        payload.extend(chunk)
    return bytes(payload)
