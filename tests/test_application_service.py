import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from lenkobot.action_confirmation import (
    ActionConfirmationService,
    SQLiteActionConfirmationStore,
)
from lenkobot.application_service import TelegramApplicationService
from lenkobot.context_builder import ContextBuilder
from lenkobot.memory import MemoryScope, NewMemory, SQLiteMemoryStore
from lenkobot.personas import PersonaCatalog
from lenkobot.session_store import (
    FailureStage,
    SQLiteSessionFinalizer,
    SQLiteSessionStore,
)
from lenkobot.sqlite_schema import open_state_database
from lenkobot.telegram_presentation import (
    TelegramInlineButton,
    TelegramParseMode,
    TelegramResponseKind,
    TelegramSentMessage,
    confirmation_callback_data,
    parse_confirmation_callback_data,
    parse_forget_callback_data,
    parse_memories_page_callback_data,
    render_command_index,
)
from lenkobot.telegram_router import (
    IncomingTelegramCallback,
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)
from lenkobot.web_search import SearchResult, ToolLoopResult
from lenkobot.xai_provider import ProviderRequestError, XaiTextResponse


class RecordingResponsePort:
    def __init__(self):
        self.responses = []

    async def send(self, response):
        self.responses.append(response)


class EditableRecordingPort(RecordingResponsePort):
    def __init__(self, *, edit_result=True, bound=None):
        super().__init__()
        self.edits = []
        self.edit_result = edit_result
        self.bound = bound

    async def send(self, response):
        await super().send(response)
        return TelegramSentMessage(
            chat_id=response.chat_id,
            message_id=len(self.responses),
        )

    async def edit(self, handle, response):
        self.edits.append((handle, response))
        return self.edit_result

    def bound_handle(self):
        return self.bound


class SourcesFailingPort(EditableRecordingPort):
    async def send(self, response):
        if response.parse_mode is TelegramParseMode.HTML:
            raise RuntimeError("source delivery failed")
        return await super().send(response)


class MutableClock:
    def __init__(self):
        self.now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def confirmation_token(response, *, action="confirm"):
    for row in response.inline_keyboard:
        for button in row:
            parsed = parse_confirmation_callback_data(button.callback_data)
            if parsed is not None and parsed[0] == action:
                return parsed[1]
    raise AssertionError(f"{action} button not found in response keyboard")


def telegram_callback(data, *, user_id=42, chat_id=500, chat_type="private"):
    return IncomingTelegramCallback(
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        data=data,
    )


class RecordingProvider:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.prompts = []

    def respond(self, prompt):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingToolLoop:
    def __init__(self, result, *, query="курс доллара сегодня"):
        self.result = result
        self.query = query
        self.prompts = []

    async def respond(self, prompt, *, on_search_start=None):
        self.prompts.append(prompt)
        if on_search_start is not None:
            await on_search_start(self.query)
        return self.result


class FailingFinalResponsePort(RecordingResponsePort):
    async def send(self, response):
        if response.kind is TelegramResponseKind.FINAL:
            raise RuntimeError("telegram transport details")
        await super().send(response)


class FailingSessionStore:
    def begin_user_turn(self, **kwargs):
        raise RuntimeError("database-secret")


class FailingExtractionStore:
    def ensure_extraction_run(self, **kwargs):
        raise RuntimeError("memory-secret")


class FixedSummaryGenerator:
    def __init__(self):
        self.calls = 0

    def generate(self, *, turns):
        self.calls += 1
        return "A bounded summary"


def build_catalog(tmp_path):
    config_path = tmp_path / "personas.toml"
    config_path.write_text(
        """
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "A calm companion."
        identity_version = 1

        [[personas]]
        key = "analyst"
        display_name = "Analyst"
        identity_prompt = "A precise analyst."
        identity_version = 1
        """,
        encoding="utf-8",
    )
    return PersonaCatalog.from_toml(config_path)


def build_service(
    tmp_path,
    provider,
    response_port,
    *,
    context_builder=None,
    memory_store=None,
    session_store=None,
    extraction_service=None,
    session_finalizer=None,
    confirmation_store="auto",
    confirmation_clock=None,
    tool_loop=None,
):
    catalog = build_catalog(tmp_path)
    router = TelegramRouter(
        allowed_user_id=42,
        store=SQLiteConversationStore(tmp_path / "state.db"),
        reply_port=RecordingResponsePort(),
        persona_catalog=catalog,
    )
    if confirmation_store == "auto":
        confirmation_store = ActionConfirmationService(
            SQLiteActionConfirmationStore(
                tmp_path / "state.db",
                clock=confirmation_clock,
            )
        )
    return TelegramApplicationService(
        router=router,
        persona_catalog=catalog,
        provider=provider,
        response_port=response_port,
        context_builder=context_builder,
        memory_store=memory_store,
        session_store=session_store,
        extraction_service=extraction_service,
        session_finalizer=session_finalizer,
        confirmation_service=confirmation_store,
        tool_loop=tool_loop,
    ), router


