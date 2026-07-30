from __future__ import annotations

from .domain import ModAction, PermissionDecision, PermissionSnapshot


REQUIRED_PERMISSION: dict[ModAction, str] = {
    ModAction.WARN: "moderate_members",
    ModAction.WARNINGS: "moderate_members",
    ModAction.TIMEOUT: "moderate_members",
    ModAction.UNTIMEOUT: "moderate_members",
    ModAction.KICK: "kick_members",
    ModAction.BAN: "ban_members",
    ModAction.UNBAN: "ban_members",
    ModAction.PURGE: "manage_messages",
    ModAction.PURGE_USER: "manage_messages",
    ModAction.PURGE_LINKS: "manage_messages",
    ModAction.CASE: "moderate_members",
}
TARGET_ACTIONS = {
    ModAction.WARN,
    ModAction.TIMEOUT,
    ModAction.UNTIMEOUT,
    ModAction.KICK,
    ModAction.BAN,
    ModAction.PURGE_USER,
}


def decide_permission(action: ModAction, snapshot: PermissionSnapshot) -> PermissionDecision:
    permission = REQUIRED_PERMISSION[action]
    actor_is_owner = snapshot.actor_id == snapshot.guild_owner_id
    if not actor_is_owner and permission not in snapshot.actor_permissions:
        return PermissionDecision(False, f"権限 {permission} が必要です")
    if permission not in snapshot.bot_permissions:
        return PermissionDecision(False, f"Botに権限 {permission} がありません")
    if action not in TARGET_ACTIONS or snapshot.target_id is None:
        return PermissionDecision(True)
    if snapshot.target_id == snapshot.actor_id:
        return PermissionDecision(False, "自分自身を対象にはできません")
    if snapshot.target_id == snapshot.bot_user_id:
        return PermissionDecision(False, "Bot自身を対象にはできません")
    if snapshot.target_id == snapshot.guild_owner_id:
        return PermissionDecision(False, "サーバー所有者を対象にはできません")
    if snapshot.target_top_role is None:
        return PermissionDecision(False, "対象メンバーのロールを確認できません")
    if snapshot.bot_top_role <= snapshot.target_top_role:
        return PermissionDecision(False, "Botのロールが対象メンバーより上にありません")
    if not actor_is_owner and snapshot.actor_top_role <= snapshot.target_top_role:
        return PermissionDecision(False, "あなたのロールが対象メンバーより上にありません")
    return PermissionDecision(True)


def validate_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        raise ValueError("理由は必須です")
    if len(normalized) > 500:
        raise ValueError("理由は500文字以内にしてください")
    return normalized


def validate_purge(amount: int, reason: str, *, dry_run: bool, confirm: str) -> str:
    if not 1 <= amount <= 100:
        raise ValueError("削除件数は1〜100件で指定してください")
    normalized = validate_reason(reason)
    if not dry_run and confirm != "PURGE":
        raise ValueError("実行には confirm に PURGE と入力してください")
    return normalized


def validate_timeout_minutes(minutes: int) -> int:
    if not 1 <= minutes <= 28 * 24 * 60:
        raise ValueError("タイムアウトは1分〜28日で指定してください")
    return minutes
