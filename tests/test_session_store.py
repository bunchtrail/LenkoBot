import pytest

from lenkobot.personas import Persona, PersonaCatalog
from lenkobot.memory import SQLiteMemoryStore
from lenkobot.session_store import (
    FailureStage,
    SQLiteSessionFinalizer,
    SQLiteSessionStore,
)
from lenkobot.sqlite_schema import open_state_database
from lenkobot.telegram_router import (
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)


class DiscardingReplyPort:
    def send(self, turn):
        pass


class FixedSummaryGenerator:
    def __init__(self, result="A bounded summary", error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def generate(self, *, turns):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def catalog():
    return PersonaCatalog(
        (
            Persona(
                key="companion",
                display_name="Companion",
                identity_prompt="A calm companion.",
                identity_version=1,
            ),
            Persona(
                key="analyst",
                display_name="Analyst",
                identity_prompt="A precise analyst.",
                identity_version=1,
            ),
        ),
        default_persona_key="companion",
    )


def message(text="hello"):
    return IncomingTelegramMessage(
        user_id=42,
        chat_id=500,
        chat_type="private",
        text=text,
    )


def routed_lane(database_path, *, persona_key="companion"):
    persona_catalog = catalog()
    conversation_store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        allowed_user_id=42,
        store=conversation_store,
        reply_port=DiscardingReplyPort(),
        persona_catalog=persona_catalog,
    )
    if persona_key != "companion":
        assert router.switch_persona(42, 500, persona_key, "private") is True
    turn = router.route(message())
    conversation_store.close()
    return turn


def test_active_session_and_raw_turns_survive_restart(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)

    user_turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="First question",
    )
    assistant_turn = store.append_assistant_turn(
        session_id=user_turn.session_id,
        content="First answer",
        provider_response_id="resp-1",
    )
    store.close()

    reopened = SQLiteSessionStore(database_path)
    session = reopened.ensure_active_session(
        user_id=42,
        persona_session_id=lane.session_id,
    )
    turns = reopened.list_turns(session_id=session.id, user_id=42)

    assert session.id == user_turn.session_id
    assert session.status == "active"
    assert session.generation == 1
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "First question"),
        ("assistant", "First answer"),
    ]
    assert assistant_turn.provider_response_id == "resp-1"
    assert reopened.list_turns(session_id=session.id, user_id=99) == ()
    with pytest.raises(PermissionError):
        reopened.ensure_active_session(
            user_id=99,
            persona_session_id=lane.session_id,
        )


def test_persona_lanes_have_different_active_sessions_and_transcripts(tmp_path):
    database_path = tmp_path / "state.db"
    companion_lane = routed_lane(database_path)
    analyst_lane = routed_lane(database_path, persona_key="analyst")
    store = SQLiteSessionStore(database_path)

    companion_turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=companion_lane.session_id,
        content="COMPANION-ONLY",
    )
    analyst_turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=analyst_lane.session_id,
        content="ANALYST-ONLY",
    )

    assert companion_turn.session_id != analyst_turn.session_id
    assert [
        turn.content
        for turn in store.list_turns(
            session_id=companion_turn.session_id,
            user_id=42,
        )
    ] == ["COMPANION-ONLY"]
    assert [
        turn.content
        for turn in store.list_turns(
            session_id=analyst_turn.session_id,
            user_id=42,
        )
    ] == ["ANALYST-ONLY"]


def test_provider_and_delivery_failures_are_redacted_operational_records(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    user_turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Question",
    )
    store.record_failure(
        session_id=user_turn.session_id,
        related_turn_id=user_turn.id,
        stage=FailureStage.PROVIDER,
        error_kind="provider_request_failed",
    )
    assistant_turn = store.append_assistant_turn(
        session_id=user_turn.session_id,
        content="Durable answer",
        provider_response_id="resp-1",
    )
    store.record_failure(
        session_id=user_turn.session_id,
        related_turn_id=assistant_turn.id,
        stage=FailureStage.DELIVERY,
        error_kind="telegram_delivery_failed",
    )

    failures = store.list_failures(session_id=user_turn.session_id, user_id=42)

    assert [(item.stage, item.error_kind) for item in failures] == [
        (FailureStage.PROVIDER, "provider_request_failed"),
        (FailureStage.DELIVERY, "telegram_delivery_failed"),
    ]
    assert "Durable answer" not in " ".join(item.error_kind for item in failures)


def test_extraction_run_is_idempotent_and_keeps_turn_provenance(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Extract this turn",
    )

    first = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )
    second = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )

    assert first == second
    assert first.owner_user_id == 42
    assert first.session_id == turn.session_id
    assert first.source_turn_id == turn.id
    assert first.lifecycle_epoch == 1
    assert first.status.value == "pending"
    assert first.attempt == 0


