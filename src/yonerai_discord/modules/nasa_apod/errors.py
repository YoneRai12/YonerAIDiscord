from __future__ import annotations


class NasaApodError(RuntimeError):
    """利用者へ内部情報を返さないNASA APOD moduleの基底例外。"""


class ApodConfigurationError(NasaApodError):
    """明示opt-inまたはAPI keyが安全な起動条件を満たさない。"""


class ApodTransportError(NasaApodError):
    """固定NASA endpointとの通信に失敗した。"""


class ApodResponseError(NasaApodError):
    """NASA応答が公開契約を満たさない。"""


class ApodResponseTooLargeError(ApodResponseError):
    """NASA応答が256KiB上限を超えた。"""


class ApodNotFoundError(NasaApodError):
    """指定日のAPODが存在しない。"""


class ApodRateLimitedError(NasaApodError):
    """NASA側の利用上限へ到達した。"""


class ApodDateError(ValueError):
    """利用者指定日が公開範囲外またはISO日付でない。"""


__all__ = [
    "ApodConfigurationError",
    "ApodDateError",
    "ApodNotFoundError",
    "ApodRateLimitedError",
    "ApodResponseError",
    "ApodResponseTooLargeError",
    "ApodTransportError",
    "NasaApodError",
]