def telegram_message(text, *, user_id=42, chat_id=500, chat_type="private"):
    return IncomingTelegramMessage(
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        text=text,
    )


def test_text_turn_uses_active_persona_and_presents_status_then_final(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Hello from the companion",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, _ = build_service(tmp_path, provider, response_port)

    result = asyncio.run(service.handle(telegram_message("Tell me something")))

    assert result.text == "Hello from the companion"
    assert provider.prompts == [
        "A calm companion.\n\nUser message:\nTell me something"
    ]
    assert [(item.kind, item.text) for item in response_port.responses] == [
        (TelegramResponseKind.STATUS, "Готовлю ответ"),
        (TelegramResponseKind.FINAL, "Hello from the companion"),
    ]


def test_web_search_edits_status_and_sends_linked_sources(tmp_path):
    provider = RecordingProvider(error=AssertionError("plain provider called"))
    source = SearchResult(
        title="ЦБ РФ & курсы",
        url="https://cbr.ru/currency_base/daily/?a=1&b=2",
        snippet="USD 80",
    )
    tool_loop = RecordingToolLoop(
        ToolLoopResult(
            response=XaiTextResponse(
                response_id="resp-search",
                model="grok-4.5",
                text="сейчас доллар стоит 80 рублей",
                credential_source="xai_oauth",
            ),
            sources=(source,),
        )
    )
    response_port = EditableRecordingPort()
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        tool_loop=tool_loop,
    )

    result = asyncio.run(service.handle(telegram_message("почём доллар?")))

    assert result.text == "сейчас доллар стоит 80 рублей"
    assert provider.prompts == []
    assert tool_loop.prompts == [
        "A calm companion.\n\nUser message:\nпочём доллар?"
    ]
    assert response_port.responses[0].kind is TelegramResponseKind.STATUS
    assert response_port.edits[0][1].kind is TelegramResponseKind.STATUS
    assert response_port.edits[0][1].text == "ищу: «курс доллара сегодня»"
    assert response_port.edits[1][1].kind is TelegramResponseKind.FINAL
    assert response_port.edits[1][1].text == "сейчас доллар стоит 80 рублей"
    sources_response = response_port.responses[1]
    assert sources_response.kind is TelegramResponseKind.NOTICE
    assert sources_response.parse_mode is TelegramParseMode.HTML
    assert sources_response.text == (
        '<b>Источники:</b>\n'
        '1. <a href="https://cbr.ru/currency_base/daily/?a=1&amp;b=2">'
        "ЦБ РФ &amp; курсы</a>"
    )


def test_source_delivery_failure_does_not_lose_final_answer(tmp_path):
    response = XaiTextResponse(
        response_id="resp-search",
        model="grok-4.5",
        text="answer survives",
        credential_source="xai_oauth",
    )
    tool_loop = RecordingToolLoop(
        ToolLoopResult(
            response=response,
            sources=(SearchResult("Source", "https://example.com", "data"),),
        )
    )
    response_port = SourcesFailingPort()
    service, _ = build_service(
        tmp_path,
        RecordingProvider(error=AssertionError("plain provider called")),
        response_port,
        tool_loop=tool_loop,
    )

    result = asyncio.run(service.handle(telegram_message("search this")))

    assert result is response
    assert response_port.edits[-1][1].text == "answer survives"


def test_provider_fallback_is_presented_as_an_expense_notice(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Paid answer",
            credential_source="xai_api_key",
            fallback_from="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, _ = build_service(tmp_path, provider, response_port)

    asyncio.run(service.handle(telegram_message("Continue")))

    assert [item.kind for item in response_port.responses] == [
        TelegramResponseKind.STATUS,
        TelegramResponseKind.NOTICE,
        TelegramResponseKind.FINAL,
    ]
    assert "API key" in response_port.responses[1].text
    assert "расход" in response_port.responses[1].text


def test_provider_failure_returns_safe_error_without_internal_details(tmp_path):
    provider = RecordingProvider(
        error=ProviderRequestError(
            "request failed",
            status=500,
            code="server_error",
            raw_body='{"token":"do-not-show"}',
            headers={},
        )
    )
    response_port = RecordingResponsePort()
    service, _ = build_service(tmp_path, provider, response_port)

    result = asyncio.run(service.handle(telegram_message("Break")))

    assert result is None
    assert response_port.responses[0].kind is TelegramResponseKind.STATUS
    assert response_port.responses[-1].kind is TelegramResponseKind.ERROR
    assert response_port.responses[-1].text == "Не удалось подготовить ответ. Попробуйте ещё раз."
    assert "do-not-show" not in response_port.responses[-1].text
    assert "server_error" not in response_port.responses[-1].text


def test_provider_failure_keeps_user_turn_and_redacted_failure_record(tmp_path):
    database_path = tmp_path / "state.db"
    provider = RecordingProvider(
        error=ProviderRequestError(
            "request failed",
            status=500,
            code="server_error",
            raw_body='{"token":"do-not-store"}',
            headers={},
        )
    )
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(database_path)
    session_store = SQLiteSessionStore(database_path)
    service, router = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=memory_store,
        session_store=session_store,
    )

    result = asyncio.run(service.handle(telegram_message("Persist me")))
    lane = router.route(telegram_message("inspect"))
    active = session_store.ensure_active_session(
        user_id=42,
        persona_session_id=lane.session_id,
    )

    assert result is None
    assert [turn.content for turn in session_store.list_turns(session_id=active.id, user_id=42)] == [
        "Persist me"
    ]
    assert [
        (failure.stage, failure.error_kind)
        for failure in session_store.list_failures(session_id=active.id, user_id=42)
    ] == [(FailureStage.PROVIDER, "provider_request_failed")]
    assert memory_store.list_extraction_runs(
        owner_user_id=42,
        session_id=active.id,
    ) == ()


