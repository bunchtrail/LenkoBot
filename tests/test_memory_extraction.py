import json

import pytest

from lenkobot.memory import (
    ExtractionRunStatus,
    MemoryCategory,
    MemoryScope,
    SQLiteMemoryStore,
)
from lenkobot.memory_extraction import MemoryExtractionService
from lenkobot.memory_extraction import MemoryCandidate, MemoryCandidatePolicy
from lenkobot.personas import Persona, PersonaCatalog
from lenkobot.session_store import SQLiteSessionStore
from lenkobot.telegram_router import (
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)


class StructuredProvider:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def respond(self, prompt, *, schema_name, schema):
        self.calls.append((prompt, schema_name, schema))
        if self.error is not None:
            raise self.error
        return type("Response", (), {"value": self.payload})()


class FlakyStructuredProvider(StructuredProvider):
    def __init__(self, payload):
        super().__init__(payload)
        self.failures_left = 1

    def respond(self, prompt, *, schema_name, schema):
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("temporary provider failure")
        return super().respond(prompt, schema_name=schema_name, schema=schema)


def persona_catalog():
    return PersonaCatalog(
        (
            Persona(
                key="companion",
                display_name="Companion",
                identity_prompt="A calm companion.",
                identity_version=1,
            ),
        ),
        default_persona_key="companion",
    )


def active_exchange(database_path):
    catalog = persona_catalog()
    conversation_store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        allowed_user_id=42,
        store=conversation_store,
        reply_port=lambda response: None,
        persona_catalog=catalog,
    )
    lane = router.route(
        IncomingTelegramMessage(
            user_id=42,
            chat_id=500,
            chat_type="private",
            text="start",
        )
    )
    conversation_store.close()
    session_store = SQLiteSessionStore(database_path)
    user_turn = session_store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="I prefer concise answers",
    )
    assistant_turn = session_store.append_assistant_turn(
        session_id=user_turn.session_id,
        content="I will keep answers concise.",
        provider_response_id="resp-1",
    )
    return session_store, user_turn, assistant_turn


def test_memory_extraction_activates_valid_candidates_and_denies_sensitive_data(
    tmp_path,
):
    database_path = tmp_path / "state.db"
    session_store, user_turn, _ = active_exchange(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    memory_store.register_persona(persona_catalog().get("companion"))
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=user_turn.session_id,
        source_turn_id=user_turn.id,
    )
    provider = StructuredProvider(
        {
            "candidates": [
                {
                    "text": "Prefers concise answers",
                    "category": "preference",
                    "scope": "persona_private",
                    "confidence": 0.81,
                    "evidence_turn_ids": [user_turn.id, user_turn.id + 1],
                },
                {
                    "text": "My password is secret",
                    "category": "fact",
                    "scope": "shared",
                    "confidence": 0.99,
                    "evidence_turn_ids": [user_turn.id],
                },
            ]
        }
    )
    service = MemoryExtractionService(
        memory_store,
        session_store,
        provider,
    )

    records = service.process(
        run.id,
        owner_user_id=42,
    )

    assert len(records) == 1
    assert records[0].category is MemoryCategory.PREFERENCE
    assert records[0].scope is MemoryScope.PERSONA_PRIVATE
    assert records[0].provenance_turn_id == user_turn.id
    assert records[0].provenance_session_id == user_turn.session_id
    assert memory_store.list_for_user(user_id=42, page=1, page_size=5) == records
    assert memory_store.get_extraction_run(run.id, owner_user_id=42).status is (
        ExtractionRunStatus.COMPLETED
    )
    assert json.dumps(provider.calls[0][2], ensure_ascii=True)


