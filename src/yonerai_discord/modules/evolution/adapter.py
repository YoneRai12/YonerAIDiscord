from __future__ import annotations

import discord
from discord import app_commands

from .service import EvolutionArtifactError, EvolutionDisabledError, EvolutionService, EvolutionServiceError

NO_MENTIONS = discord.AllowedMentions.none()


class EvolutionGroup(app_commands.Group):
    def __init__(self, service: EvolutionService) -> None:
        super().__init__(name="evolution", description="所有者審査付き自己進化proposal")
        self.service = service

    @app_commands.command(name="status", description="自己進化review基盤の状態を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        proposals = self.service.list(limit=10)
        counts = {
            status: sum(item.status == status for item in proposals)
            for status in ("proposed", "in_review", "approved", "rejected")
        }
        ai_ready = bool(self.service.ai_service and self.service.ai_service.available)
        await _reply(
            interaction,
            "\n".join(
                (
                    f"自己進化review: {'有効' if self.service.enabled else '無効'}",
                    f"quality provider: {'利用可能' if ai_ready else '未設定'}",
                    f"直近10件: proposed={counts['proposed']} / review={counts['in_review']} / approved={counts['approved']} / rejected={counts['rejected']}",
                    "自動適用・merge・push・restart: 実装なし",
                )
            ),
        )

    @app_commands.command(name="propose", description="改善proposal artifactを作成します")
    @app_commands.describe(
        title="提案名",
        rationale="理由（秘密値を含めない）",
        target_paths="workspace相対pathをカンマ区切り",
        generate_with_ai="設定済みquality modelでproposal planを生成",
        allow_remote="互換用（永続同意の代替にはなりません）",
    )
    async def propose(
        self,
        interaction: discord.Interaction,
        title: str,
        rationale: str,
        target_paths: str,
        generate_with_ai: bool = True,
        allow_remote: bool = False,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            created = await self.service.propose(
                actor_id=interaction.user.id,
                guild_id=interaction.guild_id,
                title=title,
                rationale=rationale,
                target_paths=tuple(path.strip() for path in target_paths.split(",") if path.strip()),
                generate_with_ai=generate_with_ai,
                allow_remote=allow_remote,
            )
        except (EvolutionServiceError, ValueError, KeyError):
            await interaction.followup.send(
                "proposalを作成できませんでした。設定・入力・AI同意を確認してください。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.followup.send(
            f"`{created.record.proposal_id}` を作成しました。状態: `{created.record.status}`\n"
            "artifactはローカル保存済みで、自動適用されません。",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @app_commands.command(name="review", description="proposalのreviewを開始します")
    async def review(self, interaction: discord.Interaction, proposal_id: str, note: str) -> None:
        await self._transition(interaction, proposal_id, note, "review")

    @app_commands.command(name="approve", description="proposalを承認済みにします（適用はしません）")
    async def approve(self, interaction: discord.Interaction, proposal_id: str, note: str) -> None:
        await self._transition(interaction, proposal_id, note, "approve")

    @app_commands.command(name="reject", description="proposalを却下します")
    async def reject(self, interaction: discord.Interaction, proposal_id: str, note: str) -> None:
        await self._transition(interaction, proposal_id, note, "reject")

    async def _transition(
        self,
        interaction: discord.Interaction,
        proposal_id: str,
        note: str,
        action: str,
    ) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _reply(interaction, "サーバー内でのみ利用できます。")
            return
        try:
            if action == "review":
                record = self.service.begin_review(
                    proposal_id,
                    actor_id=interaction.user.id,
                    guild_id=guild_id,
                    note=note,
                )
            elif action == "approve":
                record = self.service.approve(
                    proposal_id,
                    actor_id=interaction.user.id,
                    guild_id=guild_id,
                    note=note,
                )
            else:
                record = self.service.reject(
                    proposal_id,
                    actor_id=interaction.user.id,
                    guild_id=guild_id,
                    note=note,
                )
        except (EvolutionDisabledError, EvolutionArtifactError, ValueError, KeyError):
            await _reply(interaction, "状態遷移を拒否しました。proposal ID・整合性・現在状態を確認してください。")
            return
        await _reply(interaction, f"`{record.proposal_id}` → `{record.status}`。自動適用は行いません。")

    @app_commands.command(name="show", description="artifact整合性とproposal内容を表示します")
    async def show(self, interaction: discord.Interaction, proposal_id: str) -> None:
        try:
            proposal = self.service.show(proposal_id)
        except (EvolutionArtifactError, ValueError, KeyError):
            await _reply(interaction, "proposal artifactを取得できませんでした。")
            return
        integrity = "OK" if proposal.integrity_ok else "NG"
        await _reply(
            interaction,
            f"`{proposal.record.proposal_id}` / `{proposal.record.status}` / integrity={integrity}\n"
            + proposal.content[:1_650],
        )


async def _reply(interaction: discord.Interaction, message: str) -> None:
    kwargs = {"ephemeral": True, "allowed_mentions": NO_MENTIONS}
    if interaction.response.is_done():
        await interaction.followup.send(message[:1_950], **kwargs)
    else:
        await interaction.response.send_message(message[:1_950], **kwargs)