def test_transcript_persistence_failure_is_safe_and_skips_provider(tmp_path):
    provider = RecordingProvider()
    response_port = RecordingResponsePort()
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        session_store=FailingSessionStore(),
    )

    result = asyncio.run(service.handle(telegram_message("Do not lose me")))

    assert result is None
    assert provider.prompts == []
    assert [(response.kind, response.text) for response in response_port.responses] == [
        (
            TelegramResponseKind.ERROR,
            "Не удалось сохранить сообщение. Попробуйте ещё раз.",
        )
    ]
    assert "database-secret" not in response_port.responses[0].text


def test_delivery_failure_keeps_assistant_turn_and_separate_failure(tmp_path):
    database_path = tmp_path / "state.db"
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Durable answer",
            credential_source="xai_oauth",
        )
    )
    response_port = FailingFinalResponsePort()
    memory_store = SQLiteMemoryStore(database_path)
    session_store = SQLiteSessionStore(database_path)
    service, router = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=memory_store,
        session_store=session_store,
    )

    with pytest.raises(RuntimeError, match="telegram transport"):
        asyncio.run(service.handle(telegram_message("Question")))
    lane = router.route(telegram_message("inspect"))
    active = session_store.ensure_active_session(
        user_id=42,
        persona_session_id=lane.session_id,
    )

    turns = session_store.list_turns(session_id=active.id, user_id=42)
    failures = session_store.list_failures(session_id=active.id, user_id=42)
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "Question"),
        ("assistant", "Durable answer"),
    ]
    assert [(failure.stage, failure.error_kind) for failure in failures] == [
        (FailureStage.DELIVERY, "telegram_delivery_failed")
    ]
    runs = memory_store.list_extraction_runs(
        owner_user_id=42,
        session_id=active.id,
    )
    assert len(runs) == 1
    assert runs[0].source_turn_id == turns[0].id
    assert runs[0].status.value == "pending"


def test_extraction_run_failure_keeps_assistant_turn_and_returns_safe_error(tmp_path):
    database_path = tmp_path / "state.db"
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Durable answer",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    session_store = SQLiteSessionStore(database_path)
    service, router = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=FailingExtractionStore(),
        session_store=session_store,
    )

    result = asyncio.run(service.handle(telegram_message("Persist exchange")))
    lane = router.route(telegram_message("inspect"))
    active = session_store.ensure_active_session(
        user_id=42,
        persona_session_id=lane.session_id,
    )

    assert result is None
    assert [turn.content for turn in session_store.list_turns(
        session_id=active.id,
        user_id=42,
    )] == ["Persist exchange", "Durable answer"]
    assert [(item.kind, item.text) for item in response_port.responses] == [
        (TelegramResponseKind.STATUS, "Готовлю ответ"),
        (
            TelegramResponseKind.ERROR,
            "Не удалось сохранить состояние памяти. Попробуйте ещё раз.",
        ),
    ]
    assert "memory-secret" not in response_port.responses[-1].text


