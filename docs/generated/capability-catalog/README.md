# Capability catalog

> Generated file — do not edit. `scripts/generate_capability_catalog.py` から再生成してください。

この一覧はcode-owned正本から生成した静的な候補metadataです。
実行権限、module/plugin readiness、guild override、実Discordや外部依存でのlive成功を示しません。

- schema: `yonerai.discord.capability-catalog.v1`
- scope: `static_candidate_metadata`
- catalog revision: `fd014a185ad7b46fbd6e8fd0cdd0958a3142c81f2a708958f2ce6e38cc0d3192`
- source revision: `5e9756fed3d240649ed89cf5a0d7e94a205c5fd746e44434bcd38c64e6b8f38d`

## Counts

| 母集団 | 件数 |
| --- | ---: |
| 歴史canonical | 656 |
| runtime宣言 | 170 |
| Registry合計 | 826 |
| 静的projection | 180 |
| projection canonical由来 | 17 |
| projection runtime由来 | 163 |
| surface接続unique ID | 170 |
| command capability ID | 157 |
| command path | 175 |
| event capability ID | 14 |
| event path | 14 |
| planner action capability ID | 9 |
| planner action path | 9 |
| model-tool binding | 1 |
| command/event/model-tool未接続runtime | 16 |
| 既知binding未接続runtime | 7 |

## Runtime binding gaps

`direct_surface_unbound_ids` はcommand/event/model-toolへ直接接続していないruntime宣言です。
planner action接続を含む全known bindingの残余は `unbound_ids` です。

### Direct surface unbound IDs

- `cap-run-admin-ui-read`
- `cap-run-ai-attachment-understand`
- `cap-run-audio-ducking-core`
- `cap-run-browser-remote-interactive`
- `cap-run-browser-remote-screenshot`
- `cap-run-capability-forge-owner-notification`
- `cap-run-image-edit`
- `cap-run-media-compose-grid`
- `cap-run-media-discord-asset-inspect`
- `cap-run-media-place-on-canvas`
- `cap-run-media-qr-encode`
- `cap-run-media-quote-card`
- `cap-run-media-url-inspection`
- `cap-run-memory-context-recall`
- `cap-run-speech-synthesize`
- `cap-run-speech-transcribe`

### All-known-binding unbound IDs

- `cap-run-admin-ui-read`
- `cap-run-ai-attachment-understand`
- `cap-run-audio-ducking-core`
- `cap-run-capability-forge-owner-notification`
- `cap-run-memory-context-recall`
- `cap-run-speech-synthesize`
- `cap-run-speech-transcribe`

## Entries

