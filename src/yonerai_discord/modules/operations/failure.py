from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    ROLE_HIERARCHY = "role_hierarchy"
    RATE_LIMITED = "rate_limited"
    ALREADY_PROCESSED = "already_processed"
    TIMEOUT = "timeout"
    EXTERNAL_UNAVAILABLE = "external_unavailable"
    UNCERTAIN = "uncertain"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class SafeFailure:
    kind: FailureKind
    user_message: str
    retryable: bool
    error_type: str


def format_failure_message(
    failure: SafeFailure,
    *,
    error_code: str,
    reference_id: str,
) -> str:
    """Production-safe Discord text shared by delivery and tokenless preview."""

    if not isinstance(failure, SafeFailure):
        raise TypeError("failure must be SafeFailure")
    if not isinstance(error_code, str) or not re.fullmatch(r"discord\.[a-z_]{1,40}", error_code):
        raise ValueError("error_code is invalid")
    if not isinstance(reference_id, str) or not re.fullmatch(r"ERR-[A-F0-9]{12}", reference_id):
        raise ValueError("reference_id is invalid")
    return f"{failure.user_message}\nエラーコード: `{error_code}`\n参照ID: `{reference_id}`"


def classify_failure(error: BaseException) -> SafeFailure:
    """例外本文を返さず、安全な表示と型名だけへ分類する。"""

    seen: set[int] = set()
    for _ in range(4):
        seen.add(id(error))
        try:
            original = getattr(error, "original", None)
        except Exception:
            break
        if not isinstance(original, BaseException) or id(original) in seen:
            break
        error = original
    name = type(error).__name__
    lowered = name.lower()
    try:
        status = getattr(error, "status", None)
    except Exception:
        status = None
    if status == 403 or "missingpermission" in lowered or "forbidden" in lowered:
        return SafeFailure(FailureKind.PERMISSION_DENIED, "この操作を行う権限がありません。", False, name)
    if status == 404 or "notfound" in lowered or "interactionresponded" in lowered:
        return SafeFailure(FailureKind.ALREADY_PROCESSED, "対象が見つからないか、すでに処理済みです。", False, name)
    if status == 429:
        return SafeFailure(FailureKind.RATE_LIMITED, "混雑しています。少し待って再試行してください。", True, name)
    if isinstance(status, int) and 500 <= status <= 599:
        return SafeFailure(FailureKind.EXTERNAL_UNAVAILABLE, "外部サービスへ接続できません。", True, name)
    if isinstance(error, (ValueError, TypeError)):
        return SafeFailure(FailureKind.INVALID_INPUT, "入力内容を確認してください。", False, name)
    if isinstance(error, PermissionError):
        return SafeFailure(FailureKind.PERMISSION_DENIED, "この操作を行う権限がありません。", False, name)
    if isinstance(error, TimeoutError):
        return SafeFailure(FailureKind.TIMEOUT, "処理が時間内に完了しませんでした。", True, name)
    if "ratelimit" in lowered or "rate_limit" in lowered:
        return SafeFailure(FailureKind.RATE_LIMITED, "混雑しています。少し待って再試行してください。", True, name)
    if "hierarchy" in lowered:
        return SafeFailure(FailureKind.ROLE_HIERARCHY, "ロールの上下関係により実行できません。", False, name)
    if isinstance(error, (ConnectionError, OSError)):
        return SafeFailure(FailureKind.EXTERNAL_UNAVAILABLE, "外部サービスへ接続できません。", True, name)
    return SafeFailure(FailureKind.INTERNAL, "処理に失敗しました。管理者は監査ログを確認してください。", False, name)