def test_persona_command_switches_lane_without_provider_call(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Analyst answer",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, router = build_service(tmp_path, provider, response_port)

    command_result = asyncio.run(service.handle(telegram_message("/persona analyst")))
    text_result = asyncio.run(service.handle(telegram_message("Analyze this")))

    assert command_result is None
    assert text_result.text == "Analyst answer"
    assert len(provider.prompts) == 1
    assert provider.prompts[0] == "A precise analyst.\n\nUser message:\nAnalyze this"
    assert response_port.responses[0].kind is TelegramResponseKind.FINAL
    assert response_port.responses[0].text == "Персона переключена: Analyst."
    assert router.route(telegram_message("check")).persona_key == "analyst"


def test_persona_command_without_key_returns_display_name_picker_without_provider_call(
    tmp_path,
):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, _ = build_service(tmp_path, provider, response_port)

    result = asyncio.run(service.handle(telegram_message("/persona")))

    assert result is None
    assert provider.prompts == []
    assert response_port.responses[0].kind is TelegramResponseKind.FINAL
    assert response_port.responses[0].text == "Выбери персону: Companion, Analyst."
    assert response_port.responses[0].inline_keyboard == (
        (
            TelegramInlineButton(
                text="✓ Companion",
                callback_data="persona:v1:companion",
            ),
        ),
        (
            TelegramInlineButton(
                text="Analyst",
                callback_data="persona:v1:analyst",
            ),
        ),
    )


def test_persona_callback_switches_lane_without_provider_call(tmp_path):
    provider = RecordingProvider()
    response_port = RecordingResponsePort()
    service, router = build_service(tmp_path, provider, response_port)

    result = asyncio.run(
        service.handle_callback(
            IncomingTelegramCallback(
                user_id=42,
                chat_id=500,
                chat_type="private",
                data="persona:v1:analyst",
            )
        )
    )

    assert result is None
    assert provider.prompts == []
    assert response_port.responses[0].text == "Персона переключена: Analyst."
    assert router.route(telegram_message("check")).persona_key == "analyst"


def test_repeated_persona_callback_is_idempotent_for_active_version(tmp_path):
    provider = RecordingProvider()
    response_port = RecordingResponsePort()
    service, router = build_service(tmp_path, provider, response_port)
    callback = IncomingTelegramCallback(
        user_id=42,
        chat_id=500,
        chat_type="private",
        data="persona:v1:analyst",
    )

    asyncio.run(service.handle_callback(callback))
    lane_after_first = router.current_lane(telegram_message("inspect"))
    asyncio.run(service.handle_callback(callback))
    lane_after_second = router.current_lane(telegram_message("inspect"))

    assert lane_after_first.session_id == lane_after_second.session_id
    assert response_port.responses[1].text == "Персона переключена: Analyst."


def test_unauthorized_or_malformed_persona_callback_has_no_state_or_provider_side_effect(
    tmp_path,
):
    provider = RecordingProvider()
    response_port = RecordingResponsePort()
    service, router = build_service(tmp_path, provider, response_port)

    asyncio.run(
        service.handle_callback(
            IncomingTelegramCallback(
                user_id=99,
                chat_id=500,
                chat_type="private",
                data="persona:v1:analyst",
            )
        )
    )
    asyncio.run(
        service.handle_callback(
            IncomingTelegramCallback(
                user_id=42,
                chat_id=500,
                chat_type="group",
                data="persona:v1:analyst",
            )
        )
    )
    asyncio.run(
        service.handle_callback(
            IncomingTelegramCallback(
                user_id=42,
                chat_id=500,
                chat_type="private",
                data="persona:v2:analyst",
            )
        )
    )

    assert provider.prompts == []
    assert response_port.responses[-1].kind is TelegramResponseKind.ERROR
    assert router.current_lane(telegram_message("inspect")) is None


def test_invalid_or_unauthorized_commands_do_not_change_state_or_call_provider(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, router = build_service(tmp_path, provider, response_port)

    asyncio.run(service.handle(telegram_message("/persona unknown")))
    asyncio.run(
        service.handle(
            telegram_message("/persona analyst", user_id=99, chat_id=501)
        )
    )

    assert provider.prompts == []
    assert router.route(telegram_message("still companion")).persona_key == "companion"
    assert [item.kind for item in response_port.responses] == [TelegramResponseKind.ERROR]
    assert "unknown" not in response_port.responses[0].text


@pytest.mark.parametrize("command", ("/start", "/help"))
def test_start_and_help_return_command_index_without_provider_call(tmp_path, command):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, _ = build_service(tmp_path, provider, response_port)

    result = asyncio.run(service.handle(telegram_message(command)))

    assert result is None
    assert provider.prompts == []
    assert response_port.responses[0].kind is TelegramResponseKind.FINAL
    assert "/remember <text>" in response_port.responses[0].text
    assert "/memories [page]" in response_port.responses[0].text
    assert "/forget [id]" in response_port.responses[0].text


def test_help_returns_exact_index_and_start_prepends_voice_greeting(tmp_path):
    response_port = RecordingResponsePort()
    service, _ = build_service(tmp_path, RecordingProvider(), response_port)

    asyncio.run(service.handle(telegram_message("/help")))
    asyncio.run(service.handle(telegram_message("/start")))

    assert response_port.responses[0].text == render_command_index()
    assert response_port.responses[1].text.endswith(render_command_index())
    assert response_port.responses[1].text != render_command_index()


def test_new_closes_current_session_and_next_turn_uses_summary(tmp_path):
    database_path = tmp_path / "state.db"
    memory_store = SQLiteMemoryStore(database_path)
    session_store = SQLiteSessionStore(database_path)
    summary_generator = FixedSummaryGenerator()
    finalizer = SQLiteSessionFinalizer(
        database_path,
        summary_generator,
        extraction_store=memory_store,
    )
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Answer",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, router = build_service(
        tmp_path,
        provider,
        response_port,
        context_builder=ContextBuilder(memory_store, transcript_store=session_store),
        memory_store=memory_store,
        session_store=session_store,
        session_finalizer=finalizer,
    )

    asyncio.run(service.handle(telegram_message("First")))
    lane = router.route(telegram_message("inspect"))
    run = memory_store.list_extraction_runs(
        owner_user_id=42,
        session_id=session_store.ensure_active_session(
            user_id=42,
            persona_session_id=lane.session_id,
        ).id,
    )[0]
    memory_store.claim_extraction_run(run.id, owner_user_id=42)
    memory_store.complete_extraction_run(run.id, owner_user_id=42)

    asyncio.run(service.handle(telegram_message("/new")))
    prompt = response_port.responses[-1]
    assert summary_generator.calls == 0

    asyncio.run(
        service.handle_callback(
            telegram_callback(
                confirmation_callback_data(
                    "confirm",
                    confirmation_token(prompt),
                )
            )
        )
    )
    asyncio.run(service.handle(telegram_message("Next")))

    texts = [response.text for response in response_port.responses]
    assert summary_generator.calls == 1
    assert "Разговор закрыт. Следующее сообщение начнёт новый." in texts
    assert "UNTRUSTED CLOSED SESSION SUMMARY" in provider.prompts[-1]
    assert "A bounded summary" in provider.prompts[-1]
    assert provider.prompts[-1].endswith("User message:\nNext")


def test_memory_commands_create_list_and_delete_only_owned_shared_records(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=memory_store,
    )

    asyncio.run(service.handle(telegram_message("/remember User likes tea")))
    record = memory_store.list_for_user(user_id=42, page=1, page_size=5)[0]
    asyncio.run(service.handle(telegram_message("/memories")))
    assert memory_store.delete(record.id, user_id=99) is False
    asyncio.run(service.handle(telegram_message(f"/forget {record.id}")))

    prompt = response_port.responses[2]
    assert memory_store.get(record.id, user_id=42) is not None
    asyncio.run(
        service.handle_callback(
            telegram_callback(
                confirmation_callback_data("confirm", confirmation_token(prompt))
            )
        )
    )

    assert record.scope is MemoryScope.SHARED
    assert record.kind == "fact"
    assert record.content == "User likes tea"
    assert response_port.responses[0].text == "Запомнил: User likes tea."
    assert "[shared] User likes tea" in response_port.responses[1].text
    assert response_port.responses[3].text == f"Удалено: запись {record.id}."
    assert memory_store.get(record.id, user_id=42) is None
    assert provider.prompts == []


def test_memory_commands_validate_arguments_and_page(tmp_path):
    provider = RecordingProvider()
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=memory_store,
    )

    asyncio.run(service.handle(telegram_message("/remember")))
    asyncio.run(service.handle(telegram_message("/memories 0")))
    asyncio.run(service.handle(telegram_message("/forget nope")))

    assert [response.kind for response in response_port.responses] == [
        TelegramResponseKind.ERROR,
        TelegramResponseKind.ERROR,
        TelegramResponseKind.ERROR,
    ]
    assert response_port.responses[0].text == "Формат команды: /remember <text>."
    assert response_port.responses[1].text == "Номер страницы должен быть положительным."
    assert response_port.responses[2].text == "Формат команды: /forget [id]."
    assert provider.prompts == []


