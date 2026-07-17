from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
import sqlite3
from typing import Protocol

from .sqlite_schema import open_state_database


class FailureStage(str, Enum):
    PROVIDER = "provider"
    DELIVERY = "delivery"


@dataclass(frozen=True, slots=True)
class ActiveSession:
    id: int
    persona_session_id: int
    owner_user_id: int
    generation: int
    status: str
    opened_at: str


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    id: int
    session_id: int
    sequence: int
    role: str
    content: str
    provider_response_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TranscriptFailure:
    id: int
    session_id: int
    related_turn_id: int
    stage: FailureStage
    error_kind: str
    created_at: str


class SessionFinalizer(Protocol):
    def finalize(self, *, session_id: int, owner_user_id: int) -> None: ...


_ERROR_KIND = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class SQLiteSessionStore:
    def __init__(self, database_path: Path | str) -> None:
        self._connection = open_state_database(database_path)

    def ensure_active_session(
        self,
        *,
        user_id: int,
        persona_session_id: int,
    ) -> ActiveSession:
        if user_id < 1 or persona_session_id < 1:
            raise ValueError("session owner and persona lane must be positive")
        now = _utc_now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO user_profile (user_id, created_at)
                VALUES (?, ?)
                """,
                (user_id, now),
            )
            lane = self._connection.execute(
                "SELECT id FROM persona_session WHERE id = ?",
                (persona_session_id,),
            ).fetchone()
            if lane is None:
                raise ValueError("persona session lane does not exist")
            row = self._connection.execute(
                """
                SELECT id, persona_session_id, owner_user_id, generation, status,
                    opened_at
                FROM session
                WHERE persona_session_id = ? AND status = 'active'
                """,
                (persona_session_id,),
            ).fetchone()
            if row is not None and int(row["owner_user_id"]) != user_id:
                raise PermissionError("active session belongs to another owner")
            if row is None:
                generation = int(
                    self._connection.execute(
                        """
                        SELECT COALESCE(MAX(generation), 0) + 1
                        FROM session
                        WHERE persona_session_id = ?
                        """,
                        (persona_session_id,),
                    ).fetchone()[0]
                )
                cursor = self._connection.execute(
                    """
                    INSERT INTO session (
                        persona_session_id, owner_user_id, generation, status,
                        opened_at
                    ) VALUES (?, ?, ?, 'active', ?)
                    """,
                    (persona_session_id, user_id, generation, now),
                )
                row = self._connection.execute(
                    """
                    SELECT id, persona_session_id, owner_user_id, generation, status,
                        opened_at
                    FROM session
                    WHERE id = ?
                    """,
                    (cursor.lastrowid,),
                ).fetchone()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _active_session_from_row(row)

    def begin_user_turn(
        self,
        *,
        user_id: int,
        persona_session_id: int,
        content: str,
    ) -> TranscriptTurn:
        active_session = self.ensure_active_session(
            user_id=user_id,
            persona_session_id=persona_session_id,
        )
        return self._append_turn(
            session_id=active_session.id,
            role="user",
            content=content,
            provider_response_id=None,
        )

    def append_assistant_turn(
        self,
        *,
        session_id: int,
        content: str,
        provider_response_id: str | None,
    ) -> TranscriptTurn:
        return self._append_turn(
            session_id=session_id,
            role="assistant",
            content=content,
            provider_response_id=provider_response_id,
        )

    def _append_turn(
        self,
        *,
        session_id: int,
        role: str,
        content: str,
        provider_response_id: str | None,
    ) -> TranscriptTurn:
        if role not in {"user", "assistant"}:
            raise ValueError("unsupported transcript role")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("transcript content cannot be empty")
        now = _utc_now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            active = self._connection.execute(
                "SELECT id FROM session WHERE id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if active is None:
                raise ValueError("active session does not exist")
            sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM transcript_turn
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            cursor = self._connection.execute(
                """
                INSERT INTO transcript_turn (
                    session_id, sequence, role, content, provider_response_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    role,
                    content.strip(),
                    provider_response_id,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id, session_id, sequence, role, content,
                    provider_response_id, created_at
                FROM transcript_turn
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _turn_from_row(row)

    def record_failure(
        self,
        *,
        session_id: int,
        related_turn_id: int,
        stage: FailureStage,
        error_kind: str,
    ) -> TranscriptFailure:
        try:
            normalized_stage = FailureStage(stage)
        except ValueError as error:
            raise ValueError("unsupported transcript failure stage") from error
        if not isinstance(error_kind, str) or _ERROR_KIND.fullmatch(error_kind) is None:
            raise ValueError("failure error_kind must be a bounded identifier")
        now = _utc_now()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO transcript_failure (
                    session_id, related_turn_id, stage, error_kind, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    related_turn_id,
                    normalized_stage.value,
                    error_kind,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id, session_id, related_turn_id, stage, error_kind,
                    created_at
                FROM transcript_failure
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
        return _failure_from_row(row)

    def list_turns(
        self,
        *,
        session_id: int,
        user_id: int,
    ) -> tuple[TranscriptTurn, ...]:
        rows = self._connection.execute(
            """
            SELECT turn.id, turn.session_id, turn.sequence, turn.role,
                turn.content, turn.provider_response_id, turn.created_at
            FROM transcript_turn AS turn
            JOIN session AS active_session ON active_session.id = turn.session_id
            WHERE turn.session_id = ? AND active_session.owner_user_id = ?
            ORDER BY turn.sequence ASC
            """,
            (session_id, user_id),
        ).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def list_recent_for_context(
        self,
        *,
        user_id: int,
        persona_session_id: int,
        session_id: int,
        before_turn_id: int,
        limit: int,
    ) -> tuple[TranscriptTurn, ...]:
        if limit < 1:
            raise ValueError("transcript context limit must be positive")
        rows = self._connection.execute(
            """
            SELECT turn.id, turn.session_id, turn.sequence, turn.role,
                turn.content, turn.provider_response_id, turn.created_at
            FROM transcript_turn AS turn
            JOIN session AS active_session ON active_session.id = turn.session_id
            JOIN transcript_turn AS current_turn
                ON current_turn.id = ? AND current_turn.session_id = turn.session_id
            WHERE turn.session_id = ?
                AND active_session.owner_user_id = ?
                AND active_session.persona_session_id = ?
                AND active_session.status = 'active'
                AND turn.sequence < current_turn.sequence
            ORDER BY turn.sequence DESC
            LIMIT ?
            """,
            (
                before_turn_id,
                session_id,
                user_id,
                persona_session_id,
                limit,
            ),
        ).fetchall()
        return tuple(_turn_from_row(row) for row in reversed(rows))

    def list_failures(
        self,
        *,
        session_id: int,
        user_id: int,
    ) -> tuple[TranscriptFailure, ...]:
        rows = self._connection.execute(
            """
            SELECT failure.id, failure.session_id, failure.related_turn_id,
                failure.stage, failure.error_kind, failure.created_at
            FROM transcript_failure AS failure
            JOIN session AS active_session ON active_session.id = failure.session_id
            WHERE failure.session_id = ? AND active_session.owner_user_id = ?
            ORDER BY failure.id ASC
            """,
            (session_id, user_id),
        ).fetchall()
        return tuple(_failure_from_row(row) for row in rows)

    def profile_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0])

    def session_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM session").fetchone()[0])

    def turn_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM transcript_turn").fetchone()[0])

    def close(self) -> None:
        self._connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_session_from_row(row: sqlite3.Row) -> ActiveSession:
    return ActiveSession(
        id=int(row["id"]),
        persona_session_id=int(row["persona_session_id"]),
        owner_user_id=int(row["owner_user_id"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        opened_at=str(row["opened_at"]),
    )


def _turn_from_row(row: sqlite3.Row) -> TranscriptTurn:
    return TranscriptTurn(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        sequence=int(row["sequence"]),
        role=str(row["role"]),
        content=str(row["content"]),
        provider_response_id=(
            None
            if row["provider_response_id"] is None
            else str(row["provider_response_id"])
        ),
        created_at=str(row["created_at"]),
    )


def _failure_from_row(row: sqlite3.Row) -> TranscriptFailure:
    return TranscriptFailure(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        related_turn_id=int(row["related_turn_id"]),
        stage=FailureStage(str(row["stage"])),
        error_kind=str(row["error_kind"]),
        created_at=str(row["created_at"]),
    )
