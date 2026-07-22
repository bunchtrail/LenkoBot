import asyncio
import json
from pathlib import Path

import pytest

from lenkobot.application_service import TelegramApplicationService
from lenkobot.context_builder import ContextBuilder
from lenkobot.memory import SQLiteMemoryStore
from lenkobot.memory_extraction import ExtractionCoordinator
from lenkobot.personas import Persona, PersonaCatalog, VoicePack, VoiceRenderer
from lenkobot.session_store import SQLiteSessionStore
from lenkobot.telegram_presentation import TelegramResponseKind
from lenkobot.telegram_router import (
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)
from lenkobot.xai_provider import (
    BearerCredential,
    HttpResponse,
    XaiInputMessage,
    XaiResponsesTransport,
    XaiTextResponse,
)


def persona_catalog(version=1, *, voice=None):
    return PersonaCatalog(
        (
            Persona(
                key="companion",
                display_name="Companion",
                identity_prompt=f"Identity version {version}.",
                identity_version=version,
                voice=voice or VoicePack(),
            ),
        ),
        default_persona_key="companion",
    )


def test_bro_identity_keeps_conversation_sloppy_without_corrupting_terms():
    persona = PersonaCatalog.from_toml(
        Path(__file__).parents[1] / "config.example.toml"
    ).get("lenko")

    assert persona is not None
    assert persona.identity_version == 6
    assert "не копируй ошибки" in persona.identity_prompt
    assert "каноническом виде" in persona.identity_prompt
    assert "не искажай термины" in persona.identity_prompt
    assert "косвенным признакам" in persona.identity_prompt
    assert "печатал с телефона" in persona.identity_prompt


def test_bro_status_voice_rotates_plain_short_phrases():
    persona = PersonaCatalog.from_toml(
        Path(__file__).parents[1] / "config.example.toml"
    ).get("lenko")

    assert persona is not None
    assert persona.identity_version == 6
    assert persona.voice.status == (
        "щас вникну",
        "дай соображу",
        "разбираюсь",
        "собираю ответ",
    )

    renderer = VoiceRenderer()
    rendered = tuple(
        renderer.render(persona, "status", fallback="Готовлю ответ")
        for _ in persona.voice.status
    )

    assert rendered == persona.voice.status


class RecordingResponsePort:
    def __init__(self, events):
        self.events = events
        self.responses = []

    async def send(self, response):
        self.events.append(("response", response.kind))
        self.responses.append(response)


class RecordingProvider:
    def __init__(self):
        self.prompts = []

    def respond(self, prompt):
        self.prompts.append(prompt)
        return XaiTextResponse(
            response_id="response-1",
            model="grok-4.5",
            text="Answer",
            credential_source="xai_oauth",
        )


class RecordingCoordinator:
    def __init__(self, events):
        self.events = events

    def drain_for_lane(self, *, owner_user_id, persona_session_id):
        self.events.append(("drain", persona_session_id))

    def process_after_delivery(self, *, run_id, owner_user_id, persona_session_id):
        self.events.append(("claim", run_id, persona_session_id))


class RecoveryExtractionService:
    def __init__(self, memory_store):
        self.memory_store = memory_store
        self.calls = []

    def process_with_retry(self, run_id, *, owner_user_id, max_attempts=3):
        self.calls.append(run_id)
        self.memory_store.claim_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
        )
        return self.memory_store.complete_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
        )


def test_voice_pack_parses_allowlisted_placeholders_and_renders_deterministically(tmp_path):
    config_path = tmp_path / "personas.toml"
    config_path.write_text(
        """
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "Stay direct. Avoid canned greetings and robotic status language."
        identity_version = 2

        [personas.voice]
        status = ["Working, {persona}."]
        notice = ["{text}"]
        command = ["Done: {text}"]
        error = ["Could not complete: {text}"]
        """.strip(),
        encoding="utf-8",
    )

    persona = PersonaCatalog.from_toml(config_path).get("companion")

    assert persona.voice.render("status", persona="Companion") == (
        "Working, Companion."
    )
    assert persona.voice.render("command", text="reloaded") == "Done: reloaded"


def test_voice_pack_rejects_untrusted_template_placeholders():
    with pytest.raises(ValueError, match="placeholder"):
        VoicePack(status=("{provider_output}",))


@pytest.mark.parametrize("kind", ("status", "notice", "command", "error"))
def test_voice_pack_rejects_banned_phrase_in_every_user_facing_kind(kind):
    with pytest.raises(ValueError, match="banned phrase"):
        VoicePack(**{kind: ("As an AI, I cannot help.",)})


