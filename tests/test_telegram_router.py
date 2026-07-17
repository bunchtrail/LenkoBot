from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

from lenkobot.telegram_router import (
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)
from lenkobot.personas import PersonaCatalog


class RecordingReplyPort:
    def __init__(self):
        self.turns = []

    def send(self, turn):
        self.turns.append(turn)


def build_catalog(tmp_path, companion_version=1):
    config_path = tmp_path / "personas.toml"
    config_path.write_text(
        f"""
        default_persona_key = "companion"

        [[personas]]
        key = "companion"
        display_name = "Companion"
        identity_prompt = "A calm companion."
        identity_version = {companion_version}

        [[personas]]
        key = "analyst"
        display_name = "Analyst"
        identity_prompt = "A precise analyst."
        identity_version = 1
        """,
        encoding="utf-8",
    )
    return PersonaCatalog.from_toml(config_path)


def test_unauthorized_message_creates_no_state_and_sends_no_reply(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state.db")
    reply_port = RecordingReplyPort()
    router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=reply_port,
        persona_catalog=build_catalog(tmp_path),
    )

    outcome = router.handle(
        IncomingTelegramMessage(user_id=99, chat_id=500, chat_type="private", text="hello")
    )

    assert outcome is None
    assert store.conversation_count() == 0
    assert store.persona_session_count() == 0
    assert reply_port.turns == []


def test_authorized_message_gets_a_stable_default_persona_session_and_one_reply(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state.db")
    reply_port = RecordingReplyPort()
    router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=reply_port,
        persona_catalog=build_catalog(tmp_path),
    )

    first_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="hello")
    )
    second_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="again")
    )

    assert first_turn.persona_key == "companion"
    assert first_turn.chat_id == 500
    assert first_turn.conversation_id == second_turn.conversation_id
    assert first_turn.session_id == second_turn.session_id
    assert store.conversation_count() == 1
    assert store.persona_session_count() == 1
    assert reply_port.turns == [first_turn, second_turn]


def test_switching_persona_uses_a_new_lane_and_resumes_the_original_lane(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state.db")
    reply_port = RecordingReplyPort()
    router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=reply_port,
        persona_catalog=build_catalog(tmp_path),
    )

    companion_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="hello")
    )
    assert router.switch_persona(
        user_id=42, chat_id=500, persona_key="analyst", chat_type="private"
    )
    analyst_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="analyze this")
    )
    assert router.switch_persona(
        user_id=42, chat_id=500, persona_key="companion", chat_type="private"
    )
    resumed_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="back")
    )

    assert analyst_turn.persona_key == "analyst"
    assert analyst_turn.session_id != companion_turn.session_id
    assert resumed_turn.persona_key == "companion"
    assert resumed_turn.session_id == companion_turn.session_id
    assert store.persona_session_count() == 2


def test_unknown_or_unauthorized_persona_switch_does_not_change_active_lane(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state.db")
    reply_port = RecordingReplyPort()
    router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=reply_port,
        persona_catalog=build_catalog(tmp_path),
    )

    original_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="hello")
    )

    assert not router.switch_persona(
        user_id=99, chat_id=500, persona_key="analyst", chat_type="private"
    )
    assert not router.switch_persona(
        user_id=42, chat_id=500, persona_key="unknown", chat_type="private"
    )
    current_turn = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="still here")
    )

    assert current_turn.persona_key == "companion"
    assert current_turn.session_id == original_turn.session_id
    assert store.persona_session_count() == 1


def test_allowed_user_in_group_chat_is_rejected_by_private_only_mvp(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state.db")
    reply_port = RecordingReplyPort()
    router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=reply_port,
        persona_catalog=build_catalog(tmp_path),
    )

    outcome = router.handle(
        IncomingTelegramMessage(
            user_id=42,
            chat_id=-500,
            chat_type="group",
            text="hello",
        )
    )
    missing_type_outcome = router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=501, text="hello")
    )

    assert outcome is None
    assert missing_type_outcome is None
    assert store.conversation_count() == 0
    assert store.persona_session_count() == 0
    assert reply_port.turns == []


def test_identity_version_change_starts_a_new_session_lane(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state.db")
    first_router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=RecordingReplyPort(),
        persona_catalog=build_catalog(tmp_path, companion_version=1),
    )
    first_turn = first_router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="before update")
    )

    updated_router = TelegramRouter(
        allowed_user_id=42,
        store=store,
        reply_port=RecordingReplyPort(),
        persona_catalog=build_catalog(tmp_path, companion_version=2),
    )
    updated_turn = updated_router.handle(
        IncomingTelegramMessage(user_id=42, chat_id=500, chat_type="private", text="after update")
    )

    assert updated_turn.persona_key == "companion"
    assert updated_turn.identity_version == 2
    assert updated_turn.session_id != first_turn.session_id
    assert store.persona_session_count() == 2


def test_concurrent_routes_share_one_lane_and_advance_conversation_version(tmp_path):
    database_path = tmp_path / "state.db"
    persona_catalog = build_catalog(tmp_path)
    initialized = SQLiteConversationStore(database_path)
    initialized.close()
    barrier = Barrier(2)

    def route(text):
        store = SQLiteConversationStore(database_path)
        try:
            barrier.wait(timeout=5)
            return store.route_message(
                IncomingTelegramMessage(
                    user_id=42,
                    chat_id=500,
                    chat_type="private",
                    text=text,
                ),
                persona_catalog,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        turns = tuple(executor.map(route, ("first", "second")))

    connection = sqlite3.connect(database_path)
    version = connection.execute(
        "SELECT version FROM conversation WHERE chat_id = 500"
    ).fetchone()[0]
    connection.close()

    assert turns[0].conversation_id == turns[1].conversation_id
    assert turns[0].session_id == turns[1].session_id
    assert version == 2


def test_concurrent_route_and_switch_keep_turn_bound_to_a_valid_lane(tmp_path):
    database_path = tmp_path / "state.db"
    persona_catalog = build_catalog(tmp_path)
    initial_store = SQLiteConversationStore(database_path)
    initial_store.route_message(
        IncomingTelegramMessage(
            user_id=42,
            chat_id=500,
            chat_type="private",
            text="initial",
        ),
        persona_catalog,
    )
    initial_store.close()
    barrier = Barrier(2)

    def route():
        store = SQLiteConversationStore(database_path)
        try:
            barrier.wait(timeout=5)
            return store.route_message(
                IncomingTelegramMessage(
                    user_id=42,
                    chat_id=500,
                    chat_type="private",
                    text="racing turn",
                ),
                persona_catalog,
            )
        finally:
            store.close()

    def switch():
        store = SQLiteConversationStore(database_path)
        try:
            barrier.wait(timeout=5)
            store.switch_persona(500, persona_catalog.get("analyst"))
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        route_future = executor.submit(route)
        switch_future = executor.submit(switch)
        turn = route_future.result()
        switch_future.result()

    connection = sqlite3.connect(database_path)
    conversation = connection.execute(
        "SELECT active_persona_key, version FROM conversation WHERE chat_id = 500"
    ).fetchone()
    matching_lane = connection.execute(
        """
        SELECT COUNT(*)
        FROM persona_session
        WHERE id = ? AND conversation_id = ? AND persona_key = ?
            AND identity_version = ?
        """,
        (
            turn.session_id,
            turn.conversation_id,
            turn.persona_key,
            turn.identity_version,
        ),
    ).fetchone()[0]
    connection.close()

    assert tuple(conversation) == ("analyst", 3)
    assert matching_lane == 1
