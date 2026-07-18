from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .personas import Persona, PersonaCatalog
from .sqlite_schema import open_state_database


@dataclass(frozen=True)
class IncomingTelegramMessage:
    user_id: int
    chat_id: int
    text: str
    chat_type: str | None = None


@dataclass(frozen=True)
class RoutedTurn:
    conversation_id: int
    chat_id: int
    persona_key: str
    session_id: int
    identity_version: int
    text: str


class ReplyPort(Protocol):
    def send(self, turn: RoutedTurn) -> None: ...


class SQLiteConversationStore:
    def __init__(self, database_path: Path) -> None:
        self._connection = open_state_database(database_path)

    def route_message(
        self,
        message: IncomingTelegramMessage,
        persona_catalog: PersonaCatalog,
    ) -> RoutedTurn:
        self._ensure_conversation(message.chat_id, persona_catalog.default_persona_key)
        for _ in range(8):
            conversation = self._connection.execute(
                """
                SELECT id, active_persona_key, version
                FROM conversation
                WHERE platform = 'telegram' AND chat_id = ?
                """,
                (message.chat_id,),
            ).fetchone()
            conversation_id = int(conversation["id"])
            persona_key = str(conversation["active_persona_key"])
            version = int(conversation["version"])
            persona = persona_catalog.get(persona_key)
            if persona is None:
                raise ValueError("active persona is not present in catalog")
            try:
                with self._connection:
                    updated = self._connection.execute(
                        """
                        UPDATE conversation
                        SET version = version + 1
                        WHERE id = ? AND version = ?
                        """,
                        (conversation_id, version),
                    )
                    if updated.rowcount != 1:
                        raise _ConversationVersionConflict
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO persona_session
                            (conversation_id, persona_key, identity_version)
                        VALUES (?, ?, ?)
                        """,
                        (conversation_id, persona_key, persona.identity_version),
                    )
                    session = self._connection.execute(
                        """
                        SELECT id
                        FROM persona_session
                        WHERE conversation_id = ? AND persona_key = ?
                            AND identity_version = ?
                        """,
                        (conversation_id, persona_key, persona.identity_version),
                    ).fetchone()
            except _ConversationVersionConflict:
                continue

            return RoutedTurn(
                conversation_id=conversation_id,
                chat_id=message.chat_id,
                persona_key=persona_key,
                session_id=int(session["id"]),
                identity_version=persona.identity_version,
                text=message.text,
            )
        raise RuntimeError("conversation changed too frequently")

    def current_lane(
        self,
        chat_id: int,
        persona_catalog: PersonaCatalog,
    ) -> RoutedTurn | None:
        row = self._connection.execute(
            """
            SELECT conversation.id, conversation.active_persona_key,
                lane.id AS persona_session_id, lane.identity_version
            FROM conversation
            JOIN persona_session AS lane
                ON lane.conversation_id = conversation.id
                AND lane.persona_key = conversation.active_persona_key
            WHERE conversation.platform = 'telegram' AND conversation.chat_id = ?
            ORDER BY lane.id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        persona = persona_catalog.get(str(row["active_persona_key"]))
        if persona is None:
            raise ValueError("active persona is not present in catalog")
        return RoutedTurn(
            conversation_id=int(row["id"]),
            chat_id=chat_id,
            persona_key=persona.key,
            session_id=int(row["persona_session_id"]),
            identity_version=int(row["identity_version"]),
            text="",
        )

    def switch_persona(self, chat_id: int, persona: Persona) -> None:
        self._ensure_conversation(chat_id, persona.key)
        for _ in range(8):
            conversation = self._connection.execute(
                """
                SELECT id, version
                FROM conversation
                WHERE platform = 'telegram' AND chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            conversation_id = int(conversation["id"])
            version = int(conversation["version"])
            try:
                with self._connection:
                    updated = self._connection.execute(
                        """
                        UPDATE conversation
                        SET active_persona_key = ?, version = version + 1
                        WHERE id = ? AND version = ?
                        """,
                        (persona.key, conversation_id, version),
                    )
                    if updated.rowcount != 1:
                        raise _ConversationVersionConflict
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO persona_session
                            (conversation_id, persona_key, identity_version)
                        VALUES (?, ?, ?)
                        """,
                        (conversation_id, persona.key, persona.identity_version),
                    )
            except _ConversationVersionConflict:
                continue
            return
        raise RuntimeError("conversation changed too frequently")

    def _ensure_conversation(self, chat_id: int, persona_key: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO conversation (platform, chat_id, active_persona_key)
                VALUES ('telegram', ?, ?)
                """,
                (chat_id, persona_key),
            )

    def conversation_count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]

    def persona_session_count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM persona_session").fetchone()[0]

    def close(self) -> None:
        self._connection.close()


class _ConversationVersionConflict(RuntimeError):
    pass


class TelegramRouter:
    def __init__(
        self,
        allowed_user_id: int,
        store: SQLiteConversationStore,
        reply_port: ReplyPort,
        persona_catalog: PersonaCatalog,
    ) -> None:
        self._allowed_user_id = allowed_user_id
        self._store = store
        self._reply_port = reply_port
        self._persona_catalog = persona_catalog

    def is_authorized(self, user_id: int, chat_type: str | None) -> bool:
        return user_id == self._allowed_user_id and chat_type == "private"

    def route(self, message: IncomingTelegramMessage) -> RoutedTurn | None:
        if not self.is_authorized(message.user_id, message.chat_type):
            return None

        return self._store.route_message(message, self._persona_catalog)

    def current_lane(self, message: IncomingTelegramMessage) -> RoutedTurn | None:
        if not self.is_authorized(message.user_id, message.chat_type):
            return None
        return self._store.current_lane(message.chat_id, self._persona_catalog)

    def handle(self, message: IncomingTelegramMessage) -> RoutedTurn | None:
        turn = self.route(message)
        if turn is None:
            return None

        self._reply_port.send(turn)
        return turn

    def switch_persona(
        self,
        user_id: int,
        chat_id: int,
        persona_key: str,
        chat_type: str | None = None,
    ) -> bool:
        if not self.is_authorized(user_id, chat_type):
            return False

        persona = self._persona_catalog.get(persona_key)
        if persona is None:
            return False

        self._store.switch_persona(chat_id, persona)
        return True
