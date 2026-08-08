# Public Command and Surface Index

静的binding一覧です。Discord登録、設定、runtime readiness、live成功は別の証拠です。

## Command paths

| path | capability_id | module | risk | minimum RBAC | public availability |
| --- | --- | --- | --- | --- | --- |
| `/ai ask` | `cap-can-0161` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `/ai model auto` | `cap-run-ai-model-auto` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `/ai model list` | `cap-run-ai-model-list` | `intelligence.ai-runtime` | `low` | `everyone` | `integrated_offline` |
| `/ai model set` | `cap-run-ai-model-set` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `/ai provider list` | `cap-run-ai-provider-list` | `intelligence.ai-runtime` | `low` | `everyone` | `integrated_offline` |
| `/ai provider set` | `cap-run-ai-provider-set` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `/ai reset` | `cap-run-ai-reset` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `/ai route` | `cap-run-ai-route` | `intelligence.ai-runtime` | `low` | `everyone` | `integrated_offline` |
| `/ai search` | `cap-can-0153` | `integration.api-web` | `medium` | `everyone` | `integrated_offline` |
| `/ai status` | `cap-can-0162` | `intelligence.ai-runtime` | `low` | `everyone` | `integrated_offline` |
| `/automod channel` | `cap-run-automod-channel` | `moderation.automod` | `high` | `guild_admin` | `integrated_offline` |
| `/automod policy` | `cap-run-automod-policy` | `moderation.automod` | `high` | `guild_admin` | `integrated_offline` |
| `/automod status` | `cap-run-automod-status` | `moderation.automod` | `medium` | `guild_admin` | `integrated_offline` |
| `/earthquake latest` | `cap-run-earthquake-latest` | `operations.earthquake` | `medium` | `everyone` | `integrated_offline` |
| `/earthquake status` | `cap-run-earthquake-status` | `operations.earthquake` | `low` | `everyone` | `integrated_offline` |
| `/earthquake subscribe` | `cap-run-earthquake-subscribe` | `operations.earthquake` | `high` | `guild_admin` | `integrated_offline` |
| `/earthquake unsubscribe` | `cap-run-earthquake-unsubscribe` | `operations.earthquake` | `high` | `guild_admin` | `integrated_offline` |
| `/evolution approve` | `cap-run-evolution-approve` | `intelligence.ai-runtime` | `high` | `bot_owner` | `integrated_offline` |
| `/evolution propose` | `cap-run-evolution-propose` | `intelligence.ai-runtime` | `high` | `bot_owner` | `integrated_offline` |
| `/evolution reject` | `cap-run-evolution-reject` | `intelligence.ai-runtime` | `high` | `bot_owner` | `integrated_offline` |
| `/evolution review` | `cap-run-evolution-review` | `intelligence.ai-runtime` | `high` | `bot_owner` | `integrated_offline` |
| `/evolution show` | `cap-run-evolution-show` | `intelligence.ai-runtime` | `high` | `bot_owner` | `integrated_offline` |
| `/evolution status` | `cap-run-evolution-status` | `intelligence.ai-runtime` | `low` | `bot_owner` | `integrated_offline` |
| `/help` | `cap-run-discovery-help` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/holiday next` | `cap-run-holiday-next` | `operations.public-information` | `low` | `everyone` | `integrated_offline` |
| `/holiday year` | `cap-run-holiday-year` | `operations.public-information` | `low` | `everyone` | `integrated_offline` |
| `/image generate` | `cap-run-image-generate` | `media.image-generation` | `high` | `trusted` | `integrated_offline` |
| `/info avatar` | `cap-run-info-avatar` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/info channel` | `cap-run-info-channel` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/info permissions` | `cap-run-info-permissions` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/info role` | `cap-run-info-role` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/info server` | `cap-run-info-server` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/info user` | `cap-run-info-user` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/jobs cancel` | `cap-run-jobs-cancel` | `operations.execution` | `high` | `guild_admin` | `integrated_offline` |
| `/jobs list` | `cap-run-jobs-list` | `operations.execution` | `medium` | `guild_admin` | `integrated_offline` |
| `/jobs retry` | `cap-run-jobs-retry` | `operations.execution` | `high` | `guild_admin` | `integrated_offline` |
| `/jobs status` | `cap-run-jobs-status` | `operations.execution` | `medium` | `guild_admin` | `integrated_offline` |
| `/memory clear` | `cap-run-memory-clear` | `intelligence.personal-memory` | `critical` | `everyone` | `integrated_offline` |
| `/memory disable` | `cap-run-memory-disable` | `intelligence.personal-memory` | `low` | `everyone` | `integrated_offline` |
| `/memory enable` | `cap-run-memory-enable` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory export` | `cap-run-memory-export` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory forget` | `cap-run-memory-forget` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory list` | `cap-run-memory-list` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory preview` | `cap-run-memory-preview` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory privacy` | `cap-run-memory-privacy` | `intelligence.personal-memory` | `low` | `everyone` | `integrated_offline` |
| `/memory remember` | `cap-run-memory-remember` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory search` | `cap-run-memory-search` | `intelligence.personal-memory` | `medium` | `everyone` | `integrated_offline` |
| `/memory status` | `cap-run-memory-status` | `intelligence.personal-memory` | `low` | `everyone` | `integrated_offline` |
| `/minecraft status` | `cap-run-minecraft-status` | `gaming.minecraft` | `low` | `everyone` | `integrated_offline` |
| `/mod ban` | `cap-run-mod-ban` | `moderation.actions` | `critical` | `guild_admin` | `integrated_offline` |
| `/mod case` | `cap-run-mod-case` | `moderation.actions` | `medium` | `moderator` | `integrated_offline` |
| `/mod kick` | `cap-run-mod-kick` | `moderation.actions` | `critical` | `guild_admin` | `integrated_offline` |
| `/mod purge` | `cap-run-mod-purge` | `moderation.actions` | `critical` | `guild_admin` | `integrated_offline` |
| `/mod purge-links` | `cap-run-mod-purge-links` | `moderation.actions` | `critical` | `guild_admin` | `integrated_offline` |
| `/mod purge-user` | `cap-run-mod-purge-user` | `moderation.actions` | `critical` | `guild_admin` | `integrated_offline` |
| `/mod timeout` | `cap-run-mod-timeout` | `moderation.actions` | `critical` | `guild_admin` | `integrated_offline` |
| `/mod unban` | `cap-run-mod-unban` | `moderation.actions` | `high` | `guild_admin` | `integrated_offline` |
| `/mod untimeout` | `cap-run-mod-untimeout` | `moderation.actions` | `high` | `moderator` | `integrated_offline` |
| `/mod warn` | `cap-run-mod-warn` | `moderation.actions` | `high` | `moderator` | `integrated_offline` |
| `/mod warnings` | `cap-run-mod-warnings` | `moderation.actions` | `medium` | `moderator` | `integrated_offline` |
| `/music clear-mine` | `cap-run-music-clear-mine` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music import` | `cap-run-music-import` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music join` | `cap-run-music-join` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music leave` | `cap-run-music-leave` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music loop` | `cap-run-music-loop` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music move` | `cap-run-music-move` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music now` | `cap-run-music-now` | `media.music` | `low` | `everyone` | `integrated_offline` |
| `/music pause` | `cap-run-music-pause` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music play` | `cap-run-music-play` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music playlist delete` | `cap-run-music-playlist-delete` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music playlist list` | `cap-run-music-playlist-list` | `media.music` | `low` | `everyone` | `integrated_offline` |
| `/music playlist load` | `cap-run-music-playlist-load` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music playlist save` | `cap-run-music-playlist-save` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music queue` | `cap-run-music-queue` | `media.music` | `low` | `everyone` | `integrated_offline` |
| `/music radio` | `cap-run-music-radio` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music read-aloud dictionary-delete` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud dictionary-set` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud disable` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud enable` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud exclude-add` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud exclude-delete` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud focus-cancel` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud focus-start` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud list` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud my-preset` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music read-aloud policy` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music read-aloud preset` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music read-aloud server-preset` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |
| `/music remove` | `cap-run-music-remove` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music resume` | `cap-run-music-resume` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music search` | `cap-run-music-search` | `media.music` | `low` | `everyone` | `integrated_offline` |
| `/music search-youtube` | `cap-run-music-search-youtube` | `media.music` | `low` | `everyone` | `integrated_offline` |
| `/music seek` | `cap-run-music-seek` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music shuffle` | `cap-run-music-shuffle` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music skip` | `cap-run-music-skip` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music speak` | `cap-run-music-speak` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music status` | `cap-run-music-status` | `media.music` | `low` | `everyone` | `integrated_offline` |
| `/music stop` | `cap-run-music-stop` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/music volume` | `cap-run-music-volume` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `/musicgen generate` | `cap-run-music-generate` | `media.music-generation` | `high` | `trusted` | `integrated_offline` |
| `/nasa apod` | `cap-run-nasa-apod-read` | `operations.nasa-apod` | `low` | `everyone` | `integrated_offline` |
| `/poll close` | `cap-run-poll-close` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/poll create` | `cap-run-poll-create` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/poll results` | `cap-run-poll-results` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/schedule cancel` | `cap-can-0001` | `collaboration.meeting` | `medium` | `everyone` | `integrated_offline` |
| `/schedule create` | `cap-can-0002` | `collaboration.meeting` | `medium` | `everyone` | `integrated_offline` |
| `/schedule list` | `cap-can-0007` | `collaboration.meeting` | `low` | `everyone` | `integrated_offline` |
| `/schedule remind` | `cap-can-0538` | `operations.scheduling-notification` | `medium` | `everyone` | `integrated_offline` |
| `/schedule resolve` | `cap-run-schedule-resolve` | `operations.scheduling-notification` | `critical` | `bot_owner` | `integrated_offline` |
| `/schedule rsvp` | `cap-can-0030` | `collaboration.meeting` | `medium` | `everyone` | `integrated_offline` |
| `/schedule show` | `cap-can-0272` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/schedule uncertain` | `cap-run-schedule-uncertain` | `operations.scheduling-notification` | `high` | `bot_owner` | `integrated_offline` |
| `/selfrole add` | `cap-run-selfrole-add` | `interaction.discord-surface` | `high` | `moderator` | `integrated_offline` |
| `/selfrole panel` | `cap-run-selfrole-panel` | `interaction.discord-surface` | `high` | `moderator` | `integrated_offline` |
| `/selfrole remove` | `cap-run-selfrole-remove` | `interaction.discord-surface` | `high` | `moderator` | `integrated_offline` |
| `/server announce` | `cap-run-server-announce` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/server config-show` | `cap-run-server-config-show` | `security.access-control` | `medium` | `guild_admin` | `integrated_offline` |
| `/server goodbye-set` | `cap-run-server-goodbye-set` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/server lock` | `cap-run-server-lock` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/server log-channel` | `cap-run-server-log-channel` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/server nick` | `cap-run-server-nick` | `security.access-control` | `high` | `moderator` | `integrated_offline` |
| `/server role-add` | `cap-run-server-role-add` | `security.access-control` | `critical` | `guild_admin` | `integrated_offline` |
| `/server role-remove` | `cap-run-server-role-remove` | `security.access-control` | `critical` | `guild_admin` | `integrated_offline` |
| `/server slowmode` | `cap-run-server-slowmode` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/server unlock` | `cap-run-server-unlock` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/server welcome-set` | `cap-run-server-welcome-set` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `/site archive` | `cap-run-site-archive` | `publishing.site-host` | `high` | `guild_admin` | `unavailable_public` |
| `/site domain` | `cap-run-site-domain-manage` | `publishing.site-host` | `critical` | `bot_owner` | `unavailable_public` |
| `/site list` | `cap-run-site-list` | `publishing.site-host` | `medium` | `everyone` | `unavailable_public` |
| `/site publish` | `cap-run-site-publish` | `publishing.site-host` | `high` | `trusted` | `unavailable_public` |
| `/site rollback` | `cap-run-site-rollback` | `publishing.site-host` | `high` | `guild_admin` | `unavailable_public` |
| `/site show` | `cap-run-site-show` | `publishing.site-host` | `medium` | `everyone` | `unavailable_public` |
| `/site status` | `cap-run-site-status` | `publishing.site-host` | `low` | `everyone` | `unavailable_public` |
| `/site update` | `cap-run-site-update` | `publishing.site-host` | `high` | `trusted` | `unavailable_public` |
| `/site visibility` | `cap-run-site-visibility` | `publishing.site-host` | `high` | `guild_admin` | `unavailable_public` |
| `/suggest create` | `cap-run-suggest-create` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/suggest status` | `cap-run-suggest-status` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/system audit` | `cap-run-system-audit` | `operations.observability` | `high` | `guild_admin` | `integrated_offline` |
| `/system backup` | `cap-run-system-backup` | `data.storage` | `critical` | `bot_owner` | `integrated_offline` |
| `/system capabilities` | `cap-can-0589` | `platform.runtime` | `low` | `guild_admin` | `integrated_offline` |
| `/system capability-set` | `cap-can-0613` | `security.access-control` | `low` | `guild_admin` | `integrated_offline` |
| `/system doctor` | `cap-can-0278` | `interaction.discord-surface` | `low` | `guild_admin` | `integrated_offline` |
| `/system health` | `cap-can-0519` | `operations.observability` | `low` | `guild_admin` | `integrated_offline` |
| `/system module-set` | `cap-can-0613` | `security.access-control` | `low` | `guild_admin` | `integrated_offline` |
| `/system modules` | `cap-can-0589` | `platform.runtime` | `low` | `guild_admin` | `integrated_offline` |
| `/system overrides` | `cap-run-system-overrides` | `operations.observability` | `high` | `guild_admin` | `integrated_offline` |
| `/system permission-set` | `cap-can-0613` | `security.access-control` | `low` | `guild_admin` | `integrated_offline` |
| `/system ping` | `cap-can-0265` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/system plugins` | `cap-can-0588` | `platform.runtime` | `low` | `guild_admin` | `integrated_offline` |
| `/system runtime` | `cap-run-system-runtime` | `operations.observability` | `high` | `guild_admin` | `integrated_offline` |
| `/ticket add` | `cap-run-ticket-add` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/ticket close` | `cap-run-ticket-close` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/ticket open` | `cap-run-ticket-open` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/ticket remove` | `cap-run-ticket-remove` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/ticket transcript-info` | `cap-run-ticket-transcript-info` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `/tools choose` | `cap-run-tools-choose` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/tools color` | `cap-run-tools-color` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/tools dice` | `cap-run-tools-dice` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/tools random` | `cap-run-tools-random` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/tools sha256` | `cap-run-tools-sha256` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/tools snowflake` | `cap-run-tools-snowflake` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/tools timestamp` | `cap-run-tools-timestamp` | `utility.general` | `low` | `everyone` | `integrated_offline` |
| `/verify configure` | `cap-run-verify-configure` | `security.access-control` | `critical` | `guild_admin` | `integrated_offline` |
| `/verify start` | `cap-run-verify-start` | `security.access-control` | `medium` | `everyone` | `integrated_offline` |
| `/verify status` | `cap-run-verify-status` | `security.access-control` | `low` | `everyone` | `integrated_offline` |
| `/video generate` | `cap-run-video-generate` | `media.video-generation` | `high` | `trusted` | `integrated_offline` |
| `/voice status` | `cap-can-0415` | `media.voice` | `low` | `everyone` | `integrated_offline` |
| `/voice synthesize` | `cap-can-0416` | `media.voice` | `medium` | `everyone` | `integrated_offline` |
| `/warning` | `cap-run-warning` | `operations.public-information` | `low` | `everyone` | `integrated_offline` |
| `/weather` | `cap-run-weather` | `operations.public-information` | `low` | `everyone` | `integrated_offline` |
| `/web fetch` | `cap-can-0153` | `integration.api-web` | `medium` | `everyone` | `integrated_offline` |
| `/web find` | `cap-can-0153` | `integration.api-web` | `medium` | `everyone` | `integrated_offline` |
| `/web search` | `cap-can-0153` | `integration.api-web` | `medium` | `everyone` | `integrated_offline` |
| `/yonerai health` | `cap-run-yonerai-health` | `operations.observability` | `medium` | `guild_admin` | `integrated_offline` |
| `/yonerai status` | `cap-run-yonerai-status` | `operations.observability` | `medium` | `guild_admin` | `integrated_offline` |

## Event paths

| path | capability_id | module | risk | minimum RBAC | public availability |
| --- | --- | --- | --- | --- | --- |
| `ai_mention_message` | `cap-run-ai-mention-chat` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `automod_message_create` | `cap-run-automod-message-create` | `moderation.automod` | `high` | `guild_admin` | `integrated_offline` |
| `automod_message_edit` | `cap-run-automod-message-edit` | `moderation.automod` | `high` | `guild_admin` | `integrated_offline` |
| `component.poll-vote` | `cap-run-poll-vote` | `interaction.discord-surface` | `low` | `everyone` | `integrated_offline` |
| `component.selfrole-toggle` | `cap-run-selfrole-toggle` | `interaction.discord-surface` | `medium` | `everyone` | `integrated_offline` |
| `earthquake_feed_delivery` | `cap-run-earthquake-delivery` | `operations.earthquake` | `high` | `guild_admin` | `integrated_offline` |
| `member_join` | `cap-run-server-member-join` | `security.access-control` | `medium` | `everyone` | `integrated_offline` |
| `member_remove` | `cap-run-server-member-remove` | `security.access-control` | `medium` | `everyone` | `integrated_offline` |
| `message_delete` | `cap-run-server-message-delete` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `message_edit` | `cap-run-server-message-edit` | `security.access-control` | `high` | `guild_admin` | `integrated_offline` |
| `message_link_expand` | `cap-run-message-link-expand` | `intelligence.ai-runtime` | `medium` | `everyone` | `integrated_offline` |
| `music_read_aloud_message` | `cap-run-music-read-aloud-message` | `media.music` | `medium` | `everyone` | `integrated_offline` |
| `site_auto_publish` | `cap-run-site-auto-publish` | `publishing.site-host` | `high` | `trusted` | `unavailable_public` |
| `worker.jobs-execute` | `cap-run-jobs-execute` | `operations.execution` | `high` | `guild_admin` | `integrated_offline` |

## Typed planner actions

| path | capability_id | module | risk | minimum RBAC | public availability |
| --- | --- | --- | --- | --- | --- |
| `browser interact` | `cap-run-browser-remote-interactive` | `web.browser-rendering` | `high` | `bot_owner` | `integrated_offline` |
| `browser screenshot` | `cap-run-browser-remote-screenshot` | `web.browser-rendering` | `high` | `bot_owner` | `integrated_offline` |
| `image edit` | `cap-run-image-edit` | `media.image-editing` | `high` | `trusted` | `integrated_offline` |
| `media compose-grid` | `cap-run-media-compose-grid` | `media.pipeline` | `high` | `trusted` | `integrated_offline` |
| `media discord-asset-inspect` | `cap-run-media-discord-asset-inspect` | `media.pipeline` | `medium` | `trusted` | `integrated_offline` |
| `media place-on-canvas` | `cap-run-media-place-on-canvas` | `media.pipeline` | `high` | `trusted` | `integrated_offline` |
| `media qr-encode` | `cap-run-media-qr-encode` | `media.pipeline` | `high` | `trusted` | `integrated_offline` |
| `media quote-card` | `cap-run-media-quote-card` | `media.pipeline` | `medium` | `trusted` | `integrated_offline` |
| `media url-inspect` | `cap-run-media-url-inspection` | `media.url-inspection` | `high` | `bot_owner` | `integrated_offline` |
