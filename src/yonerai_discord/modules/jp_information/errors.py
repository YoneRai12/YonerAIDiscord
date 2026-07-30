from __future__ import annotations


class JpInformationError(Exception):
    """利用者へ上流の詳細を露出せずに扱える基底例外。"""


class TransportError(JpInformationError):
    """公式 provider との通信に失敗した。"""


class RedirectRejectedError(TransportError):
    """固定 endpoint が redirect を返した。"""


class ResponseTooLargeError(TransportError):
    """応答が設定済みの上限を超えた。"""


class InvalidPayloadError(JpInformationError, ValueError):
    """応答が期待する最小 schema を満たさない。"""


class UnknownRegionError(JpInformationError, LookupError):
    """入力が公式地域 allowlist に存在しない、または一意でない。"""


class PublishedRangeError(JpInformationError, LookupError):
    """問い合わせが内閣府 CSV の公式掲載範囲外である。"""


class ProviderBusyError(JpInformationError):
    """bounded rate/concurrency limit 内で provider 呼び出しを開始できない。"""


class CircuitOpenError(JpInformationError):
    """連続失敗後の provider circuit breaker が開いている。"""