def test_memory_extraction_failure_does_not_persist_partial_candidates(tmp_path):
    database_path = tmp_path / "state.db"
    session_store, user_turn, _ = active_exchange(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=user_turn.session_id,
        source_turn_id=user_turn.id,
    )
    provider = StructuredProvider(
        {
            "candidates": [
                {
                    "text": "valid candidate",
                    "category": "fact",
                    "scope": "shared",
                    "confidence": 0.8,
                    "evidence_turn_ids": [999999],
                }
            ]
        }
    )
    service = MemoryExtractionService(memory_store, session_store, provider)

    with pytest.raises(ValueError, match="evidence"):
        service.process(run.id, owner_user_id=42)

    assert memory_store.memory_count() == 0
    failed = memory_store.get_extraction_run(run.id, owner_user_id=42)
    assert failed.status is ExtractionRunStatus.FAILED
    assert failed.error_kind == "extraction_failed"


def test_memory_extraction_retry_is_deduplicated_by_provenance(tmp_path):
    database_path = tmp_path / "state.db"
    session_store, user_turn, _ = active_exchange(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=user_turn.session_id,
        source_turn_id=user_turn.id,
    )
    payload = {
        "candidates": [
            {
                "text": "Likes examples",
                "category": "preference",
                "scope": "shared",
                "confidence": 0.7,
                "evidence_turn_ids": [user_turn.id],
            }
        ]
    }
    service = MemoryExtractionService(
        memory_store,
        session_store,
        StructuredProvider(payload),
    )
    memory_store.register_persona(persona_catalog().get("companion"))

    first = service.process(run.id, owner_user_id=42)
    assert len(first) == 1
    with pytest.raises(RuntimeError, match="claimable"):
        service.process(run.id, owner_user_id=42)
    assert memory_store.memory_count() == 1


def test_memory_extraction_retries_transient_provider_failure(tmp_path):
    database_path = tmp_path / "state.db"
    session_store, user_turn, _ = active_exchange(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    memory_store.register_persona(persona_catalog().get("companion"))
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=user_turn.session_id,
        source_turn_id=user_turn.id,
    )
    provider = FlakyStructuredProvider(
        {
            "candidates": [
                {
                    "text": "Likes examples",
                    "category": "preference",
                    "scope": "shared",
                    "confidence": 0.7,
                    "evidence_turn_ids": [user_turn.id],
                }
            ]
        }
    )

    records = MemoryExtractionService(
        memory_store,
        session_store,
        provider,
    ).process_with_retry(run.id, owner_user_id=42)

    assert len(records) == 1
    assert memory_store.get_extraction_run(run.id, owner_user_id=42).attempt == 2
    assert memory_store.memory_count() == 1


def test_memory_extraction_activation_rolls_back_all_candidates_on_write_failure(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "state.db"
    session_store, user_turn, _ = active_exchange(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    memory_store.register_persona(persona_catalog().get("companion"))
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=user_turn.session_id,
        source_turn_id=user_turn.id,
    )
    provider = StructuredProvider(
        {
            "candidates": [
                {
                    "text": "first",
                    "category": "fact",
                    "scope": "shared",
                    "confidence": 0.7,
                    "evidence_turn_ids": [user_turn.id],
                },
                {
                    "text": "second",
                    "category": "goal",
                    "scope": "shared",
                    "confidence": 0.7,
                    "evidence_turn_ids": [user_turn.id],
                },
            ]
        }
    )
    original = memory_store._create_in_transaction
    calls = 0

    def fail_on_second(memory, *, timestamp):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated write failure")
        return original(memory, timestamp=timestamp)

    monkeypatch.setattr(memory_store, "_create_in_transaction", fail_on_second)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        MemoryExtractionService(memory_store, session_store, provider).process(
            run.id,
            owner_user_id=42,
        )

    assert memory_store.memory_count() == 0
    assert memory_store.get_extraction_run(run.id, owner_user_id=42).status.value == "failed"


@pytest.mark.parametrize(
    "text",
    (
        "The password is secret",
        "The user's salary is private",
        "The diagnosis requires treatment",
        "The email contact is private",
        "The home address is private",
        "The intimate relationship is private",
    ),
)
def test_local_policy_denies_sensitive_content_regardless_of_confidence(text):
    candidate = MemoryCandidate(
        text=text,
        category="fact",
        scope="shared",
        confidence=1.0,
        evidence_turn_ids=(1,),
    )

    assert MemoryCandidatePolicy.validate(candidate) is None
