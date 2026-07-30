from __future__ import annotations

import discord
from discord import app_commands

from .service import BoundaryStatus, YonerAIStatusService


_STATE_LABELS = {
    "disabled": "無効（既定値）",
    "local_only": "ローカル境界のみ",
    "remote_opt_in_incomplete": "外部接続の二重opt-in未完了",
    "contract_pending": "公式API contract待ち（外部接続なし）",
    "ready_to_probe": "readiness確認可能",
    "healthy": "正常",
    "degraded": "一部低下",
    "unavailable": "確認不可",
}


def render_status(result: BoundaryStatus) -> str:
    """token、URL、query、上流本文を受け取らない安全な表示。"""
    return "\n".join(
        (
            f"YonerAI境界: **{_STATE_LABELS[result.state.value]}**",
            f"機能: {'ON' if result.enabled else 'OFF'}",
            f"外部status参照: {'許可済み' if result.remote_permitted else '禁止'}",
            f"認証情報: {'設定済み' if result.token_configured else '未設定'}（値は表示しません）",
            "操作: readiness/status参照のみ（書き込み・コード実行なし）",
        )
    )


class YonerAIGroup(app_commands.Group):
    def __init__(self, service: YonerAIStatusService) -> None:
        super().__init__(name="yonerai", description="YonerAI将来連携の安全なreadiness境界")
        self.service = service

    @app_commands.command(name="status", description="YonerAI連携境界の設定状態を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            render_status(self.service.status()),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="health", description="許可済みreadiness境界の状態を確認します")
    async def health(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.service.health()
        await interaction.followup.send(
            render_status(result),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
