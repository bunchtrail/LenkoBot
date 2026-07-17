from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Protocol

from .personas import Persona, PersonaCatalog


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
        self._connection = sqlite3.connect(database_path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation (
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                active_persona_key TEXT NOT NULL,
                UNIQUE(platform, chat_id)
            );

            CREATE TABLE IF NOT EXISTS persona_session (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversation(id),
                persona_key TEXT NOT NULL,
                identity_version INTEGER NOT NULL,
                UNIQUE(conversation_id, persona_key, identity_version)
            );
            """
        )

    def route_message(
        self,
        message: IncomingTelegramMessage,
        persona_catalog: PersonaCatalog,
    ) -> RoutedTurn:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO conversation (platform, chat_id, active_persona_key)
                VALUES ('telegram', ?, ?)
                """,
                (message.chat_id, persona_catalog.default_persona_key),
            )
            conversation = self._connection.execute(
                """
                SELECT id, active_persona_key
                FROM conversation
                WHERE platform = 'telegram' AND chat_id = ?
                """,
                (message.chat_id,),
            ).fetchone()
            conversation_id, persona_key = conversation
            persona = persona_catalog.get(persona_key)
            if persona is None:
                raise ValueError("active persona is not present in catalog")
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
                WHERE conversation_id = ? AND persona_key = ? AND identity_version = ?
                """,
                (conversation_id, persona_key, persona.identity_version),
            ).fetchone()

        return RoutedTurn(
            conversation_id=conversation_id,
            chat_id=message.chat_id,
            persona_key=persona_key,
            session_id=session[0],
            identity_version=persona.identity_version,
            text=message.text,
        )

    def switch_persona(self, chat_id: int, persona: Persona) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO conversation (platform, chat_id, active_persona_key)
                VALUES ('telegram', ?, ?)
                """,
                (chat_id, persona.key),
            )
            self._connection.execute(
                """
                UPDATE conversation
                SET active_persona_key = ?
                WHERE platform = 'telegram' AND chat_id = ?
                """,
                (persona.key, chat_id),
            )
            conversation_id = self._connection.execute(
                """
                SELECT id
                FROM conversation
                WHERE platform = 'telegram' AND chat_id = ?
                """,
                (chat_id,),
            ).fetchone()[0]
            self._connection.execute(
                """
                INSERT OR IGNORE INTO persona_session
                    (conversation_id, persona_key, identity_version)
                VALUES (?, ?, ?)
                """,
                (conversation_id, persona.key, persona.identity_version),
            )

    def conversation_count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]

    def persona_session_count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM persona_session").fetchone()[0]


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