@pytest.mark.parametrize(
    "cliche",
    ("Чем помочь?", "Чем могу помочь?", "How can I help you today?"),
)
def test_voice_pack_rejects_help_offer_cliche(cliche):
    with pytest.raises(ValueError, match="banned phrase"):
        VoicePack(status=(cliche,))


def test_persona_lint_rejects_help_offer_cliche_in_identity():
    with pytest.raises(ValueError, match="banned phrase"):
        PersonaCatalog(
            (
                Persona(
                    key="companion",
                    display_name="Companion",
                    identity_prompt="Всегда завершай ответ вопросом: чем могу помочь?",
                    identity_version=1,
                ),
            ),
            default_persona_key="companion",
        )


def test_voice_enabled_persona_requires_anti_template_identity_guidance(tmp_path):
    config_path = tmp_path / "personas.toml"
    config_path.write_text(
        """
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "Stay direct."
        identity_version = 1

        [personas.voice]
        status = ["Working."]
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="anti-template"):
        PersonaCatalog.from_toml(config_path)


def test_voice_enabled_persona_accepts_russian_anti_template_guidance(tmp_path):
    config_path = tmp_path / "personas.toml"
    config_path.write_text(
        """
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "Пиши простым текстом, без канцелярита и без тика ассистента."
        identity_version = 1

        [personas.voice]
        status = ["сек"]
        """.strip(),
        encoding="utf-8",
    )

    persona = PersonaCatalog.from_toml(config_path).get("companion")

    assert persona is not None


def test_persona_lint_rejects_banned_identity_phrase():
    with pytest.raises(ValueError, match="banned phrase"):
        PersonaCatalog(
            (
                Persona(
                    key="companion",
                    display_name="Companion",
                    identity_prompt="You are an AI assistant.",
                    identity_version=1,
                ),
            ),
            default_persona_key="companion",
        )


def test_persona_reload_persists_new_version_without_rewriting_old_lane(tmp_path):
    database_path = tmp_path / "state.db"
    store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        42,
        store,
        _DiscardingReplyPort(),
        persona_catalog(1),
    )

    first = router.route(
        IncomingTelegramMessage(42, 500, "before", "private")
    )
    router.replace_catalog(persona_catalog(2))

    old_lane = router.current_lane(
        IncomingTelegramMessage(42, 500, "", "private")
    )
    second = router.route(
        IncomingTelegramMessage(42, 500, "after", "private")
    )
    old_persona = router.persona_for_turn(old_lane)

    assert old_lane.session_id == first.session_id
    assert old_lane.identity_version == 1
    assert old_persona.identity_prompt == "Identity version 1."
    assert second.identity_version == 2
    assert second.session_id != first.session_id

    store.close()
    restarted = SQLiteConversationStore(database_path)
    restarted_router = TelegramRouter(
        42,
        restarted,
        _DiscardingReplyPort(),
        persona_catalog(2),
    )
    persisted_old = restarted_router.persona_for_turn(old_lane)

    assert persisted_old.identity_version == 1
    assert persisted_old.identity_prompt == "Identity version 1."
    assert persisted_old.voice == VoicePack()
    restarted.close()


def test_unauthorized_persona_reload_does_not_read_config(tmp_path):
    database_path = tmp_path / "state.db"
    conversation_store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        42,
        conversation_store,
        _DiscardingReplyPort(),
        persona_catalog(1),
    )
    response_port = RecordingResponsePort([])
    service = TelegramApplicationService(
        router,
        persona_catalog(1),
        RecordingProvider(),
        response_port=response_port,
        persona_config_path=tmp_path / "missing.toml",
    )

    asyncio.run(
        service.handle(
            IncomingTelegramMessage(99, 500, "/persona reload", "private")
        )
    )

    assert response_port.responses == []
    conversation_store.close()


def test_persona_reload_is_fail_closed_for_same_version_and_monotonic_for_new_version(
    tmp_path,
):
    config_path = tmp_path / "personas.toml"
    database_path = tmp_path / "state.db"
    conversation_store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        42,
        conversation_store,
        _DiscardingReplyPort(),
        persona_catalog(1),
    )
    response_port = RecordingResponsePort([])
    service = TelegramApplicationService(
        router,
        persona_catalog(1),
        RecordingProvider(),
        response_port=response_port,
        persona_config_path=config_path,
    )

    config_path.write_text(
        """
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "Changed without a version bump."
        identity_version = 1
        """.strip(),
        encoding="utf-8",
    )
    asyncio.run(
        service.handle(IncomingTelegramMessage(42, 500, "/persona reload", "private"))
    )

    assert response_port.responses[-1].kind is TelegramResponseKind.ERROR
    assert router.route(IncomingTelegramMessage(42, 500, "old", "private")).identity_version == 1

    config_path.write_text(
        """
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "Identity version 2."
        identity_version = 2
        """.strip(),
        encoding="utf-8",
    )
    asyncio.run(
        service.handle(IncomingTelegramMessage(42, 500, "/persona reload", "private"))
    )

    assert response_port.responses[-1].kind is TelegramResponseKind.FINAL
    new_turn = router.route(
        IncomingTelegramMessage(42, 500, "new", "private")
    )
    assert new_turn.identity_version == 2
    assert router.persona_for_turn(new_turn).identity_prompt == "Identity version 2."
    conversation_store.close()


def test_context_builder_emits_role_structured_messages_with_untrusted_sections(tmp_path):
    from lenkobot.telegram_router import RoutedTurn

    memory_store = SQLiteMemoryStore(tmp_path / "state.db")
    persona = persona_catalog().get("companion")
    messages = ContextBuilder(memory_store).build_messages(
        user_id=42,
        persona=persona,
        turn=RoutedTurn(1, 500, "companion", 10, 1, "Hello"),
    )

    assert [(message.role, message.content) for message in messages] == [
        ("system", "Identity version 1."),
        ("user", "Hello"),
    ]
    memory_store.close()


def test_responses_transport_serializes_role_messages_as_input_array():
    class RecordingHttpClient:
        def __init__(self):
            self.payload = None

        def post_json(self, url, headers, payload):
            self.payload = payload
            return HttpResponse(
                status=200,
                headers={},
                body=json.dumps(
                    {
                        "id": "response-1",
                        "model": "grok-4.5",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": "Answer"}
                                ],
                            }
                        ],
                    }
                ),
            )

    http_client = RecordingHttpClient()
    transport = XaiResponsesTransport(http_client=http_client)

    transport.complete(
        BearerCredential(
            token="secret",
            expires_at=None,
            base_url="https://api.x.ai/v1",
            source_identity="xai_oauth",
        ),
        "grok-4.5",
        (
            XaiInputMessage("system", "Identity"),
            XaiInputMessage("user", "Hello"),
        ),
    )

    assert http_client.payload["input"] == [
        {"role": "system", "content": "Identity"},
        {"role": "user", "content": "Hello"},
    ]


def test_final_delivery_happens_before_extraction_claim(tmp_path):
    database_path = tmp_path / "state.db"
    memory_store = SQLiteMemoryStore(database_path)
    session_store = SQLiteSessionStore(database_path)
    events = []
    response_port = RecordingResponsePort(events)
    provider = RecordingProvider()
    coordinator = RecordingCoordinator(events)
    catalog = persona_catalog()
    router = TelegramRouter(
        42,
        SQLiteConversationStore(database_path),
        _DiscardingReplyPort(),
        catalog,
    )
    service = TelegramApplicationService(
        router,
        catalog,
        provider,
        response_port=response_port,
        memory_store=memory_store,
        session_store=session_store,
        extraction_coordinator=coordinator,
    )

    asyncio.run(
        service.handle(IncomingTelegramMessage(42, 500, "hello", "private"))
    )

    final_index = next(
        index
        for index, event in enumerate(events)
        if event == ("response", TelegramResponseKind.FINAL)
    )
    claim_index = next(
        index for index, event in enumerate(events) if event[0] == "claim"
    )
    assert final_index < claim_index
    memory_store.close()
    session_store.close()


def test_extraction_coordinator_recovers_processing_run_before_next_lane_turn(tmp_path):
    database_path = tmp_path / "state.db"
    catalog = persona_catalog()
    conversation_store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        42,
        conversation_store,
        _DiscardingReplyPort(),
        catalog,
    )
    lane = router.route(IncomingTelegramMessage(42, 500, "start", "private"))
    session_store = SQLiteSessionStore(database_path)
    user_turn = session_store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="question",
    )
    memory_store = SQLiteMemoryStore(database_path)
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=user_turn.session_id,
        source_turn_id=user_turn.id,
    )
    memory_store.claim_extraction_run(run.id, owner_user_id=42)
    extraction_service = RecoveryExtractionService(memory_store)

    ExtractionCoordinator(memory_store, extraction_service).drain_for_lane(
        owner_user_id=42,
        persona_session_id=lane.session_id,
    )

    recovered = memory_store.get_extraction_run(run.id, owner_user_id=42)
    assert recovered.status.value == "completed"
    assert extraction_service.calls == [run.id]
    memory_store.close()
    session_store.close()
    conversation_store.close()


class _DiscardingReplyPort:
    def send(self, turn):
        return None
