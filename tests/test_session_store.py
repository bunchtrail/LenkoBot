import pytest

from lenkobot.personas import Persona, PersonaCatalog
from lenkobot.session_store import FailureStage, SQLiteSessionStore
from lenkobot.telegram_router import (
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)


class DiscardingReplyPort:
    def send(self, turn):
        pass


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
