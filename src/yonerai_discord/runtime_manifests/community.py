from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    # Community: command内のobject ownership検査に加え、中央RBACでも入口を制御する。
    _cap("cap-run-ticket-open", "interaction.discord-surface", "非公開ticketを作成", command="ticket open"),
    _cap("cap-run-ticket-close", "interaction.discord-surface", "ticketを閉じる", command="ticket close"),
    _cap("cap-run-ticket-add", "interaction.discord-surface", "ticket参加者を追加", command="ticket add"),
    _cap("cap-run-ticket-remove", "interaction.discord-surface", "ticket参加者を削除", command="ticket remove"),
    _cap(
        "cap-run-ticket-transcript-info",
        "interaction.discord-surface",
        "ticket transcriptのprivacy方針を表示",
        command="ticket transcript-info",
    ),
    _cap("cap-run-poll-create", "interaction.discord-surface", "選択式投票を作成", command="poll create"),
    _cap("cap-run-poll-close", "interaction.discord-surface", "投票を終了", command="poll close"),
    _cap("cap-run-poll-results", "interaction.discord-surface", "投票結果を表示", command="poll results"),
    _cap(
        "cap-run-poll-vote",
        "interaction.discord-surface",
        "persistent buttonから投票",
        event="component.poll-vote",
        plugin="community",
    ),
    _cap("cap-run-suggest-create", "interaction.discord-surface", "提案を登録", command="suggest create"),
    _cap("cap-run-suggest-status", "interaction.discord-surface", "提案状態を表示・更新", command="suggest status"),
    _cap(
        "cap-run-selfrole-panel",
        "interaction.discord-surface",
        "self-role panelを設置",
        command="selfrole panel",
        level=RbacLevel.MODERATOR,
        risk=RiskLevel.HIGH,
    ),
    _cap(
        "cap-run-selfrole-add",
        "interaction.discord-surface",
        "self-role候補を追加",
        command="selfrole add",
        level=RbacLevel.MODERATOR,
        risk=RiskLevel.HIGH,
    ),
    _cap(
        "cap-run-selfrole-remove",
        "interaction.discord-surface",
        "self-role候補を削除",
        command="selfrole remove",
        level=RbacLevel.MODERATOR,
        risk=RiskLevel.HIGH,
    ),
    _cap(
        "cap-run-selfrole-toggle",
        "interaction.discord-surface",
        "persistent buttonから安全なroleを付け外し",
        event="component.selfrole-toggle",
        plugin="community",
        risk=RiskLevel.MEDIUM,
    ),
)