def test_remember_rejects_text_longer_than_500_characters(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )

    asyncio.run(service.handle(telegram_message(f"/remember {'x' * 501}")))

    assert response_port.responses[0].kind is TelegramResponseKind.ERROR
    assert response_port.responses[0].text == "Текст не должен быть длиннее 500 символов."
    assert memory_store.memory_count() == 0


def test_group_text_is_ignored_before_status_or_provider(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, router = build_service(tmp_path, provider, response_port)

    result = asyncio.run(
        service.handle(telegram_message("Hello", chat_id=-500, chat_type="group"))
    )

    assert result is None
    assert provider.prompts == []
    assert response_port.responses == []
    assert router.route(telegram_message("private check")).persona_key == "companion"


def test_unauthorized_input_is_ignored_without_a_response_port(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    catalog = build_catalog(tmp_path)
    router = TelegramRouter(
        allowed_user_id=42,
        store=SQLiteConversationStore(tmp_path / "state.db"),
        reply_port=RecordingResponsePort(),
        persona_catalog=catalog,
    )
    service = TelegramApplicationService(
        router=router,
        persona_catalog=catalog,
        provider=provider,
    )

    result = asyncio.run(
        service.handle(telegram_message("hello", user_id=99, chat_id=501))
    )

    assert result is None
    assert provider.prompts == []


def test_application_service_builds_provider_prompt_with_scoped_memory(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Remembered answer",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    catalog = build_catalog(tmp_path)
    companion = catalog.get("companion")
    companion_id = memory_store.register_persona(companion)
    memory_store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.PERSONA_PRIVATE,
            persona_id=companion_id,
            kind="preference",
            content="Prefer examples.",
        )
    )
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        context_builder=ContextBuilder(memory_store),
    )

    asyncio.run(service.handle(telegram_message("Explain it")))

    assert len(provider.prompts) == 1
    assert "Prefer examples." in provider.prompts[0]
    assert provider.prompts[0].endswith("User message:\nExplain it")


