from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .persona_store import ensure_persona_version, persona_version_from_row
from .personas import Persona, PersonaCatalog
from .sqlite_schema import open_state_database


@dataclass(frozen=True)
class IncomingTelegramMessage:
    user_id: int
    chat_id: int
    text: str
    chat_type: str | None = None


@dataclass(frozen=True)
class IncomingTelegramCallback:
    user_id: int
    chat_id: int
    data: str
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
                    _, persona_version_id = ensure_persona_version(
                        self._connection,
                        persona,
                    )
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
                            (conversation_id, persona_key, identity_version,
                                persona_version_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            persona_key,
                            persona.identity_version,
                            persona_version_id,
                        ),
                    )
                    self._connection.execute(
                        """
                        UPDATE persona_session
                        SET persona_version_id = ?
                        WHERE conversation_id = ? AND persona_key = ?
                            AND identity_version = ?
                            AND persona_version_id IS NULL
                        """,
                        (
                            persona_version_id,
                            conversation_id,
                            persona_key,
                            persona.identity_version,
                        ),
                    )
                    session = self._connection.execute(
                        """
                        SELECT id
                        FROM persona_session
                        WHERE conversation_id = ? AND persona_key = ?
                            AND identity_version = ? AND persona_version_id = ?
                        """,
                        (
                            conversation_id,
                            persona_key,
                            persona.identity_version,
                            persona_version_id,
                        ),
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
                    _, persona_version_id = ensure_persona_version(
                        self._connection,
                        persona,
                    )
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
                            (conversation_id, persona_key, identity_version,
                                persona_version_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            persona.key,
                            persona.identity_version,
                            persona_version_id,
                        ),
                    )
                    self._connection.execute(
                        """
                        UPDATE persona_session
                        SET persona_version_id = ?
                        WHERE conversation_id = ? AND persona_key = ?
                            AND identity_version = ?
                            AND persona_version_id IS NULL
                        """,
                        (
                            persona_version_id,
                            conversation_id,
                            persona.key,
                            persona.identity_version,
                        ),
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

    def persist_persona_catalog(self, persona_catalog: PersonaCatalog) -> None:
        with self._connection:
            for persona in persona_catalog.personas:
                ensure_persona_version(self._connection, persona)

    def persona_version_for_lane(self, session_id: int):
        row = self._connection.execute(
            """
            SELECT version.id, version.persona_id, persona.key,
                version.display_name, version.identity_prompt,
                version.identity_version, version.voice_json,
                version.content_hash, version.created_at
            FROM persona_session AS lane
            JOIN persona_version AS version
                ON version.id = lane.persona_version_id
            JOIN persona ON persona.id = version.persona_id
            WHERE lane.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            row = self._connection.execute(
                """
                SELECT version.id, version.persona_id, persona.key,
                    version.display_name, version.identity_prompt,
                    version.identity_version, version.voice_json,
                    version.content_hash, version.created_at
                FROM persona_session AS lane
                JOIN persona ON persona.key = lane.persona_key
                    AND persona.profile_id = 'default'
                JOIN persona_version AS version
                    ON version.persona_id = persona.id
                    AND version.identity_version = lane.identity_version
                WHERE lane.id = ?
                """,
                (session_id,),
            ).fetchone()
        return None if row is None else persona_version_from_row(row)

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

    @property
    def allowed_user_id(self) -> int:
        return self._allowed_user_id

    def route(self, message: IncomingTelegramMessage) -> RoutedTurn | None:
        if not self.is_authorized(message.user_id, message.chat_type):
            return None

        return self._store.route_message(message, self._persona_catalog)

    def replace_catalog(self, persona_catalog: PersonaCatalog) -> None:
        for current in self._persona_catalog.personas:
            replacement = persona_catalog.get(current.key)
            if replacement is None:
                continue
            if replacement.identity_version < current.identity_version or (
                replacement.identity_version == current.identity_version
                and replacement != current
            ):
                raise ValueError(
                    "persona reload requires a higher identity_version"
                )
        self._store.persist_persona_catalog(persona_catalog)
        self._persona_catalog = persona_catalog

    def persona_for_turn(self, turn: RoutedTurn) -> Persona:
        version = self._store.persona_version_for_lane(turn.session_id)
        if version is not None:
            return version.as_persona()
        persona = self._persona_catalog.get(turn.persona_key)
        if persona is None or persona.identity_version != turn.identity_version:
            raise ValueError("routed persona version is not available")
        return persona

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
