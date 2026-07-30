# Model Catalog recommendations（2026-07-24 refresh）

この文書は採用候補の調査結果であり、ダウンロード済み・API接続済み・本番readyを意味しない。
Discord handlerは実モデルIDを参照せず、`capability × locality × tier` の論理aliasだけを要求する。
機械可読の助言用正本は
`src/yonerai_discord/provider_registry/manifests/provider-recommendations.rtx5090.v2.json`
である。24組すべてが`active:false` / `probe_required:true`で、active provider catalogやruntime routeへは接続しない。
昇格条件は[`ADR_MODEL_RECOMMENDATION_PROMOTION_V2_20260723.md`](ADR_MODEL_RECOMMENDATION_PROMOTION_V2_20260723.md)
を正とする。

2026-07-24の一次資料refresh後のrecommendation revisionは
`4b080461a25e111b7238103c46320041f7cbd21d915c69f1df5ec72a7c0d7152`である。
active provider catalog revisionは
`69cbf941292301c8e24aab75d741a5e2ba27b818a67894ac3f4769b4c0f47e5d`のままで、
runtime route、adapter、capability catalog artifactを変更していない。候補の存在確認は利用可、品質、
ライセンス適合、RTX 5090上の実測、readiness、live成功を意味しない。

## Routing policy

| tier | 用途 | 自動選択 |
|---|---|---|
| `fast` | 分類、短い整形、低遅延・大量処理 | 低risk、toolなし、副作用なしだけ |
| `balanced` | 通常会話と通常生成 | 既定 |
| `quality` | 複雑推論、コード、tool利用、高品質生成 | 高難度・高risk・品質優先 |

APIとlocalは別routeであり、local障害時にAPIへ自動送信しない。API fallbackには保存済みの外部送信同意と
現在のcapability policyを再確認する。Previewモデルとライセンス不適合モデルへ自動fallbackしない。

## Text / reasoning / code

| tier | API | Local（RTX 5090 32GB） |
|---|---|---|
| quality | `gpt-5.6-sol` | `nvidia/Qwen3.6-27B-NVFP4` |
| balanced | `gpt-5.6-terra` | `openai/gpt-oss-20b` |
| fast | `gpt-5.6-luna` | `Qwen/Qwen3.5-4B` |

API側は現在の安全routing（Terra既定、Sol複雑、Luna極小）を維持する。Local側のcontextはKV cacheを考慮し、
初期16K〜32K、必要時だけ拡張する。`gpt-oss-20b`は公式の16 GB以内というmemory claimを候補根拠にするが、
Harmony format対応と実機probeが必要で、chain-of-thoughtを利用者へ表示しない。
NVIDIA NVFP4候補はgeneric vLLMではなく、nightly vLLM、ModelOpt quantization、Qwen3 reasoning parser、
Blackwell GPU、bounded contextを必須profileとする。公式検証環境はGB300であり、RTX 5090実測ではない。