def test_application_service_builds_next_prompt_from_durable_recent_transcript(tmp_path):
    database_path = tmp_path / "state.db"
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="First durable answer",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(database_path)
    session_store = SQLiteSessionStore(database_path)
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        context_builder=ContextBuilder(
            memory_store,
            transcript_store=session_store,
        ),
        session_store=session_store,
    )

    asyncio.run(service.handle(telegram_message("First durable question")))
    asyncio.run(service.handle(telegram_message("Second question")))

    assert len(provider.prompts) == 2
    assert "UNTRUSTED ACTIVE SESSION TRANSCRIPT" in provider.prompts[1]
    assert "First durable question" in provider.prompts[1]
    assert "First durable answer" in provider.prompts[1]
    assert provider.prompts[1].count("Second question") == 1
    assert provider.prompts[1].endswith("User message:\nSecond question")


def test_completed_exchange_creates_pending_extraction_run_anchored_to_user_turn(
    tmp_path,
):
    database_path = tmp_path / "state.db"
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Durable answer",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(database_path)
    session_store = SQLiteSessionStore(database_path)
    service, router = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=memory_store,
        session_store=session_store,
    )

    asyncio.run(service.handle(telegram_message("Remember this exchange")))
    lane = router.route(telegram_message("inspect"))
    active = session_store.ensure_active_session(
        user_id=42,
        persona_session_id=lane.session_id,
    )
    turns = session_store.list_turns(session_id=active.id, user_id=42)
    runs = memory_store.list_extraction_runs(
        owner_user_id=42,
        session_id=active.id,
    )

    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "Remember this exchange"),
        ("assistant", "Durable answer"),
    ]
    assert len(runs) == 1
    assert runs[0].source_turn_id == turns[0].id
    assert runs[0].status.value == "pending"


def test_unauthorized_input_is_rejected_before_context_builder(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    context_builder = RecordingContextBuilder()
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        context_builder=context_builder,
    )

    result = asyncio.run(
        service.handle(telegram_message("Reveal memory", user_id=99, chat_id=501))
    )

    assert result is None
    assert context_builder.calls == []
    assert provider.prompts == []
    assert response_port.responses == []


def test_unauthorized_input_creates_no_profile_session_or_transcript(tmp_path):
    session_store = SQLiteSessionStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        RecordingResponsePort(),
        session_store=session_store,
    )

    asyncio.run(service.handle(telegram_message("No access", user_id=99, chat_id=501)))

    assert session_store.profile_count() == 0
    assert session_store.session_count() == 0
    assert session_store.turn_count() == 0


def test_context_failure_returns_safe_error_without_calling_provider(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Should not be called",
            credential_source="xai_oauth",
        )
    )
    response_port = RecordingResponsePort()
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        context_builder=FailingContextBuilder(),
    )

    result = asyncio.run(service.handle(telegram_message("Remember")))

    assert result is None
    assert provider.prompts == []
    assert [response.kind for response in response_port.responses] == [
        TelegramResponseKind.STATUS,
        TelegramResponseKind.ERROR,
    ]
    assert "private-memory-secret" not in response_port.responses[-1].text


class FailingContextBuilder:
    def build(self, **kwargs):
        raise RuntimeError("private-memory-secret")


class RecordingContextBuilder:
    def __init__(self):
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        return "prompt"

def test_chat_turn_edits_status_message_into_final(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Long answer",
            credential_source="xai_oauth",
        )
    )
    response_port = EditableRecordingPort()
    service, _ = build_service(tmp_path, provider, response_port)

    result = asyncio.run(service.handle(telegram_message("hello")))

    assert result.text == "Long answer"
    assert [response.kind for response in response_port.responses] == [
        TelegramResponseKind.STATUS
    ]
    assert len(response_port.edits) == 1
    handle, edited = response_port.edits[0]
    assert handle == TelegramSentMessage(chat_id=500, message_id=1)
    assert edited.kind is TelegramResponseKind.FINAL
    assert edited.text == "Long answer"