| capability_id | module_id | name | primary_intent | intent_tags | risk | minimum_rbac | source_provenance | bindings | surface_bindings | content_revision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cap-can-0001 | collaboration.meeting | /meeting cancel で中止 | unknown | unknown | medium | everyone | canonical_registry |  | command:schedule.cancel | 5041cf7dd732e73568bd11c6802e06e0641e2fa1416f8f2801d4c1e378728d2c |
| cap-can-0002 | collaboration.meeting | /meeting create full meeting workflow | unknown | unknown | medium | everyone | canonical_registry |  | command:schedule.create | bb1012e3e769f21fe1d91e809f310caba074e3f625bd0ecd955881230a510159 |
| cap-can-0007 | collaboration.meeting | /meeting list で会議一覧 | knowledge | knowledge | low | everyone | canonical_registry |  | command:schedule.list | fb7f96f95c1b98ca7beaeba1ca5fd700356fc825894e6cecb5020ab4bd1071a3 |
| cap-can-0030 | collaboration.meeting | DM自然文で出欠を変更 | unknown | unknown | medium | everyone | canonical_registry |  | command:schedule.rsvp | f6e27747f3d5d4e0a3918d27546b899f02a12cc454d9c4fb10869d023b34de59 |
| cap-can-0153 | integration.api-web | web_search_api safe Web検索 | conversation | conversation, knowledge | medium | everyone | canonical_registry |  | command:ai.search, command:web.fetch, command:web.find, command:web.search | acfd72af374c77f1a246bdf10f2b5fe50101085c52b190b7d915d0c8cb60c52e |
| cap-can-0161 | intelligence.ai-runtime | /ai ask でlocal優先AIへ質問 | conversation | code, conversation, knowledge, media, site | medium | everyone | canonical_registry |  | command:ai.ask | 31c49549c4cc62b8a09f71e2645b65641f5bf5fd00edfe0e098d587ca1868656 |
| cap-can-0162 | intelligence.ai-runtime | /ai status でAI provider・remote可否確認 | conversation | conversation, knowledge | low | everyone | canonical_registry |  | command:ai.status | 83144b93e96b7e478cfcb0a565e1cf19e37e4739781e96bc95a0007ce7b41873 |
| cap-can-0265 | interaction.discord-surface | /ping でGateway遅延を確認 | knowledge | knowledge | low | everyone | canonical_registry |  | command:system.ping | 77a93fbe8e90ab40ac07b7bd6ba459939b03e28ad445e9c8456eb135902d3873 |
| cap-can-0272 | interaction.discord-surface | /schedule show で予定表示 | knowledge | knowledge | low | everyone | canonical_registry |  | command:schedule.show | 24c571368969bb7ccf67124470d879c36b28e2d08c7851ab2d2824eb3dfc55fe |
| cap-can-0278 | interaction.discord-surface | /system doctor で権限・設定診断 | knowledge | knowledge | low | everyone | canonical_registry |  | command:system.doctor | 7f4ee91cf57c9b84851669a9157ccf0ca8e484347b7871a63f13975483c27bed |
| cap-can-0415 | media.voice | /voice status でVOICEVOX状態確認 | media | media | low | everyone | canonical_registry |  | command:voice.status | 4dd019fa593a4b4ee034536355d67ed887df3542c1c50ed1f1bb2524066ca1eb |
| cap-can-0416 | media.voice | /voice synthesize でWAVを添付生成 | media | media | medium | everyone | canonical_registry |  | command:voice.synthesize | b278cb6dcc865460c24bb21ef3cbd84387e2d0200342f77c9e100ae3dc4b375a |
| cap-can-0519 | operations.observability | /system health で稼働状態確認 | knowledge | knowledge | low | everyone | canonical_registry |  | command:system.health | 5bfe3e834850de8ea041246ecacd9690cc760a55d1d0dbd2f01d5e7eeb7c58fd |
| cap-can-0538 | operations.scheduling-notification | /schedule remind でDM/channel通知予約 | unknown | unknown | medium | everyone | canonical_registry |  | command:schedule.remind | 93a09f8a77aa2761b03578a5e1a472166a69939a9385055ae4e0f65c805dec93 |
| cap-can-0588 | platform.runtime | /system plugins でplugin状態一覧 | knowledge | knowledge | low | everyone | canonical_registry |  | command:system.plugins | 3ffbc35562b4d0523d98eb4071e5d65154e9c1d659b9fcd11d63eec100da588a |
| cap-can-0589 | platform.runtime | capabilityを中央registryへ登録する | knowledge | knowledge | low | everyone | canonical_registry |  | command:system.capabilities, command:system.modules | 2362ffde29e467ebd27b18f6bcac36bceef796b8ce144b71306301338fe2ae5a |
| cap-can-0613 | security.access-control | capabilityごとにclient・権限・riskをpolicy判定する | moderation | moderation | low | everyone | canonical_registry |  | command:system.capability-set, command:system.module-set, command:system.permission-set | 9cb3c7d9873fe42c7e264a974a7999a09f6e8650c65e6c8f85c849b689ba3ffb |
| cap-run-ai-mention-chat | intelligence.ai-runtime | BOTへの明示メンションをTerra/Solへ安全に接続して返信 | conversation | code, conversation, knowledge, media, site | medium | everyone | runtime_manifest |  | event:ai_mention_message | adcbd146a49ecae9fc86e5936b23b38890b6ea6e4d7d8f2af6569804401c410e |
| cap-run-ai-model-auto | intelligence.ai-runtime | 本人の論理モデル設定を自動選択へ戻す | conversation | conversation, knowledge | medium | everyone | runtime_manifest |  | command:ai.model.auto | d976e2420bcc40e03facc0b6bd4cdc09588dfb2a0c4652cf8ce4701d293e9dcc |
| cap-run-ai-model-list | intelligence.ai-runtime | 本人が利用可能な論理モデル候補を非公開表示 | conversation | conversation, knowledge | low | everyone | runtime_manifest |  | command:ai.model.list | 34adbc1646b6346a02e76e8f0def52de70ced3aa09855a031d58ff974f78eaf5 |
| cap-run-ai-model-set | intelligence.ai-runtime | 本人の論理モデル設定を変更 | conversation | conversation, knowledge | medium | everyone | runtime_manifest |  | command:ai.model.set | 785a11043f35fca7612b7bb0ea0342e0b6b0e41d98599c38f30c3a15b1a45e26 |
| cap-run-ai-provider-list | intelligence.ai-runtime | 本人が利用可能なprovider候補を非公開表示 | conversation | conversation, knowledge | low | everyone | runtime_manifest |  | command:ai.provider.list | 06b3cf7c4352fbff0327c960eb93657a9fe1769112cf6a5d0eb530e9e4db3168 |
| cap-run-ai-provider-set | intelligence.ai-runtime | 本人のprovider設定を変更 | conversation | conversation, knowledge | medium | everyone | runtime_manifest |  | command:ai.provider.set | 4a1913fb4ed199bbf40ce559ab1ac6317a1839be56606c98c540e9d679c452e6 |
| cap-run-ai-reset | intelligence.ai-runtime | 本人の現在会話の短期履歴だけをリセット | conversation | conversation, knowledge | medium | everyone | runtime_manifest |  | command:ai.reset | 03cef6cf67ab614f45b5d8d97f2ad905c4ac49345f0cccff45aa30b913a2b619 |
| cap-run-ai-route | intelligence.ai-runtime | 本人の希望経路と実効経路を理由付きで非公開表示 | conversation | conversation, knowledge | low | everyone | runtime_manifest |  | command:ai.route | 2929c0f99fcca35a54c8ec18103cdcb9056f5e334ad2f9c45fc51bbc3c10a041 |
| cap-run-automod-channel | moderation.automod | AutoModのredacted report channelを設定 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:automod.channel | ef95656b11c40e52310ddc1f1db8574964b1ac8b7230646d01a31105aab355fc |
| cap-run-automod-message-create | moderation.automod | message createを本文非保存でreport-only検知 | moderation | moderation | high | guild_admin | runtime_manifest |  | event:automod_message_create | e3c52b85f3cbf996faedc359713c88aa63bae26e9402935b941140c78d0c173b |
| cap-run-automod-message-edit | moderation.automod | message editを本文非保存でreport-only再検査 | moderation | moderation | high | guild_admin | runtime_manifest |  | event:automod_message_edit | 2d010cf2836e83389cecd74b2ca663d6f9cff98977b607964e6e2e729c5f24e3 |
| cap-run-automod-policy | moderation.automod | 確認文字列付きでreport-only policyをON/OFF | moderation | moderation | high | guild_admin | runtime_manifest |  | command:automod.policy | d7c7374b6db30b4ae36ad362b80857b9d4cf058c02ae7dbaaae1f6db0a7efd7b |
| cap-run-automod-status | moderation.automod | report-only AutoModの状態を表示 | moderation | moderation | medium | guild_admin | runtime_manifest |  | command:automod.status | 39f2b0785d9901bb9077de3a920f84b9f28e23fc5a2cf48772bace0c8269898e |
| cap-run-browser-remote-interactive | web.browser-rendering | 明示opt-inしたCloudflare Browser Runの固定YouTube操作経路 | unknown | unknown | high | bot_owner | runtime_manifest |  | action:browser.interact | 765dbaa85dda0dcb4a36a5e171765711609cdebddff161eae478b9e56a0a9021 |
| cap-run-browser-remote-screenshot | web.browser-rendering | 明示opt-inしたCloudflare Browser Rendering remote screenshot経路 | unknown | unknown | high | bot_owner | runtime_manifest |  | action:browser.screenshot | b18b969da1a027a432da31ea26e5a1cd9c116e7ad243e798ca822550ceab550b |
| cap-run-discovery-help | interaction.discord-surface | 利用できるコマンドを検索・一覧表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:help | acd11ab2eed19560e8e8c1cc84e9125ce836ca6a491c7614fe78cc6313116de6 |
| cap-run-earthquake-delivery | operations.earthquake | 購読済みチャンネルへ重複排除した地震・EEWを自動通知 | knowledge | knowledge, web_research | high | guild_admin | runtime_manifest |  | event:earthquake_feed_delivery | eb7edfa4b5813c95e8263a9dc7bcae64b54ad18ee25830476d5e268cc33c74ae |
| cap-run-earthquake-latest | operations.earthquake | P2PQuake公式APIから最新の地震・EEW情報を表示 | knowledge | knowledge, web_research | medium | everyone | runtime_manifest |  | command:earthquake.latest | 33af878f7d9e86584eafb05d79a86f027f0c25aade293bbf76c4cb2314d41ded |
| cap-run-earthquake-status | operations.earthquake | このサーバーの地震通知設定とfeed状態を表示 | knowledge | knowledge, web_research | low | everyone | runtime_manifest |  | command:earthquake.status | 7bd0be91f67f2ed5f552709de044b718d4ff8fcffffde5019d05365b2b55007e |
| cap-run-earthquake-subscribe | operations.earthquake | 管理者がチャンネル別の地震・EEW通知を有効化 | knowledge | knowledge, web_research | high | guild_admin | runtime_manifest |  | command:earthquake.subscribe | 13631fecb3d1946996be6a4d55a28b9e21b475a884e129832b16358bb1d23cc8 |
| cap-run-earthquake-unsubscribe | operations.earthquake | 管理者がこのサーバーの地震・EEW通知を停止 | knowledge | knowledge, web_research | high | guild_admin | runtime_manifest |  | command:earthquake.unsubscribe | 483514c532d1bd359faf6660b1d214384648196be8eb2b31e36b0b13991364c2 |
| cap-run-evolution-approve | intelligence.ai-runtime | 自己進化proposalを承認済みにする | self_evolution | code, self_evolution | high | bot_owner | runtime_manifest |  | command:evolution.approve | f590533f6702bbfcfb94cbf52a1ba16dc8e47fa3951bb23a3d1d1c7947801ffa |
| cap-run-evolution-propose | intelligence.ai-runtime | Solで改善proposal artifactを作成 | self_evolution | code, self_evolution | high | bot_owner | runtime_manifest |  | command:evolution.propose | 1200815b2a525059172a247635349f540f23e115323ab43ce6c8a29aa0d0f779 |
| cap-run-evolution-reject | intelligence.ai-runtime | 自己進化proposalを却下 | self_evolution | code, self_evolution | high | bot_owner | runtime_manifest |  | command:evolution.reject | 1d90ca95952b8a482178d51c5bb4f370321e7aff20f33a0e60ef1d7fe1149d63 |
| cap-run-evolution-review | intelligence.ai-runtime | 自己進化proposalのreviewを開始 | self_evolution | code, self_evolution | high | bot_owner | runtime_manifest |  | command:evolution.review | ef3ab37f2c8bc3a9d832412963727b3ed08b850c5c9a67f02d808d513b2f4a72 |
| cap-run-evolution-show | intelligence.ai-runtime | 自己進化proposal artifactを検証して表示 | self_evolution | code, self_evolution | high | bot_owner | runtime_manifest |  | command:evolution.show | 37afa60201dc55d5648c1bf02bb607de3bba7bd6beee65d08cedc0bbdb8fd838 |
| cap-run-evolution-status | intelligence.ai-runtime | 自己進化review基盤の状態を表示 | self_evolution | code, self_evolution | low | bot_owner | runtime_manifest |  | command:evolution.status | 99f37f779b4dbf85d0933488cb11714a5854764728ece264297612b8586f4731 |
| cap-run-holiday-next | operations.public-information | 内閣府の公式掲載範囲から次の祝日・休日を表示 | knowledge | knowledge, web_research | low | everyone | runtime_manifest |  | command:holiday.next | 99f904a6b63e2b12aed9e36c6ce85a11011e72e46b08b8c7a67b2f1c2abb136e |
| cap-run-holiday-year | operations.public-information | 内閣府の公式掲載範囲から指定年の祝日・休日を表示 | knowledge | knowledge, web_research | low | everyone | runtime_manifest |  | command:holiday.year | 68e327cf53077b811d2b62e64d08b9fb6d935ece21f72ee481436ce4b25f3187 |
| cap-run-image-edit | media.image-editing | 検証済みPNGを別IDのcanonical PNGへ編集 | unknown | unknown | high | trusted | runtime_manifest |  | action:image.edit | aa45cf553970e21a09b0b2838dd5c75fdb1b27089236e0d87303b37aeea30acc |
| cap-run-image-generate | media.image-generation | 明示promptから検証済みPNG画像を生成 | unknown | unknown | high | trusted | runtime_manifest |  | command:image.generate | 865fe2a35d2e70d19a0683fdfe0dfc3c4c345cf4fe3214c621c8b9d4164e8b20 |
| cap-run-info-avatar | utility.general | avatar URLを表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:info.avatar | 1c743e6879487ea072a824780b4d44b79b0c3d8648a8498d658f3b8270273d2f |
| cap-run-info-channel | utility.general | channel情報を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:info.channel | 9c3796f55d5b4172a2d306cba0d0280d7a6322c27f487e531b162bf7861d8a18 |
| cap-run-info-permissions | utility.general | member権限を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:info.permissions | bcd68d1aabdbe761141dce6cb445bda26699fa153d29509767f49f1e480d5897 |
| cap-run-info-role | utility.general | role情報を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:info.role | 60c6b6c7f2a02bd29284c331a7f6caa42432f99a8fd10cf0c6ef7fa9570fd914 |
| cap-run-info-server | utility.general | server情報を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:info.server | ccd47772dd20e5f89ec66727723b99b62bbee31195682a4409c62c0c1d83021f |
| cap-run-info-user | utility.general | user情報を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:info.user | b8ea2b7a1623aa6cefb08f95f3b7d298decc43567e44be7a69753f8a697a31e2 |
| cap-run-jobs-cancel | operations.execution | 未実行durable jobを監査付きでcancel | unknown | unknown | high | guild_admin | runtime_manifest |  | command:jobs.cancel | 9ef49ff69117853bb8f36b28cdaba6306c22654302af328ba180a427cc18fd9c |
| cap-run-jobs-execute | operations.execution | allowlist済みexecutorだけをdurable workerで実行 | unknown | unknown | high | guild_admin | runtime_manifest |  | event:worker.jobs-execute | 8483bf54eb7df879885962f369e5760c55d683c69275d12064844ec9d7dde986 |
| cap-run-jobs-list | operations.execution | payloadを隠してdurable job一覧を表示 | unknown | unknown | medium | guild_admin | runtime_manifest |  | command:jobs.list | 503af6e790299d22c29477474f674c8f6656bc7b34e7b3ab5b181ad66d34aea8 |
| cap-run-jobs-retry | operations.execution | terminal jobを監査付きで再queue | unknown | unknown | high | guild_admin | runtime_manifest |  | command:jobs.retry | 178f9bcfb6fbf72def17a4618ee3bc87d870d106375e7d20adeb580edee50c40 |
| cap-run-jobs-status | operations.execution | durable job workerとqueue状態を表示 | unknown | unknown | medium | guild_admin | runtime_manifest |  | command:jobs.status | 72c999263c16d159a89d76fc2f6ca9ed4fa3c4573a9b8cc6893d491e03f6afa2 |
| cap-run-media-compose-grid | media.pipeline | 同一scopeの1〜8画像artifactをローカルgridへ合成 | unknown | unknown | high | trusted | runtime_manifest |  | action:media.compose-grid | 2df9ddaff4cb330d537b0187d9a351acf7302553cf197498199506df26a7e2f2 |
| cap-run-media-discord-asset-inspect | media.pipeline | 明示されたDiscord絵文字またはスタンプを読み取り専用で検査 | unknown | unknown | medium | trusted | runtime_manifest |  | action:media.discord-asset-inspect | 4f7a38a5acd2011b8df0a4cfcd6748de4010c23c86e003ceaf9e6fdbc5bfbdfe |
| cap-run-media-place-on-canvas | media.pipeline | 同一scopeの画像artifactをローカルcanvasへ配置 | unknown | unknown | high | trusted | runtime_manifest |  | action:media.place-on-canvas | b1a1b6bf299936d6e73acdcb12d732771cb61e67d1a0300fab43a4ca40d12ba0 |
| cap-run-media-qr-encode | media.pipeline | 制限付きテキストからローカルQR画像artifactを生成 | unknown | unknown | high | trusted | runtime_manifest |  | action:media.qr-encode | c96b5f9aa910eee2653044c2b0bfc1a13df4f5d7b1451bef4e68d67cbb4b8f14 |
| cap-run-media-quote-card | media.pipeline | 本文と実行者情報からローカル引用カードartifactを生成 | unknown | unknown | medium | trusted | runtime_manifest |  | action:media.quote-card | 1d93c76d540620fad2eb7d64a3723a37c5d50a1d6808431e6ba9c7573d31c9d8 |
| cap-run-media-url-inspection | media.url-inspection | 明示された公開YouTube URLを隔離Hyper-V VMまたは明示remote providerで解析 | unknown | unknown | high | bot_owner | runtime_manifest |  | action:media.url-inspect | 344c9b4f5a010c00ba41fee857be7fd89f383614917d7ded588656573266aaaa |
| cap-run-memory-clear | intelligence.personal-memory | 本人の個人AIメモリを確認付きで全削除 | memory | memory | critical | everyone | runtime_manifest |  | command:memory.clear | 34220c5958f3926b827deb12eac116082a38f6eb02e8b26dae18e21651e00e0a |
| cap-run-memory-disable | intelligence.personal-memory | 本人の個人AIメモリ記録と利用を停止 | memory | memory | low | everyone | runtime_manifest |  | command:memory.disable | 1ddbd9d77ad7cae1d757c4577624c4b3bbd320c8468f2f1b19defa53da79ff03 |
| cap-run-memory-enable | intelligence.personal-memory | 本人の明示同意で個人AIメモリを有効化 | memory | memory | medium | everyone | runtime_manifest |  | command:memory.enable | 3c384e686d352e8dffac21a336c8e8dea1b33909e5ecf9f78bae162cecf3dd05 |
| cap-run-memory-export | intelligence.personal-memory | 本人の個人AIメモリをJSONエクスポート | memory | memory | medium | everyone | runtime_manifest |  | command:memory.export | 112af3d0299b1cae249bad7fe556dbe65b66d842896ff26b0f665493300546ae |
| cap-run-memory-forget | intelligence.personal-memory | 本人の個人AIメモリをID指定で削除 | memory | memory | medium | everyone | runtime_manifest |  | command:memory.forget | 601c5eb7e58b322dedba659a09ddf3bfae8bba3b1e2fc54e39001120d8279c3d |
| cap-run-memory-list | intelligence.personal-memory | 本人の個人AIメモリだけを非公開表示 | memory | memory | medium | everyone | runtime_manifest |  | command:memory.list | 4082ee421caa1d3aceae17f7d17f958d1f97ca34ba1f4dc5d16c3d27abb2a8a4 |
| cap-run-memory-preview | intelligence.personal-memory | AIへ渡る本人の記憶文脈を非公開表示 | memory | memory | medium | everyone | runtime_manifest |  | command:memory.preview | 6ce02aba72179f447a6cd46d3c36021da63600901ee0265af6730b8f4f31e9ad |
| cap-run-memory-privacy | intelligence.personal-memory | 個人AIメモリの保存・外部送信・削除境界を表示 | memory | memory | low | everyone | runtime_manifest |  | command:memory.privacy | 3eecc376f19cedf69ecd7964c1664cca256c528b32cf2d3679b99f0df8a2dc55 |
| cap-run-memory-remember | intelligence.personal-memory | 本人用の長期メモを明示保存 | memory | memory | medium | everyone | runtime_manifest |  | command:memory.remember | e3ca54c60f322e3ea4173c52e3ac0df2b256958022a41ab302b1dea710b25b17 |
| cap-run-memory-search | intelligence.personal-memory | 本人の個人AIメモリを関連度で検索 | memory | memory | medium | everyone | runtime_manifest |  | command:memory.search | 1871a49a192100a6152ebddd9ad175157878bb95f8fb861e81390edbf275d8d0 |
| cap-run-memory-status | intelligence.personal-memory | 本人の個人AIメモリ状態を表示 | memory | memory | low | everyone | runtime_manifest |  | command:memory.status | 85e6ad8143569dd9d9fd61d3af53f648a3d999b494b38a885fead31ebe672999 |
| cap-run-message-link-expand | intelligence.ai-runtime | 同一guildのDiscordメッセージリンクを安全に展開 | unknown | unknown | medium | everyone | runtime_manifest |  | event:message_link_expand | f65193a58d8a221c330f73f9c87f0ee55ed125da3f816bfd67ae4c1c107f38d8 |
| cap-run-minecraft-status | gaming.minecraft | 設定済みMinecraft Java serverのstatusをread-onlyで取得 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:minecraft.status | e8bb1b5edc62f24f01e8f3b542fada607405f104f1e3ee34b150dc8368914737 |
| cap-run-mod-ban | moderation.actions | memberをban | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:mod.ban | 8a31dd14f6da05dc552ceeb072c25dc3c6069b62354d19f6d4031f2b241913a9 |
| cap-run-mod-case | moderation.actions | moderation caseを表示 | moderation | moderation | medium | moderator | runtime_manifest |  | command:mod.case | cc8b0d0f7dfc891c9692a37e6ad37190e90d9254efe2e2a736b1c9f0022a897b |
| cap-run-mod-kick | moderation.actions | memberをkick | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:mod.kick | 4d88f11662392d50aa1d61e434b1c3b6ff507b15f694b8e2c7fe443e76a3d1db |
| cap-run-mod-purge | moderation.actions | messageを確認付きで一括削除 | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:mod.purge | dc1b4d9e9e78fc2b42ebfb74aa6c31b6790f157f85d565bc304867b47e9ddaee |
| cap-run-mod-purge-links | moderation.actions | URL投稿を確認付きで削除 | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:mod.purge-links | 33a8ca945de4f0f52dfde037b705360c7b201da5c2ab24d62965a5e958928141 |
| cap-run-mod-purge-user | moderation.actions | 特定userのmessageを確認付きで削除 | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:mod.purge-user | ef52911072436e5f49a138b1f74f6d04cb09787dbd91c375329f39b35933d8c7 |
| cap-run-mod-timeout | moderation.actions | memberをtimeout | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:mod.timeout | dc363e485b055c2370e55c40d7caa221418acf5f1e1eb1de9680616bd0afa3a3 |
| cap-run-mod-unban | moderation.actions | banを解除 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:mod.unban | afc0035d62ab1721deb235b61bb61844b02168157fdf696d9303f3b854d250e6 |
| cap-run-mod-untimeout | moderation.actions | memberのtimeoutを解除 | moderation | moderation | high | moderator | runtime_manifest |  | command:mod.untimeout | a9bea381b1c0a8d02b5c7859ef368dd855e436f997be0dd499347674c9ff4fe8 |
| cap-run-mod-warn | moderation.actions | 警告をcase ledgerへ記録 | moderation | moderation | high | moderator | runtime_manifest |  | command:mod.warn | 88e82920eb33b0f7ca1d62cd94feaeba63d686c8d84c3de22571d4610817158e |
| cap-run-mod-warnings | moderation.actions | 警告履歴を表示 | moderation | moderation | medium | moderator | runtime_manifest |  | command:mod.warnings | 9a7ff1619977308854dc04e227652327896ac947a947b25e6aedf42787208573 |
| cap-run-music-clear-mine | media.music | 本人が依頼した待機queue曲だけを一括削除 | music | media, music | medium | everyone | runtime_manifest |  | command:music.clear-mine | 50bd74416591ff1b6a82e3354a1c826326c7b4dc6a95301fffa303d91afc8cc4 |
| cap-run-music-generate | media.music-generation | 権利確認済みpromptから検証済みPCM16 WAVを生成 | unknown | unknown | high | trusted | runtime_manifest |  | command:musicgen.generate | 32414dbe7e2f5f60080d0461ef8643c293ab197ca5cace1974c20e7bc47f2363 |
| cap-run-music-import | media.music | 権利確認済みPCM WAV添付をcontent-addressed private libraryへ取り込む | music | media, music | medium | everyone | runtime_manifest |  | command:music.import | 9ea6ed6cb7811eea8ad5fa54e6d0b77656e0ecbd0561e198301bee91ed614d8e |
| cap-run-music-join | media.music | 利用者と同じVCへ音楽playerを接続 | music | media, music | medium | everyone | runtime_manifest |  | command:music.join | 82473f65a2a87415b369b39f00b84645d24cd0e9c34d5137bc5108b430454c0b |
| cap-run-music-leave | media.music | 権限を持つ利用者が音楽sessionを終了してVCから退出 | music | media, music | medium | everyone | runtime_manifest |  | command:music.leave | 6c86c7b23be6e0e282e2fc1042d5e09cb2f0924ee4403d8acb914ec50f0d8d36 |
| cap-run-music-loop | media.music | 現在曲の依頼者または管理者がtrack・queue loopを設定 | music | media, music | medium | everyone | runtime_manifest |  | command:music.loop | 7edab4f4ee793098fa912983b4c42e6573f3fe8e5acc79007ba08136849c1228 |
| cap-run-music-move | media.music | 本人のqueue曲または管理者が待機queue内の位置を変更 | music | media, music | medium | everyone | runtime_manifest |  | command:music.move | 50064e1877f268b71d3da648125c06e97bde1362043308bcb524d7c9d49bd933 |
| cap-run-music-now | media.music | 現在の再生曲を表示 | music | media, music | low | everyone | runtime_manifest |  | command:music.now | c52a74b1ee011f995720335461c2f7bbc512823e9c14f7dadfb243e19876eb2f |
| cap-run-music-pause | media.music | 音楽busだけを一時停止しTTS busは継続 | music | media, music | medium | everyone | runtime_manifest |  | command:music.pause | 9da707ca51d56d5f7d60cac9169a8dedd8145c027b91a96d4f6e9394194cf8b7 |
| cap-run-music-play | media.music | 許可済みローカルlibraryから曲名でqueueへ追加・再生 | music | media, music | medium | everyone | runtime_manifest |  | command:music.play | 4b2350d971bd43d30afd7f3fc4e5af2bd8828edb6b9906e6075fd3edc4b21edf |
| cap-run-music-playlist-delete | media.music | 本人所有のplaylistを削除 | music | media, music | medium | everyone | runtime_manifest |  | command:music.playlist.delete | fc17b495a48fa22f8777ab5d6a47396e4497c385a2d6a2a7910f7a05cf472329 |
| cap-run-music-playlist-list | media.music | 本人所有のplaylistだけを表示 | music | media, music | low | everyone | runtime_manifest |  | command:music.playlist.list | f14096a3a8978e65b112c9439febefadb1c30060a9666a5c5295c183327129e4 |
| cap-run-music-playlist-load | media.music | 本人のplaylistを許可済みlibraryで再解決してqueueへ追加 | music | media, music | medium | everyone | runtime_manifest |  | command:music.playlist.load | 14d6ea6e4ac23839bacf96ce7ba4c042ea84f6c7f48e8391783e88a1647b9e0a |
| cap-run-music-playlist-save | media.music | 本人の現在曲とqueueを個人playlistへ保存 | music | media, music | medium | everyone | runtime_manifest |  | command:music.playlist.save | 420f9c8d83d030f171c3e7820009320248fd0d8bb1bf3ab6099f7d2056570dc6 |
| cap-run-music-queue | media.music | 現在曲と待機queueを表示 | music | media, music | low | everyone | runtime_manifest |  | command:music.queue | a295520e35d04cbc7f3082a1c14aa181f8033731ae088621599d99abcd33e6e0 |
| cap-run-music-radio | media.music | 許可済みローカルlibraryから手動queue優先で1曲ずつ自動補充 | music | media, music | medium | everyone | runtime_manifest |  | command:music.radio | 02799fd0eace5479f5ca5f1a0927fd44627ba175e9a6b421401d4fae54b0134d |
| cap-run-music-read-aloud-message | media.music | 明示routeの短文をloopback VOICEVOXで合成し既存music sessionへducking付きで重畳 | music | media, music | medium | everyone | runtime_manifest |  | command:music.read-aloud.dictionary-delete, command:music.read-aloud.dictionary-set, command:music.read-aloud.disable, command:music.read-aloud.enable, command:music.read-aloud.exclude-add, command:music.read-aloud.exclude-delete, command:music.read-aloud.focus-cancel, command:music.read-aloud.focus-start, command:music.read-aloud.list, command:music.read-aloud.my-preset, command:music.read-aloud.policy, command:music.read-aloud.preset, command:music.read-aloud.server-preset, event:music_read_aloud_message | 90e29664cbb6446389e5207ef8fcc4fd905308b7275eb53ff59cdb1e8e8666fb |
| cap-run-music-remove | media.music | 本人のqueue曲または管理者が指定曲を削除 | music | media, music | medium | everyone | runtime_manifest |  | command:music.remove | fa225d33d5b06b4a016f557fea2c735a53eb4ab114a7341f1aaae6a619e5c423 |
| cap-run-music-resume | media.music | 一時停止中の音楽busを再開 | music | media, music | medium | everyone | runtime_manifest |  | command:music.resume | 99cb309b0f6687025f4fbffb9cd60f0dcc5722d549b4f2885e3cc514514570bd |
| cap-run-music-search | media.music | 許可済みローカルlibraryを曲名で検索 | music | media, music | low | everyone | runtime_manifest |  | command:music.search | b1ba920199f7e8507d76a7fac2c00d287ddaed412a12c75f4a53e48c9957f930 |
| cap-run-music-search-youtube | media.music | YouTube公式検索ページURLを生成（音声抽出・再配信なし） | music | media, music | low | everyone | runtime_manifest |  | command:music.search-youtube | 2294d024802376391de07db4fb3dd89641bed7b42e8d5c8f30e1222723185cdf |
| cap-run-music-seek | media.music | 現在曲の依頼者または管理者がlocal音源の再生位置を変更 | music | media, music | medium | everyone | runtime_manifest |  | command:music.seek | 5b83c7b75b886a8e850f087b5f80d6ad99b9f84eb28e91d93925ac74f6c3513b |
| cap-run-music-shuffle | media.music | 現在曲の依頼者または管理者が待機queueをshuffle | music | media, music | medium | everyone | runtime_manifest |  | command:music.shuffle | 753970e356f5d345709e273611f9bb7ff5041c0130c4d050a7831cbcdaaa10d8 |
| cap-run-music-skip | media.music | 現在曲の依頼者または管理者が曲をskip | music | media, music | medium | everyone | runtime_manifest |  | command:music.skip | 6f89cc4b7b17c95bc82407a93d46654109fdc2757953a1752969792cfde68709 |
| cap-run-music-speak | media.music | VOICEVOXをリアルタイム合成し音楽ducking付きでVCへ重畳 | music | media, music | medium | everyone | runtime_manifest |  | command:music.speak | 0f9bb237a30fa6b74deabcabd52b0d287fe04774ec3b7df5d768d2e151baf16e |
| cap-run-music-status | media.music | 音楽・TTS・ローカルlibraryの利用状態を表示 | music | media, music | low | everyone | runtime_manifest |  | command:music.status | 2e744cabe5139c6a5a695a200ec1c57f5fbdd3796ecff0e2ac59323b65f5ea4c |
| cap-run-music-stop | media.music | 音楽とqueueだけを停止しTTSは継続 | music | media, music | medium | everyone | runtime_manifest |  | command:music.stop | e25debdd11f0ac340b929bf102dacf28f7c186876105e9a062f8e5a1bd37de82 |
| cap-run-music-volume | media.music | 現在曲の依頼者または管理者が音楽またはTTS bus音量を設定 | music | media, music | medium | everyone | runtime_manifest |  | command:music.volume | 9ea42331af6192df3e3bbe0b30c93280e26460ade883456449c55bdc6b77d67d |
| cap-run-nasa-apod-read | operations.nasa-apod | NASA Astronomy Picture of the Dayを明示取得して表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:nasa.apod | b6be5624cee4ff474c41d2806e087bfc5c336f2724148907e0ea7016c72a84f5 |
| cap-run-poll-close | interaction.discord-surface | 投票を終了 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:poll.close | 06e371b423498275a50dab6b525d765bc4d1fc586fb7ea3ce2843098351e11e0 |
| cap-run-poll-create | interaction.discord-surface | 選択式投票を作成 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:poll.create | 2f101a4436daa7407b32ab5411612148180efac198bda7d5c692da59e6defecc |
| cap-run-poll-results | interaction.discord-surface | 投票結果を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:poll.results | e2d97d8b156b9a606fe641494715c3d359034c30be9e76c27ec636233c3accd8 |
| cap-run-poll-vote | interaction.discord-surface | persistent buttonから投票 | knowledge | knowledge | low | everyone | runtime_manifest |  | event:component.poll-vote | 2f1b26b625d3c2a9b7ed0e0260a2a90988c0586cf923753108105cfc8e54505b |
| cap-run-schedule-resolve | operations.scheduling-notification | uncertain reminderを所有者が監査後に解決 | unknown | unknown | critical | bot_owner | runtime_manifest |  | command:schedule.resolve | c7beff0aabc189f33100d7150bb448309b810b262042003e943bda4347c6200c |
| cap-run-schedule-uncertain | operations.scheduling-notification | 重複防止で保留したreminder配信を表示 | unknown | unknown | high | bot_owner | runtime_manifest |  | command:schedule.uncertain | 8f78f9e1c5f09b6598da8452d3ee51400a43816f647dd0bae58d104419dab91b |
| cap-run-selfrole-add | interaction.discord-surface | self-role候補を追加 | unknown | unknown | high | moderator | runtime_manifest |  | command:selfrole.add | 4ae608602c28cb31c31283f57c29626af4f00ac5fd32964651ef19bcd744444e |
| cap-run-selfrole-panel | interaction.discord-surface | self-role panelを設置 | unknown | unknown | high | moderator | runtime_manifest |  | command:selfrole.panel | d25dc7fa089263ce3c5b94ff686a3619acd2a690a820387c671c706b6ecce46e |
| cap-run-selfrole-remove | interaction.discord-surface | self-role候補を削除 | unknown | unknown | high | moderator | runtime_manifest |  | command:selfrole.remove | 5ba9b78e9edf01eda510b4e6cf260737f5b1f2bc44f5ad7945518039cbc61ce9 |
| cap-run-selfrole-toggle | interaction.discord-surface | persistent buttonから安全なroleを付け外し | unknown | unknown | medium | everyone | runtime_manifest |  | event:component.selfrole-toggle | fd552f9898c53ba1d5bbc31bcee33ee57e4702ef97642cbb92323fd967ed18cf |
| cap-run-server-announce | security.access-control | mentionを制限して告知 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.announce | f4079ab097dff32b71b2dab58d9da26af11f49e686aeefc5d6552bf8fc537066 |
| cap-run-server-config-show | security.access-control | servertools設定を表示 | moderation | moderation | medium | guild_admin | runtime_manifest |  | command:server.config-show | 39dbb6e7289ec285954d27623ec35e30b684e2d0db49b92f23eb229fc3eeb3d4 |
| cap-run-server-goodbye-set | security.access-control | goodbye messageを設定 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.goodbye-set | d84f5f4454a750129ea2ae74df198d0998a75c780d8214292b4541c1a8d9e2d7 |
| cap-run-server-lock | security.access-control | channelをlock | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.lock | 741e425e154c9dddeb82988dedf6eedb678acb7f84f351c5a0dab84beb2a2608 |
| cap-run-server-log-channel | security.access-control | server監査log channelを設定 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.log-channel | 307819beccd5c1c209ba62e4c76c0b2f002c4301841d0137ae205cce7702e2df |
| cap-run-server-member-join | security.access-control | member join時のwelcome通知 | moderation | moderation | medium | everyone | runtime_manifest |  | event:member_join | 32ff4c60d34e36ee20fcb358306c5aede104909934f7ce4e1eb45c15a64b77f8 |
| cap-run-server-member-remove | security.access-control | member remove時のgoodbye通知 | moderation | moderation | medium | everyone | runtime_manifest |  | event:member_remove | 177bc76e14e55afec279baf8a3681c6b26fe336de04a2fdb0a3be7c2e79de7d0 |
| cap-run-server-message-delete | security.access-control | message削除を監査channelへ通知 | moderation | moderation | high | guild_admin | runtime_manifest |  | event:message_delete | 95f9ec0c60789afd0dcb5f74dd2e12b16342851569959250adfba759b14a170b |
| cap-run-server-message-edit | security.access-control | message編集を監査channelへ通知 | moderation | moderation | high | guild_admin | runtime_manifest |  | event:message_edit | 831ccaf0180b498379c0e9393e4a50a8b4f296e1cfaf6b78968834621928e2cd |
| cap-run-server-nick | security.access-control | member nicknameを変更 | moderation | moderation | high | moderator | runtime_manifest |  | command:server.nick | b66a8809d42aca17a53e17424ff36555000476c9905364671c2a3eb54917bdd8 |
| cap-run-server-role-add | security.access-control | memberへroleを追加 | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:server.role-add | c319132f2fb4425dfec09dbead20cf1f87f32d6cff7c7bc5e657af4127d36292 |
| cap-run-server-role-remove | security.access-control | memberからroleを削除 | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:server.role-remove | 5bcc872e6184d054a75ae6e41b81d69fe4a374747f1205c41b79651435b7de02 |
| cap-run-server-slowmode | security.access-control | channel slowmodeを設定 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.slowmode | 65148d46185fa6e411f27769bf20b26b96b7a3b4c953ca07146360260ad0f904 |
| cap-run-server-unlock | security.access-control | channelをunlock | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.unlock | dec57970b1aea7b0ddf9dd1b50814381325e5936dacf3277a6b5bbef8b96c273 |
| cap-run-server-welcome-set | security.access-control | welcome messageを設定 | moderation | moderation | high | guild_admin | runtime_manifest |  | command:server.welcome-set | 68f1a3df108a534abf84c4075b28875ec0488ff195c198c7f2eebba9a14e27b0 |
| cap-run-site-archive | publishing.site-host | 生成サイトを配信停止して監査履歴へ保存 | site | site | high | guild_admin | runtime_manifest |  | command:site.archive | 51b17b1f0d0c531c1ada9be01bc21ed795af811861173a300d2a6e9a0b780edd |
| cap-run-site-auto-publish | publishing.site-host | 明示的なサイト作成依頼を自動公開へ接続 | site | site | high | trusted | runtime_manifest |  | event:site_auto_publish | 8832ec4ef88c2fb20bf99b0bafc8cd73dea92e6775294b4e21c9c1086cd6f915 |
| cap-run-site-domain-manage | publishing.site-host | サイト配信用ドメインと公開gatewayを管理 | site | site | critical | bot_owner | runtime_manifest |  | command:site.domain | 77d353c8434dc079afaef7f5243779f0037a83d88a73dfa4edc0dea8bc7cb2f7 |
| cap-run-site-list | publishing.site-host | 閲覧可能な生成サイトを一覧表示 | site | site | medium | everyone | runtime_manifest |  | command:site.list | f3abd4ef96c665b1ed6ea3be27ec9ae3b6e23ec5c7a30d265672fd27e2601d39 |
| cap-run-site-publish | publishing.site-host | 検証済みHTMLを専用サブドメインへ公開 | site | site | high | trusted | runtime_manifest |  | command:site.publish | 03abe8a001d54c2e19217e4a3720175c259469a34263dfc2d6d2646ed9023894 |
| cap-run-site-rollback | publishing.site-host | 生成サイトを検証済みの旧版へ戻す | site | site | high | guild_admin | runtime_manifest |  | command:site.rollback | e85bdf3e11257adaa060ee4a7eac81ea1f4e5de247a2336db257f671a36334fc |
| cap-run-site-show | publishing.site-host | 生成サイトの版と公開状態を表示 | site | site | medium | everyone | runtime_manifest |  | command:site.show | 562eede4e63b7ed2c85183c8ca3147aeb84b72de7238ee9c28ea9101f6b5cbce |
| cap-run-site-status | publishing.site-host | サイト公開基盤の状態を表示 | site | site | low | everyone | runtime_manifest |  | command:site.status | 64586cf4287b629095f294747b02512161e37b94aa6847246342ee5bddc09799 |
| cap-run-site-update | publishing.site-host | 所有する生成サイトを新しい版へ更新 | site | site | high | trusted | runtime_manifest |  | command:site.update | 754d08c0f5baf69e639b90214b24a635809ca6d5519f5f271fe9960372458359 |
| cap-run-site-visibility | publishing.site-host | 生成サイトの公開範囲を変更 | site | site | high | guild_admin | runtime_manifest |  | command:site.visibility | 510aeda1fbc0ed8b3ce06b7f08248a7578402f457af6dc0d12da0f3958da142c |
| cap-run-suggest-create | interaction.discord-surface | 提案を登録 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:suggest.create | b71d233b07de6427451a1b049e11ecff464bf3a2e9cd959139d267327731e5b7 |
| cap-run-suggest-status | interaction.discord-surface | 提案状態を表示・更新 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:suggest.status | 7c9fe71df2754379182bb9d11efba343cec5a551c28429e1b388348f6570fe4e |
| cap-run-system-audit | operations.observability | append-only監査logを表示 | unknown | unknown | high | guild_admin | runtime_manifest |  | command:system.audit | 69d53851a0ada26613318de6a09aba82c6eda9375033a4ca4dfc03a0aa1c00c7 |
| cap-run-system-backup | data.storage | SQLite online backupとquick_checkを実行 | unknown | unknown | critical | bot_owner | runtime_manifest |  | command:system.backup | d662c6d5431251e60de0741e601ca753c940417a6be36ffbca361c03d86250b6 |
| cap-run-system-overrides | operations.observability | guildのmodule/capability/RBAC overrideを表示 | unknown | unknown | high | guild_admin | runtime_manifest |  | command:system.overrides | 220e4f5191eb32fc07e9f601ec2220c994f018eda2184737ad0eed80fc988c79 |
| cap-run-system-runtime | operations.observability | 実tree/plugin/handler inventoryを表示 | unknown | unknown | high | guild_admin | runtime_manifest |  | command:system.runtime | 5c87e27d49221283240f40b6f1c1710850deb1c2036835daf38d3827185c3ff7 |
| cap-run-ticket-add | interaction.discord-surface | ticket参加者を追加 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:ticket.add | 1f4480562aabb5936354e33ab6f5bee1bac7905b58c4b359427bea7188fdced7 |
| cap-run-ticket-close | interaction.discord-surface | ticketを閉じる | knowledge | knowledge | low | everyone | runtime_manifest |  | command:ticket.close | 9a5dcb7576fd91111ebfca84bf6e3dc8ed3c7cced29e6dae8a75357eb539ee05 |
| cap-run-ticket-open | interaction.discord-surface | 非公開ticketを作成 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:ticket.open | 13dd319cbfc3ce23d75b87b624ccb065b57980d74092ca0dac895ccf41641f6a |
| cap-run-ticket-remove | interaction.discord-surface | ticket参加者を削除 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:ticket.remove | 0a42ba8601723f0a28e15253bafdbd54aff3987b0818ea4ae035b3dd9165978d |
| cap-run-ticket-transcript-info | interaction.discord-surface | ticket transcriptのprivacy方針を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:ticket.transcript-info | 110f423cd9ffd1fc96e6b0c09d0b1c3a6dd5686d8d861a9aa05a1834aab13ebb |
| cap-run-tools-choose | utility.general | 候補から安全に抽選 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.choose | 3d67dc27a96e1e506b3be95b9cbc04965798627f90ffa04f77d592d017410f0a |
| cap-run-tools-color | utility.general | HEX colorを検証 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.color | 47d065191fa28bf9c24d0d1cb6d2991953f1d93aefc46820b4e5c2adbec95342 |
| cap-run-tools-dice | utility.general | dice式を評価 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.dice | e5b51006e90403102980f945a48b5918a6151b9a80da667dd9a2ac76c80a2d74 |
| cap-run-tools-random | utility.general | 範囲内の整数を抽選 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.random | 0382ec704c9521d924fa22246612d25eaf8934965c8ab2c602c0cd90bfca336a |
| cap-run-tools-sha256 | utility.general | 入力のSHA-256を計算 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.sha256 | 892872510f13c28249497a0ed0785ac06b11506b675f041582220ad47e1792af |
| cap-run-tools-snowflake | utility.general | Discord Snowflake日時を表示 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.snowflake | 17c2ac4738f00e4b4a340c9138f49675b89445ab71c9826791c390e16258dd04 |
| cap-run-tools-timestamp | utility.general | Discord timestampへ変換 | knowledge | knowledge | low | everyone | runtime_manifest |  | command:tools.timestamp | 33bfba9c695c7aec28a16f01dfcb791718adb385a2ec325664b21d6c10f161cc |
| cap-run-verify-configure | security.access-control | 安全なverified roleとguild側switchを設定 | moderation | moderation | critical | guild_admin | runtime_manifest |  | command:verify.configure | 6c386eda50cb7d09a6cbfceed23cc4ae007cd5503f4b5d1e99329913bb2171a7 |
| cap-run-verify-start | security.access-control | 本人専用の短時間ワンタイム認証URLを発行 | moderation | moderation | medium | everyone | runtime_manifest |  | command:verify.start | c562ee5bd4bfd6aa4406c1f52db0808fa3aab047df92240d519c2960170bce47 |
| cap-run-verify-status | security.access-control | 本人確認の安全設定とguild側状態を表示 | moderation | moderation | low | everyone | runtime_manifest |  | command:verify.status | 836eb1397ab4424d68df438f80912ec49a8ccbbeccd8c9a5242917f844d984ca |
| cap-run-video-generate | media.video-generation | 明示promptから検証済みMP4動画を生成 | unknown | unknown | high | trusted | runtime_manifest |  | command:video.generate | 68745dabb13b6503197120a303e8bf19b6acd0929609888b4fb451a5a6749c3a |
| cap-run-warning | operations.public-information | 気象庁の公式公開データから警報・注意報を表示 | knowledge | knowledge, web_research | low | everyone | runtime_manifest |  | command:warning | eeb95f1019daed95c23fa6d223fa7ebbf1476e9981d0cec03f5dc7d40c4dd696 |
| cap-run-weather | operations.public-information | 気象庁の公式公開データから地域別天気予報を表示 | knowledge | knowledge, web_research | low | everyone | runtime_manifest |  | command:weather | ebfc121ec9c9f7ce0ab4e40d3aef71efc76ab31d798eb468e1896a7b648e18e6 |
| cap-run-web-search-openai-paid | integration.api-web | Owner明示opt-in専用のOpenAI有料Web検索tool | web_research | web_research | medium | bot_owner | runtime_manifest | web_search |  | 97593b858bb42f26f2669704c2327536401062a582d9e8c49a8e440b4120cb7d |
| cap-run-yonerai-health | operations.observability | 許可済みYonerAI readiness contractだけを照会 | unknown | unknown | medium | guild_admin | runtime_manifest |  | command:yonerai.health | 64ea8f0c00a9491532ad2103a84b69f2139091ed9bd6239be3544d397a01fb72 |
| cap-run-yonerai-status | operations.observability | YonerAI将来連携のread-only境界状態を表示 | unknown | unknown | medium | guild_admin | runtime_manifest |  | command:yonerai.status | fb005b127dda5336f8a84c79de93d0f540a4c37a1b2424837fd2ac757c621762 |
