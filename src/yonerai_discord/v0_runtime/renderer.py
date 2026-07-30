"""public CommandResult だけを表示する純粋 renderer。"""

from __future__ import annotations

from .command_service import CommandResult


def render_command_result(result: CommandResult) -> str:
    if not result.ok:
        return {
            "owner_only": "この操作は本人のみ実行できます。",
            "actor_not_authorized": "現在の権限または機能設定では操作できません。",
            "authorization_changed": "実行直前に権限または機能設定が変わったため、変更しませんでした。",
            "invalid_input": "入力が正しくありません。",
            "memory_unavailable": "個人メモリは現在利用できません。",
            "memory_rejected": "個人メモリ操作は現在許可されていません。",
            "memory_not_found": "このscopeに該当する自分のメモリはありません。",
        }.get(result.code, "操作を完了できませんでした。")
    data = result.data
    if result.code == "model_list":
        return f"利用可能なモデル: {', '.join(data['models']) or 'なし'}"
    if result.code == "model_set":
        return f"モデル設定を {data['selected']} に保存しました。"
    if result.code == "model_auto":
        return "モデル設定を自動選択に戻しました。"
    if result.code == "provider_list":
        return f"利用可能な provider: {', '.join(data['providers']) or 'なし'}"
    if result.code == "provider_set":
        return f"provider 設定を {data['selected']} に保存しました。"
    if result.code == "route":
        preferred = f"model={data['preferred_model'] or 'auto'}, provider={data['preferred_provider'] or 'auto'}"
        effective = f"model={data['effective_model'] or '未設定'}, provider={data['effective_provider'] or '未設定'}"
        state = "実行可能" if data["executable"] else "実行不可"
        return f"希望: {preferred}\n有効経路: {effective}\n状態: {state}（{data['reason']}）"
    if result.code == "reset":
        return "短期会話履歴のみをリセットしました。model/provider設定と個人メモリは保持しています。"
    if result.code == "memory_remembered":
        return f"個人メモリを保存しました（ID: {data['memory_id']}）。"
    if result.code == "memory_list":
        items = data["items"]
        if not items:
            return "このscopeに保存中の個人メモリはありません。"
        return "\n".join(
            f"`{item['memory_id']}` visibility={item['visibility']} created_at={item['created_at']}" for item in items
        )
    if result.code == "memory_forgotten":
        return "指定した個人メモリを削除しました。"
    if result.code == "memory_cleared":
        return f"このscopeの個人メモリを{data['deleted']}件削除しました。"
    if result.code == "memory_privacy":
        return f"現在の保存・参照範囲は {data['visibility']} です。"
    if result.code == "memory_preview":
        return (
            f"AIへ渡る候補: {data['count']}件 / visibility={data['visibility']}。"
            "本文・内部prompt・scoreは表示しません。"
        )
    return "操作を完了しました。"