def test_chat_turn_falls_back_to_new_message_when_edit_fails(tmp_path):
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Long answer",
            credential_source="xai_oauth",
        )
    )
    response_port = EditableRecordingPort(edit_result=False)
    service, _ = build_service(tmp_path, provider, response_port)

    asyncio.run(service.handle(telegram_message("hello")))

    assert [response.kind for response in response_port.responses] == [
        TelegramResponseKind.STATUS,
        TelegramResponseKind.FINAL,
    ]
    assert response_port.responses[-1].text == "Long answer"
    assert len(response_port.edits) == 1


def test_provider_failure_edits_status_into_generic_error(tmp_path):
    provider = RecordingProvider(
        error=ProviderRequestError(
            "request failed",
            status=500,
            code="server_error",
            raw_body='{"token":"raw-provider-secret"}',
            headers={},
        )
    )
    response_port = EditableRecordingPort()
    service, _ = build_service(tmp_path, provider, response_port)

    asyncio.run(service.handle(telegram_message("hello")))

    assert len(response_port.edits) == 1
    _, edited = response_port.edits[0]
    assert edited.kind is TelegramResponseKind.ERROR
    assert "raw-provider-secret" not in edited.text


def test_long_answer_replaces_status_with_first_chunk_and_sends_rest(tmp_path):
    long_text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text=long_text,
            credential_source="xai_oauth",
        )
    )
    response_port = EditableRecordingPort()
    service, _ = build_service(tmp_path, provider, response_port)

    asyncio.run(service.handle(telegram_message("hello")))

    _, edited = response_port.edits[0]
    assert edited.text == "a" * 3000
    assert [response.kind for response in response_port.responses] == [
        TelegramResponseKind.STATUS,
        TelegramResponseKind.FINAL,
    ]
    assert response_port.responses[-1].text == "b" * 3000


def test_new_cancel_keeps_session_active(tmp_path):
    database_path = tmp_path / "state.db"
    session_store = SQLiteSessionStore(database_path)
    finalizer = SQLiteSessionFinalizer(
        database_path,
        FixedSummaryGenerator(),
        extraction_store=SQLiteMemoryStore(database_path),
    )
    response_port = RecordingResponsePort()
    service, router = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        session_store=session_store,
        session_finalizer=finalizer,
    )
    asyncio.run(service.handle(telegram_message("First")))
    lane = router.route(telegram_message("inspect"))

    asyncio.run(service.handle(telegram_message("/new")))
    prompt = response_port.responses[-1]
    asyncio.run(
        service.handle_callback(
            telegram_callback(
                confirmation_callback_data("cancel", confirmation_token(prompt, action="cancel"))
            )
        )
    )

    assert response_port.responses[-1].text == "Отменено."
    assert (
        session_store.active_session_for_lane(
            user_id=42,
            persona_session_id=lane.session_id,
        )
        is not None
    )
    check = open_state_database(database_path)
    outcome = check.execute(
        "SELECT outcome FROM action_confirmation ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    check.close()
    assert outcome == "cancelled"


def test_confirmation_replay_does_not_repeat_action(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )
    asyncio.run(service.handle(telegram_message("/remember one")))
    record = memory_store.list_for_user(user_id=42, page=1, page_size=5)[0]
    asyncio.run(service.handle(telegram_message(f"/forget {record.id}")))
    token = confirmation_token(response_port.responses[-1])
    callback = telegram_callback(confirmation_callback_data("confirm", token))

    asyncio.run(service.handle_callback(callback))
    asyncio.run(service.handle_callback(callback))

    texts = [response.text for response in response_port.responses]
    assert texts.count(f"Удалено: запись {record.id}.") == 1
    assert response_port.responses[-1].kind is TelegramResponseKind.ERROR
    assert "устарела" in response_port.responses[-1].text


def test_expired_confirmation_does_not_execute(tmp_path):
    clock = MutableClock()
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
        confirmation_clock=clock,
    )
    asyncio.run(service.handle(telegram_message("/remember one")))
    record = memory_store.list_for_user(user_id=42, page=1, page_size=5)[0]
    asyncio.run(service.handle(telegram_message(f"/forget {record.id}")))
    token = confirmation_token(response_port.responses[-1])

    clock.advance(301)
    asyncio.run(
        service.handle_callback(
            telegram_callback(confirmation_callback_data("confirm", token))
        )
    )

    assert "устарела" in response_port.responses[-1].text
    assert memory_store.get(record.id, user_id=42) is not None


def test_forget_without_id_lists_records_with_delete_buttons(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )
    asyncio.run(service.handle(telegram_message("/remember first")))
    asyncio.run(service.handle(telegram_message("/remember second")))
    records = memory_store.list_for_user(user_id=42, page=1, page_size=5)

    asyncio.run(service.handle(telegram_message("/forget")))

    response = response_port.responses[-1]
    assert "Выбери запись для удаления:" in response.text
    button_data = [
        parse_forget_callback_data(button.callback_data)
        for row in response.inline_keyboard
        for button in row
    ]
    assert sorted(button_data) == sorted(record.id for record in records)


