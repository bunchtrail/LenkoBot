import asyncio

from lenkobot.application_service import TelegramApplicationService
from lenkobot.context_builder import ContextBuilder
from lenkobot.memory import MemoryScope, NewMemory, SQLiteMemoryStore
from lenkobot.personas import PersonaCatalog
from lenkobot.telegram_presentation import TelegramResponseKind
from lenkobot.telegram_router import IncomingTelegramMessage, SQLiteConversationStore, TelegramRouter
from lenkobot.xai_provider import ProviderRequestError, XaiTextResponse


class RecordingResponsePort:
    def __init__(self):
        self.responses = []

    async def send(self, response):
        self.responses.append(response)


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


def build_service(tmp_path, provider, response_port, *, context_builder=None):
    catalog = build_catalog(tmp_path)
    router = TelegramRouter(
        allowed_user_id=42,
        store=SQLiteConversationStore(tmp_path / "state.db"),
        reply_port=RecordingResponsePort(),
        persona_catalog=catalog,
    )
    return TelegramApplicationService(
        router=router,
        persona_catalog=catalog,
        provider=provider,
        response_port=response_port,
        context_builder=context_builder,
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


def test_persona_command_without_key_lists_catalog_without_provider_call(tmp_path):
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
    assert response_port.responses[0].text == (
        "Доступные персоны: companion (Companion), analyst (Analyst)."
    )


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
