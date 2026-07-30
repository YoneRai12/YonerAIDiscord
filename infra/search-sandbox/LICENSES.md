# Search Sandbox third-party notices

## SearXNG

- Project: SearXNG
- Upstream: https://github.com/searxng/searxng
- License: `AGPL-3.0-or-later`
- Pinned source/image version: `2026.7.26-b060c780d`
- License text: https://github.com/searxng/searxng/blob/master/LICENSE
- Corresponding source: https://github.com/searxng/searxng/tree/2026.7.26-b060c780d

YonerAIのtracked assetは、SearXNG本体のPython sourceを改変せず、別serviceとして
固定image、`settings.yml`、network/resource境界を構成します。将来SearXNG本体を
改変してnetwork経由で提供する場合は、AGPLに従って対応sourceを利用者が取得できる
方法、copyright notice、変更記録をrelease reviewで確認します。imageを再配布する
場合も同じ確認が必要です。

## Gateway runtime dependencies

GatewayのPython dependencyは`gateway-requirements.lock`へexact versionで記録します。
各dependencyのlicenseはproduction image build時のSBOM/license inventoryで再確認し、
その結果が無いimageをpublic配布しません。GatewayはYonerAIの別serviceであり、
SearXNGを検索候補discovery backendとしてprivate network越しに利用します。

## Ubuntu and container runtime

Ubuntu ServerとPodman/podman-composeはoperatorがprivate Hyper-V guestを構築するための
runtime候補です。install済みpackage versionはguest provisioning receiptで記録し、
repositoryへmachine固有値やcredentialを保存しません。これらを含むimageの再配布前に
各packageのlicense inventoryを別途確認します。

この文書は運用上のprovenance記録であり、法的助言ではありません。public network提供、
image配布、SearXNG本体変更の最終判断はowner/legal reviewが必要です。