def test_forget_delete_button_opens_confirmation_prompt(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )
    asyncio.run(service.handle(telegram_message("/remember one")))
    record = memory_store.list_for_user(user_id=42, page=1, page_size=5)[0]

    asyncio.run(
        service.handle_callback(telegram_callback(f"forget:v1:{record.id}"))
    )

    prompt = response_port.responses[-1]
    assert "Удалить запись" in prompt.text
    assert memory_store.get(record.id, user_id=42) is not None
    confirmation_token(prompt)


def test_forget_unknown_id_reports_not_found_without_confirmation(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )

    asyncio.run(service.handle(telegram_message("/forget 999")))

    assert response_port.responses[-1].kind is TelegramResponseKind.ERROR
    assert response_port.responses[-1].text == "Запись памяти не найдена."


def test_confirmed_forget_reports_when_record_already_deleted(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )
    asyncio.run(service.handle(telegram_message("/remember one")))
    record = memory_store.list_for_user(user_id=42, page=1, page_size=5)[0]
    asyncio.run(service.handle(telegram_message(f"/forget {record.id}")))
    token = confirmation_token(response_port.responses[-1])
    assert memory_store.delete(record.id, user_id=42) is True

    asyncio.run(
        service.handle_callback(
            telegram_callback(confirmation_callback_data("confirm", token))
        )
    )

    assert response_port.responses[-1].text == "Запись уже удалена."


def test_memories_pagination_buttons_and_page_callback_edits_in_place(tmp_path):
    response_port = EditableRecordingPort(
        bound=TelegramSentMessage(chat_id=500, message_id=77)
    )
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    service, _ = build_service(
        tmp_path,
        RecordingProvider(),
        response_port,
        memory_store=memory_store,
    )
    for index in range(6):
        memory_store.create(
            NewMemory(
                user_id=42,
                scope=MemoryScope.SHARED,
                kind="fact",
                content=f"memory-{index}",
                updated_at=f"2026-07-19T10:{index:02d}:00+00:00",
            )
        )

    asyncio.run(service.handle(telegram_message("/memories")))

    page_one = response_port.responses[-1]
    assert page_one.text.startswith("Память, страница 1 из 2:")
    page_one_data = [
        button.callback_data
        for row in page_one.inline_keyboard
        for button in row
    ]
    assert page_one_data == ["mem:v1:2"]

    asyncio.run(service.handle_callback(telegram_callback("mem:v1:2")))

    handle, edited = response_port.edits[-1]
    assert handle == TelegramSentMessage(chat_id=500, message_id=77)
    assert edited.text.startswith("Память, страница 2 из 2:")
    page_two_data = [
        button.callback_data
        for row in edited.inline_keyboard
        for button in row
    ]
    assert page_two_data == ["mem:v1:1"]
    assert parse_memories_page_callback_data(page_two_data[0]) == 1


def test_persona_callback_edits_picker_in_place(tmp_path):
    response_port = EditableRecordingPort(
        bound=TelegramSentMessage(chat_id=500, message_id=55)
    )
    service, router = build_service(tmp_path, RecordingProvider(), response_port)

    asyncio.run(
        service.handle_callback(telegram_callback("persona:v1:analyst"))
    )

    assert response_port.responses == []
    handle, edited = response_port.edits[-1]
    assert handle == TelegramSentMessage(chat_id=500, message_id=55)
    assert edited.text == "Выбери персону: Companion, Analyst."
    button_texts = [
        button.text for row in edited.inline_keyboard for button in row
    ]
    assert button_texts == ["Companion", "✓ Analyst"]
    assert router.route(telegram_message("check")).persona_key == "analyst"


def test_new_and_forget_are_unavailable_without_confirmation_store(tmp_path):
    response_port = RecordingResponsePort()
    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    session_store = SQLiteSessionStore(tmp_path / "state.db")
    provider = RecordingProvider(
        result=XaiTextResponse(
            response_id="resp-1",
            model="grok-4.5",
            text="Answer",
            credential_source="xai_oauth",
        )
    )
    service, _ = build_service(
        tmp_path,
        provider,
        response_port,
        memory_store=memory_store,
        session_store=session_store,
        session_finalizer=SQLiteSessionFinalizer(
            tmp_path / "state.db",
            FixedSummaryGenerator(),
            extraction_store=memory_store,
        ),
        confirmation_store=None,
    )
    asyncio.run(service.handle(telegram_message("/remember one")))
    record = memory_store.list_for_user(user_id=42, page=1, page_size=5)[0]
    asyncio.run(service.handle(telegram_message("open a session")))

    asyncio.run(service.handle(telegram_message(f"/forget {record.id}")))
    asyncio.run(service.handle(telegram_message("/new")))

    assert response_port.responses[-2].text == "Подтверждения сейчас недоступны."
    assert response_port.responses[-1].text == "Подтверждения сейчас недоступны."
    assert memory_store.get(record.id, user_id=42) is not None