- [OpenAI latest model guide](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI gpt-oss-20b model card](https://huggingface.co/openai/gpt-oss-20b)
- [OpenAI gpt-oss repository](https://github.com/openai/gpt-oss)
- [Qwen3.6 27B NVFP4](https://huggingface.co/nvidia/Qwen3.6-27B-NVFP4)
- [Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B)

## Vision / OCR

| tier | API | Local（RTX 5090 32GB） |
|---|---|---|
| quality | `gpt-5.6-sol` | `nvidia/Qwen3.6-27B-NVFP4` |
| balanced | `gpt-5.6-terra` | `Qwen/Qwen3.5-9B` |
| fast | `gpt-5.6-luna` | `Qwen/Qwen3.5-4B` |

`PaddleOCR-VL-1.5`は公式model cardが後継1.6を案内しているため新規推奨から外した。
`PaddlePaddle/PaddleOCR-VL-1.6`は文書OCR専用の差替え候補だが、汎用`vision.understand` tierとは
互換ではないため24組sidecarへ混ぜない。専用OCR capabilityを定義した別commitで、依存codeとrevisionを
固定した隔離workerによるprobeが必要である。

- [PaddleOCR-VL-1.6 model card](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)
- [PaddleOCR repository](https://github.com/PaddlePaddle/PaddleOCR)
- [Qwen3.5 9B](https://huggingface.co/Qwen/Qwen3.5-9B)
- [NVIDIA Qwen3.6 27B NVFP4](https://huggingface.co/nvidia/Qwen3.6-27B-NVFP4)
- [NVIDIA Model Optimizer](https://github.com/NVIDIA/Model-Optimizer/blob/main/examples/llm_ptq/README.md)

## Image generation / editing

| tier | API生成候補 | Local生成候補 | API編集候補 | Local編集候補 |
|---|---|---|---|---|
| quality | `gemini-3-pro-image` | `Qwen/Qwen-Image-2512` | `gpt-image-2` + `quality_high` | `Qwen/Qwen-Image-Edit-2511` |
| balanced | `gemini-3.1-flash-image` | `black-forest-labs/FLUX.2-klein-4B` | `gpt-image-2` + `quality_medium` | `black-forest-labs/FLUX.2-klein-4B` |
| fast | `gemini-3.1-flash-lite-image` | `black-forest-labs/FLUX.2-klein-4B` | `gpt-image-2` + `quality_low` | `black-forest-labs/FLUX.2-klein-4B` |

停止済みの`gemini-3.1-flash-image-preview` / `gemini-3-pro-image-preview`と、legacy
`gemini-2.5-flash-image`は新規推奨から外した。API画像生成はGA/StableのGemini IDを候補にするが、
API terms、課金、実出力contractをprobeするまでactive化しない。画像編集の3 tierは同じ`gpt-image-2`を
low/medium/high qualityで分けた助言だけである。

FLUX.2 Klein 4BはApache-2.0のexact load IDを使う。FLUX.2 Klein 9B系は非商用制約があるため
owner/legal承認まで候補へ入れない。Qwen Image 2512 / Edit 2511は公式BF16 sizeが約57.7 GBで、
RTX 5090 32 GBへ素のまま収まる候補ではない。quality tierもCPU offload、実VRAM、速度、出力品質の
probeが必須である。画像編集はtyped service/fake adapterまでの契約部分実装で、安全なsource ingestion、
実adapter、slash/Discord surfaceは未接続である。

- [OpenAI GPT Image 2](https://developers.openai.com/api/docs/models/gpt-image-2)
- [Gemini image generation](https://ai.google.dev/gemini-api/docs/image-generation)
- [Gemini API deprecations](https://ai.google.dev/gemini-api/docs/deprecations)
- [FLUX.2 collection](https://huggingface.co/collections/black-forest-labs/flux2)
- [Qwen Image 2512](https://huggingface.co/Qwen/Qwen-Image-2512)
- [Qwen Image Edit 2511](https://huggingface.co/Qwen/Qwen-Image-Edit-2511)

## Video generation

| tier | API候補 | Local候補 |
|---|---|---|
| quality | `veo-3.1-generate-preview`（4K / 8秒候補） | `Lightricks/LTX-2.3-fp8` |
| balanced | `veo-3.1-fast-generate-preview`（720p / 6秒候補） | `Wan-AI/Wan2.2-TI2V-5B` |
| fast | `veo-3.1-lite-generate-preview`（720p / 4秒候補） | `Wan-AI/Wan2.2-TI2V-5B` |

Preview利用可否は候補の自己申告ではなくowner実行policyで決める。Veoの上記解像度・秒数は2026-07-24時点の
助言constraintで、固定snapshot、実生成時間、課金、契約、地域は未確認である。`gemini-omni-flash-preview`は
owner承認が必要なPreviewの差替え候補として文書にだけ残し、Veoを置換しない。

Wan2.2 TI2V 5Bは公式の24 GB級memory/offload記載を候補根拠にするが、真のlight tierとは断定しない。
LTX-2.3 FP8はquality候補で、RTX 5090上のheadless Windows長時間運用、実速度、最大秒数は未検証である。
公開service前にCommunity Licenseのowner/legal確認が必要である。Sora 2系はAPI終了予定のため新規主力へ追加しない。

- [Gemini Veo video generation](https://ai.google.dev/gemini-api/docs/veo)
- [Gemini Omni](https://ai.google.dev/gemini-api/docs/omni)
- [Wan2.2 official model card](https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B)
- [Wan2.2 repository](https://github.com/Wan-Video/Wan2.2)
- [LTX Desktop](https://github.com/Lightricks/LTX-Desktop)
- [LTX-2 official repository](https://github.com/Lightricks/LTX-2)
- [LTX-2 Community License](https://huggingface.co/Lightricks/LTX-2/blob/main/LICENSE)

sidecarのAPI/local動画候補はすべて`active:false` / `probe_required:true`である。active
`media.video.generate` routeの3 tierは空のままで、Stage 1はStatic fake provider以外をbindしない。

## Music generation

| tier | API候補 | Local候補 |
|---|---|---|
| quality | `lyria-3-pro-preview` | `ACE-Step/acestep-v15-xl-sft` + `ACE-Step/acestep-5Hz-lm-4B` |
| balanced | ElevenLabs `music_v2` | `ACE-Step/acestep-v15-xl-turbo` + bundle内1.7B LM profile |
| fast | `lyria-3-clip-preview` | `ACE-Step/Ace-Step1.5` bundle内turbo + 0.6B LM |

Lyria 3系はPreviewであり、owner policy、地域・課金・rights/terms、出力contractの確認前にactive化しない。
公式ページ間のsample rate表現に差があるため値を断定せずprobeする。Eleven `music_v2`は現行公式で
MP3とWAVを選択できる。Stage 1のstrict PCM16 WAVへはcontainer/PCM contractをprobeするまで接続しない。

ACE-Stepはbundle内subfolderと独立repoを混同しない。fastの`acestep-v15-turbo`とbalancedの
`acestep-5Hz-lm-1.7B`は`ACE-Step/Ace-Step1.5`内profileであり、架空の単独load IDにしない。
sidecarの`provider_model`はgeneratorとlanguage model/subfolderを結合したexact composition IDであり、
昇格時にbundle rootだけをload IDとして扱わない。
revision固定の隔離workerで読み、任意remote codeをBOT processへ読み込まない。実速度、品質、voice混入、
code/model/output termsをlive probeとowner/legal reviewまでready証拠にしない。全候補は
`active:false` / `probe_required:true`で、active `media.music.generate` routeは空のままである。

- [Gemini music generation](https://ai.google.dev/gemini-api/docs/music-generation)
- [Eleven Music compose API](https://elevenlabs.io/docs/api-reference/music/compose)
- [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)
- [ACE-Step GPU compatibility](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/GPU_COMPATIBILITY.md)

## Speech and retrieval

| capability | quality | balanced | fast |
|---|---|---|---|
| TTS API | `eleven_v3` | `gemini-3.1-flash-tts-preview` | `eleven_flash_v2_5` |
| TTS local | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 同1.7B | `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` |
| STT API | `gpt-4o-transcribe-diarize` | Eleven `scribe_v2` | `gpt-4o-mini-transcribe` |
| STT local | `Qwen/Qwen3-ASR-1.7B-hf` | 同1.7B | `Qwen/Qwen3-ASR-0.6B-hf` |
| Embedding API | `gemini-embedding-2` 3072d | `gemini-embedding-2` 1536d | `gemini-embedding-2` 768d |
| Embedding local | `Qwen/Qwen3-Embedding-8B` | `Qwen/Qwen3-Embedding-4B` | `Qwen/Qwen3-Embedding-0.6B` |
| Rerank API | `rerank-v4.0-pro` | `rerank-v4.0-pro` | `rerank-v4.0-fast` |
| Rerank local | `Qwen/Qwen3-Reranker-8B` | `Qwen/Qwen3-Reranker-4B` | `Qwen/Qwen3-Reranker-0.6B` |

Gemini TTSはPreviewで、公式exampleのraw PCM profileが現Stage 1の44.1/48 kHz WAV契約と異なるため
adapter/変換を未接続にする。`gpt-4o-mini-tts`はdeprecatedのため推奨から外す。Local TTSの
CustomVoiceは固定standard voice候補だけである。VoiceDesignは声設計用の別capability、Baseはvoice clone用の
別profileとし、本人の明示同意、identity binding、監査、公開人物のなりすまし禁止が揃うまで通常TTSへ混ぜない。

STT sidecarはfile Artifact profileだけを扱う。`scribe_v2_realtime`と非HF Qwen streaming/vLLM profileは
realtime用の別capability候補で、file routeへ混ぜない。`gpt-4o-transcribe`も有効な差替え候補だが、
今回の3 tier正本はmini / scribe / diarizeである。Qwen3 ASR HF候補はTransformers 5.13.0以上と
dependency/model revision固定をprobe profileへ含める。

- [ElevenLabs models](https://elevenlabs.io/docs/overview/models)
- [Gemini 3.1 Flash TTS Preview](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview)
- [Gemini speech generation](https://ai.google.dev/gemini-api/docs/speech-generation)
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- [OpenAI speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text)
- [ElevenLabs file speech-to-text](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)
- [Qwen3-ASR 0.6B Transformers](https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf)
- [Qwen3-ASR 1.7B Transformers](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf)
- [Qwen3 Embedding](https://github.com/QwenLM/Qwen3-Embedding)
- [Cohere Rerank](https://docs.cohere.com/v2/docs/rerank)

## RTX 5090 resource policy

1. jobがGPU leaseを取得する。
2. 現在モデルをidle unloadし、CUDA cacheを解放する。
3. 実空きVRAM/RAMをprobeする。
4. 対象tierをon-demand loadし、health probeを通す。
5. job実行後、idle timeoutでunloadする。
6. OOMはcontext・解像度・batchを一段下げて1回だけ再試行する。
7. 再失敗時は下位local tierへ移り、同意なしAPI fallbackは行わない。

モデルカードに5090実測がないものは`probe_required=true`とし、実測値をmanifestへ記録するまでreadyにしない。
embedding次元を変更する場合は既存vector indexを再利用せずreindexする。

## Web browser boundary

`web.search`と`web.browser.isolated`を分離する。後者はephemeral Chromium内の
`navigate/click/type/select/scroll/wait/screenshot/extract`だけを許可し、PC、desktop、shell、host filesystem、
clipboard、credential store、任意process操作はprovider contractに定義しない。

| capability/locality | quality | balanced | fast |
|---|---|---|---|
| Web検索 API | Sol + official web search | Terra + official web search | Luna + official web search（極小だけ） |
| Web検索 local | SearXNG + Qwen3.6 27B | SearXNG + Qwen3.5 9B | SearXNG + Qwen3.5 4B |
| Browser plan API | Sol | Terra | Luna（単純なread-only操作だけ） |
| Browser plan local | Qwen3.6 27B | Qwen3.5 9B | Qwen3.5 4B |

Browser planのモデルがAPIでもlocalでも、実操作は別のephemeral browser workerへ型付きstepとして渡す。
API/LocalいずれのモデルにもPC、desktop、shell、host filesystem操作を与えない。
