from __future__ import annotations

import pytest

from yonerai_discord.ai_control import RiskLevel, TaskComplexity, TaskKind
from yonerai_discord.modules.ai.task_routing import (
    AIExecutionMode,
    AIIntent,
    AIModelTool,
    AITaskRoute,
    RetrievalSource,
    classify_ai_task,
    parse_media_inspection_request,
    requests_multi_step_execution,
)


def test_normal_chat_stays_on_terra_class_route_without_progress() -> None:
    route = classify_ai_task("おはよう")

    assert route == AITaskRoute(
        kind=TaskKind.GENERAL,
        complexity=TaskComplexity.STANDARD,
        risk=RiskLevel.NORMAL,
        uses_tools=False,
        web_search=False,
        expects_artifact=False,
        show_progress=False,
    )


@pytest.mark.parametrize(
    "prompt",
    (
        "混沌ブギを流して、そのあと千本桜を流して",
        "混沌ブギを流して、それから千本桜を流して",
        "混沌ブギを流して、次に千本桜を流して",
        "混沌ブギを流して、続けて千本桜を流して",
        "混沌ブギを流して then 千本桜を流して",
        "混沌ブギを流して,then 千本桜を流して",
        "混沌ブギを流してそのあと千本桜を流して",
    ),
)
def test_ordered_multi_step_request_uses_task_route_without_model_tools(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.intent is AIIntent.MUSIC
    assert route.complexity is TaskComplexity.COMPLEX
    assert route.execution_mode is AIExecutionMode.TASK
    assert route.allowed_model_tools == ()
    assert route.budget.max_tool_calls == 0
    assert "multi_step_requested" in route.reason_codes


def test_english_imperative_sequence_remains_a_task_request() -> None:
    route = classify_ai_task("play Chaos Boogie, then play Senbonzakura")

    assert route.complexity is TaskComplexity.COMPLEX
    assert route.execution_mode is AIExecutionMode.TASK
    assert route.allowed_model_tools == ()
    assert "multi_step_requested" in route.reason_codes


@pytest.mark.parametrize(
    "prompt",
    (
        "ブラウザを開いて、そのあとページを閉じて",
        "ファイルを読んで、それから結果を送って",
    ),
)
def test_delimited_generic_japanese_sequence_remains_multi_step(prompt: str) -> None:
    assert requests_multi_step_execution(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    (
        "混沌ブギを２かい流して、そのあと千本桜を３回流して",
        "混沌ブギを2回流して then 千本桜を3 times流して",
        "混沌ブギを2回流して after that 千本桜を3 times流して",
        "混沌ブギを2回流して next 千本桜を3 times流して",
    ),
)
def test_nfkc_and_supported_ordered_repetition_words_use_task_route(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.intent is AIIntent.MUSIC
    assert route.complexity is TaskComplexity.COMPLEX
    assert route.execution_mode is AIExecutionMode.TASK
    assert route.repetition_requested is True
    assert route.allowed_model_tools == ()
    assert route.budget.max_tool_calls == 0
    assert "repetition_requested" in route.reason_codes


@pytest.mark.parametrize(
    "prompt",
    (
        "それから何があったの？",
        "それからNASAについて教えて",
        "NASAについて教えて、それから？",
        "what happened then?",
    ),
)
def test_edge_sequence_word_does_not_force_task_route(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.complexity is TaskComplexity.STANDARD
    assert route.execution_mode is AIExecutionMode.DIRECT
    assert "multi_step_requested" not in route.reason_codes


@pytest.mark.parametrize(
    "prompt",
    (
        "曲Thenを流して",
        "Better Then Everを流して",
        "次に会いましょうを流して",
        "曲次に会いましょうを流して",
    ),
)
def test_sequence_word_inside_music_title_does_not_force_task_route(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.intent is AIIntent.MUSIC
    assert route.execution_mode is AIExecutionMode.DIRECT
    assert "multi_step_requested" not in route.reason_codes


@pytest.mark.parametrize(
    "prompt",
    (
        "おはよう",
        "雑談しよう",
        "音楽機能を説明して",
        "BANとは？",
        "「設定を変更して」という文章を英語に翻訳して",
        "PCを操作して、とはどういう意味？",
    ),
)
def test_ordinary_conversation_or_explanation_never_becomes_unknown(prompt: str) -> None:
    assert classify_ai_task(prompt).intent is not AIIntent.UNKNOWN


@pytest.mark.parametrize(
    "prompt",
    ("それを止めて", "設定を変更して", "音楽を停止して再開して", "PCを操作して"),
)
def test_ambiguous_or_unregistered_operations_fail_closed_as_unknown(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.intent is AIIntent.UNKNOWN
    assert route.retrieval_sources == ()
    assert route.allowed_model_tools == ()
    assert route.budget.max_tool_calls == 0
    assert route.uses_tools is False


def test_non_web_generic_tool_request_is_unknown_not_an_implicit_allowlist() -> None:
    route = classify_ai_task("この操作を実行", uses_tools=True)
    assert route.intent is AIIntent.UNKNOWN
    assert route.allowed_model_tools == ()


def test_recognized_unknown_operation_cannot_be_upgraded_by_web_search_flag() -> None:
    route = classify_ai_task("PCを操作して Web検索して", web_search=True)

    assert route.intent is AIIntent.UNKNOWN
    assert route.web_search is False
    assert route.uses_tools is False
    assert route.allowed_model_tools == ()
    assert route.retrieval_sources == ()


@pytest.mark.parametrize(
    "prompt",
    (
        "yonerai.com開いて4Kスクショして",
        "ｙｏｎｅｒａｉ．ｃｏｍを開いて４Ｋスクショして",
        "yonerai.comをスクリーンショット撮って",
        "yonerai.comの画面をスクショ撮って",
        "https://yonerai.com を開いてスクリーンショットを撮って",
        "OPEN example.com AND TAKE A SCREENSHOT",
    ),
)
def test_unmatched_browser_execution_never_falls_through_to_text_provider(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.intent is AIIntent.UNKNOWN
    assert route.reason_codes == ("browser_operation_unavailable",)
    assert route.retrieval_sources == ()
    assert route.allowed_model_tools == ()
    assert route.budget.max_tool_calls == 0


@pytest.mark.parametrize(
    "prompt",
    (
        "https://youtube.com/shorts/TG9KgEss-TE これはどういうの？字幕データや画像認識で把握して",
        "https://youtube.com/shorts/TG9KgEss-TE これ何？",
        "https://youtu.be/TG9KgEss-TE この動画を説明して",
        "https://www.youtube.com/watch?v=TG9KgEss-TE",
        "https://example.com/movie.mp4 の字幕データと代表フレームで分析して",
        "https://video.example/watch/123 を文字起こしと画像認識で要約して",
    ),
)
def test_unmatched_media_inspection_never_falls_through_to_text_provider(prompt: str) -> None:
    route = classify_ai_task(prompt)

    assert route.intent is AIIntent.UNKNOWN
    assert route.reason_codes == ("media_inspection_unavailable",)
    assert route.retrieval_sources == ()
    assert route.allowed_model_tools == ()
    assert route.budget.max_tool_calls == 0


@pytest.mark.parametrize(
    "prompt",
    (
        "https://youtu.be/TG9KgEss-TE を再生して",
        "https://youtube.com/shorts/TG9KgEss-TE を10回リピートして",
        "https://www.youtube.com/watch?v=TG9KgEss-TE をキューに追加して",
    ),
)
def test_youtube_playback_requests_are_not_misclassified_as_media_inspection(prompt: str) -> None:
    assert parse_media_inspection_request(prompt) is None


@pytest.mark.parametrize(
    "prompt",
    (
        "https://youtu.be/TG9KgEss-TE この動画を再生せず説明して",
        "https://youtu.be/TG9KgEss-TE 再生回数は何？",
        "https://youtu.be/TG9KgEss-TE ループ動画の内容を説明して",
        "https://youtu.be/TG9KgEss-TE これは自動再生？",
        "https://youtu.be/TG9KgEss-TE これは何のプレイリスト？",
        "https://youtu.be/TG9KgEss-TE これは無限リピート？",
    ),
)
def test_youtube_content_questions_with_playback_words_still_use_media_inspection(prompt: str) -> None:
    assert parse_media_inspection_request(prompt) is not None
    assert classify_ai_task(prompt).reason_codes == ("media_inspection_unavailable",)


@pytest.mark.parametrize(
    "prompt",
    (
        "4Kスクショとは？",
        "「yonerai.comを開いてスクショして」という文章を説明して",
        "yonerai.comについて教えて",
        "example.com の screenshot APIについて教えて",
        "what is the screenshot API on example.com?",
        "Web検索して example.com screenshot APIを教えて",
        "https://example.com の字幕APIについて教えて",
        "動画の文字起こしとは？",
    ),
)
def test_browser_words_in_explanations_do_not_trigger_execution_rejection(prompt: str) -> None:
    assert classify_ai_task(prompt).intent is not AIIntent.UNKNOWN


def test_code_generation_uses_complex_sol_route_and_progress() -> None:
    route = classify_ai_task("Discord BOTのコードを書いて実装して")

    assert route.kind is TaskKind.CODE_GENERATION
    assert route.complexity is TaskComplexity.COMPLEX
    assert route.show_progress is True
    assert route.expects_artifact is True


def test_web_search_is_a_tool_route_even_for_short_prompt() -> None:
    route = classify_ai_task("調べて", web_search=True)

    assert route.kind is TaskKind.GENERAL
    assert route.complexity is TaskComplexity.COMPLEX
    assert route.uses_tools is True
    assert route.web_search is True
    assert route.show_progress is True
    assert route.intent is AIIntent.WEB_RESEARCH
    assert route.execution_mode is AIExecutionMode.TASK
    assert route.allowed_model_tools == (AIModelTool.WEB_SEARCH,)
    assert route.retrieval_sources == (RetrievalSource.PERSONAL_MEMORY, RetrievalSource.WEB)
    assert route.budget.max_tool_calls == 1


def test_attachment_raises_complexity_without_changing_task_kind() -> None:
    route = classify_ai_task("これ見て", attachment_count=1)

    assert route.kind is TaskKind.GENERAL
    assert route.complexity is TaskComplexity.COMPLEX
    assert route.show_progress is True


def test_self_evolution_has_a_dedicated_quality_route() -> None:
    route = classify_ai_task("自己進化の設計を見直して")

    assert route.kind is TaskKind.SELF_EVOLUTION
    assert route.complexity is TaskComplexity.COMPLEX
    assert route.intent is AIIntent.SELF_EVOLUTION
    assert route.execution_mode is AIExecutionMode.TASK


def test_moderation_never_receives_ambient_memory_or_model_tools() -> None:
    route = classify_ai_task("この人をBANして")

    assert route.intent is AIIntent.MODERATION
    assert route.risk is RiskLevel.HIGH
    assert route.retrieval_sources == ()
    assert route.allowed_model_tools == ()
    assert route.budget.max_tool_calls == 0


def test_site_build_is_task_but_publisher_is_not_exposed_as_a_model_tool() -> None:
    route = classify_ai_task("ポートフォリオサイトを作って")

    assert route.intent is AIIntent.SITE
    assert route.execution_mode is AIExecutionMode.TASK
    assert route.expects_artifact is True
    assert route.allowed_model_tools == ()


def test_only_explicit_tiny_formatting_reaches_luna_route() -> None:
    route = classify_ai_task("次の文を箇条書きにして: 赤 青")

    assert route.kind is TaskKind.FORMATTING
    assert route.complexity is TaskComplexity.TINY
    assert route.risk is RiskLevel.LOW
    assert route.show_progress is False


@pytest.mark.parametrize("attachment_count", [-1, True, 1.5])
def test_invalid_attachment_count_is_rejected(attachment_count: object) -> None:
    with pytest.raises(ValueError):
        classify_ai_task("test", attachment_count=attachment_count)  # type: ignore[arg-type]