@pytest.mark.parametrize(
    ("terminal", "expected_error"),
    (("completed", None), ("failed", "extractor_failed"), ("discarded", None)),
)
def test_extraction_run_claims_once_and_has_terminal_transitions(
    tmp_path,
    terminal,
    expected_error,
):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Process this turn",
    )
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )

    claimed = memory_store.claim_extraction_run(run.id, owner_user_id=42)

    assert claimed.status.value == "processing"
    assert claimed.attempt == 1
    with pytest.raises(RuntimeError, match="claimable"):
        memory_store.claim_extraction_run(run.id, owner_user_id=42)

    if terminal == "completed":
        finished = memory_store.complete_extraction_run(run.id, owner_user_id=42)
    elif terminal == "failed":
        finished = memory_store.fail_extraction_run(
            run.id,
            owner_user_id=42,
            error_kind=expected_error,
        )
    else:
        finished = memory_store.discard_extraction_run(run.id, owner_user_id=42)

    assert finished.status.value == terminal
    assert finished.error_kind == expected_error
    with pytest.raises(RuntimeError, match="terminal"):
        memory_store.complete_extraction_run(run.id, owner_user_id=42)


def test_extraction_run_rejects_wrong_owner_and_invalid_source_turn(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Owner-bound turn",
    )

    with pytest.raises(ValueError, match="turn does not belong"):
        memory_store.ensure_extraction_run(
            owner_user_id=42,
            session_id=turn.session_id,
            source_turn_id=turn.id + 999,
        )
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )
    with pytest.raises(KeyError):
        memory_store.claim_extraction_run(run.id, owner_user_id=99)


def test_extraction_run_rejects_stale_epoch_and_new_epoch_gets_new_run(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Epoch-bound turn",
    )
    stale = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )
    connection = open_state_database(database_path)
    connection.execute(
        "UPDATE user_profile SET lifecycle_epoch = 2 WHERE user_id = 42"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="stale"):
        memory_store.claim_extraction_run(stale.id, owner_user_id=42)

    current = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )
    assert current.id != stale.id
    assert current.lifecycle_epoch == 2


def test_failed_summary_generation_preserves_active_session_and_raw_turns(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Keep this raw turn",
    )
    generator = FixedSummaryGenerator(error=RuntimeError("provider unavailable"))
    memory_store = SQLiteMemoryStore(database_path)
    finalizer = SQLiteSessionFinalizer(
        database_path,
        generator,
        extraction_store=memory_store,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        finalizer.finalize(session_id=turn.session_id, owner_user_id=42)

    assert [item.content for item in store.list_turns(session_id=turn.session_id, user_id=42)] == [
        "Keep this raw turn"
    ]
    assert store.ensure_active_session(user_id=42, persona_session_id=lane.session_id).id == turn.session_id


def test_finalization_is_atomic_idempotent_and_opens_next_generation(tmp_path):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Summarize this",
    )
    store.append_assistant_turn(
        session_id=turn.session_id,
        content="And this answer",
        provider_response_id="resp-1",
    )
    memory_store = SQLiteMemoryStore(database_path)
    extraction_run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )
    memory_store.claim_extraction_run(extraction_run.id, owner_user_id=42)
    memory_store.complete_extraction_run(extraction_run.id, owner_user_id=42)
    generator = FixedSummaryGenerator()
    finalizer = SQLiteSessionFinalizer(
        database_path,
        generator,
        extraction_store=memory_store,
    )

    first = finalizer.finalize(session_id=turn.session_id, owner_user_id=42)
    second = finalizer.finalize(session_id=turn.session_id, owner_user_id=42)
    next_session = store.ensure_active_session(user_id=42, persona_session_id=lane.session_id)

    assert first == second
    assert first.content == "A bounded summary"
    assert first.source_turn_count == 2
    assert generator.calls == 1
    assert store.list_turns(session_id=turn.session_id, user_id=42) == ()
    check = open_state_database(database_path)
    assert check.execute(
        "SELECT status FROM memory_extraction_run WHERE source_turn_id = ?",
        (turn.id,),
    ).fetchone()[0] == "completed"
    check.close()
    assert next_session.id != turn.session_id
    assert next_session.generation == 2


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_incomplete_memory_extraction_blocks_finalization_without_calling_generator(
    tmp_path,
    status,
):
    database_path = tmp_path / "state.db"
    lane = routed_lane(database_path)
    store = SQLiteSessionStore(database_path)
    turn = store.begin_user_turn(
        user_id=42,
        persona_session_id=lane.session_id,
        content="Wait for extraction",
    )
    memory_store = SQLiteMemoryStore(database_path)
    extraction_run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=turn.session_id,
        source_turn_id=turn.id,
    )
    if status != "pending":
        memory_store.claim_extraction_run(extraction_run.id, owner_user_id=42)
    if status == "failed":
        memory_store.fail_extraction_run(
            extraction_run.id,
            owner_user_id=42,
            error_kind="extractor_failed",
        )
    generator = FixedSummaryGenerator()
    finalizer = SQLiteSessionFinalizer(
        database_path,
        generator,
        extraction_store=memory_store,
    )

    with pytest.raises(RuntimeError, match="extraction is not complete"):
        finalizer.finalize(session_id=turn.session_id, owner_user_id=42)

    assert generator.calls == 0
    assert store.turn_count() == 1
