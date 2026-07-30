# Third-Party Notices / 第三者通知

日本語: 依存関係および第三者素材には、それぞれのライセンスと通知が適用されます。公開alphaが固定するPython依存の版、license、source URLは`DEPENDENCY_LICENSES.json`が機械可読正本です。

English: Dependencies and third-party materials remain subject to their own licenses and notices. `DEPENDENCY_LICENSES.json` is the machine-readable record of the Python versions, licenses, and source URLs locked by this public alpha.

## Named components / 主なコンポーネント

日本語: discord.pyとPlaywright Python packageは公開alphaのPython依存です。SearXNGは固定版の別service構築資材だけを含み、本体imageは同梱しません。VOICEVOX、FFmpeg、yt-dlpは任意の外部runtime連携先であり、このrepositoryやwheelにbinaryを同梱しません。

English: discord.py and the Playwright Python package are locked Python dependencies. The repository includes pinned deployment material for a separate SearXNG service but does not bundle its image. VOICEVOX, FFmpeg, and yt-dlp are optional external runtimes; their binaries are not shipped in this repository or wheel.

Before installing or redistributing an optional runtime or container image, obtain its license text and notices from the upstream source identified by that runtime's own version receipt. YonerAI's license does not replace those terms.
