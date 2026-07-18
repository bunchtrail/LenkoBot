from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
import sqlite3
from typing import Protocol

from .memory import MemoryExtractionRunReader
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


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: int
    session_id: int
    owner_user_id: int
    content: str
    source_turn_count: int
    lifecycle_epoch: int
    status: str
    created_at: str


class SummaryGenerator(Protocol):
    def generate(self, *, turns: tuple[TranscriptTurn, ...]) -> str: ...


class SessionFinalizer(Protocol):
    def finalize(
        self,
        *,
        session_id: int,
        owner_user_id: int,
    ) -> SessionSummary: ...


class ExtractionProcessor(Protocol):
    def process_for_session(
        self,
        *,
        session_id: int,
        owner_user_id: int,
    ) -> None: ...


_ERROR_KIND = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_MAX_SUMMARY_CHARS = 4_000


class SQLiteSessionFinalizer:
    def __init__(
        self,
        database_path: Path | str,
        summary_generator: SummaryGenerator,
        *,
        extraction_store: MemoryExtractionRunReader,
        extraction_processor: ExtractionProcessor | None = None,
    ) -> None:
        self._connection = open_state_database(database_path)
        self._summary_generator = summary_generator
        self._extraction_store = extraction_store
        self._extraction_processor = extraction_processor

    def finalize(
        self,
        *,
        session_id: int,
        owner_user_id: int,
    ) -> SessionSummary:
        existing = self._get_summary(session_id=session_id, owner_user_id=owner_user_id)
        if existing is not None:
            return existing

        session = self._load_active_session(
            session_id=session_id,
            owner_user_id=owner_user_id,
        )
        lifecycle_epoch = int(session["lifecycle_epoch"])
        turns = self._load_turns(session_id=session_id)
        if self._extraction_processor is not None:
            self._extraction_processor.process_for_session(
                session_id=session_id,
                owner_user_id=owner_user_id,
            )
        self._ensure_extraction_gate(
            session_id=session_id,
            owner_user_id=owner_user_id,
        )
        content = self._summary_generator.generate(turns=turns)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("session summary cannot be empty")
        content = content.strip()
        if len(content) > _MAX_SUMMARY_CHARS:
            raise ValueError("session summary exceeds the bounded limit")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._get_summary(
                session_id=session_id,
                owner_user_id=owner_user_id,
            )
            if existing is not None:
                self._connection.commit()
                return existing
            current = self._load_active_session(
                session_id=session_id,
                owner_user_id=owner_user_id,
            )
            if int(current["lifecycle_epoch"]) != lifecycle_epoch:
                raise RuntimeError("owner lifecycle changed during finalization")
            current_turns = self._load_turns(session_id=session_id)
            if tuple(turn.id for turn in current_turns) != tuple(
                turn.id for turn in turns
            ):
                raise RuntimeError("session changed during finalization")
            self._ensure_extraction_gate(
                session_id=session_id,
                owner_user_id=owner_user_id,
            )
            now = _utc_now()
            cursor = self._connection.execute(
                """
                INSERT INTO session_summary (
                    session_id, owner_user_id, content, source_turn_count,
                    lifecycle_epoch, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    session_id,
                    owner_user_id,
                    content,
                    len(turns),
                    int(current["lifecycle_epoch"]),
                    now,
                ),
            )
            self._connection.execute(
                "DELETE FROM transcript_turn WHERE session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                """
                UPDATE session
                SET status = 'closed', closed_at = ?
                WHERE id = ? AND owner_user_id = ? AND status = 'active'
                """,
                (now, session_id, owner_user_id),
            )
            row = self._connection.execute(
                "SELECT * FROM session_summary WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _summary_from_row(row)

    def _get_summary(
        self,
        *,
        session_id: int,
        owner_user_id: int,
    ) -> SessionSummary | None:
        row = self._connection.execute(
            """
            SELECT * FROM session_summary
            WHERE session_id = ? AND owner_user_id = ?
            """,
            (session_id, owner_user_id),
        ).fetchone()
        return None if row is None else _summary_from_row(row)

    def _load_active_session(
        self,
        *,
        session_id: int,
        owner_user_id: int,
    ) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT active_session.id, profile.lifecycle_epoch
            FROM session AS active_session
            JOIN user_profile AS profile
                ON profile.user_id = active_session.owner_user_id
            WHERE active_session.id = ?
                AND active_session.owner_user_id = ?
                AND active_session.status = 'active'
                AND profile.lifecycle_state = 'active'
            """,
            (session_id, owner_user_id),
        ).fetchone()
        if row is None:
            raise ValueError("active session does not exist for owner")
        return row

    def _load_turns(self, *, session_id: int) -> tuple[TranscriptTurn, ...]:
        rows = self._connection.execute(
            """
            SELECT id, session_id, sequence, role, content,
                provider_response_id, created_at
            FROM transcript_turn
            WHERE session_id = ?
            ORDER BY sequence ASC
            """,
            (session_id,),
        ).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def _ensure_extraction_gate(self, *, session_id: int, owner_user_id: int) -> None:
        if self._extraction_store.has_blocking_extraction_runs(
            owner_user_id=owner_user_id,
            session_id=session_id,
        ):
            raise RuntimeError("memory extraction is not complete")

    def close(self) -> None:
        self._connection.close()


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

    def active_session_for_lane(
        self,
        *,
        user_id: int,
        persona_session_id: int,
    ) -> ActiveSession | None:
        row = self._connection.execute(
            """
            SELECT id, persona_session_id, owner_user_id, generation, status,
                opened_at
            FROM session
            WHERE persona_session_id = ? AND owner_user_id = ? AND status = 'active'
            """,
            (persona_session_id, user_id),
        ).fetchone()
        return None if row is None else _active_session_from_row(row)

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

    def extraction_exchange(
        self,
        *,
        session_id: int,
        user_id: int,
        source_turn_id: int,
    ) -> tuple[TranscriptTurn, TranscriptTurn]:
        turns = self.list_turns(session_id=session_id, user_id=user_id)
        for index, turn in enumerate(turns):
            if turn.id != source_turn_id:
                continue
            if turn.role != "user" or index + 1 >= len(turns):
                break
            assistant = turns[index + 1]
            if assistant.role != "assistant":
                break
            return turn, assistant
        raise ValueError("source turn does not have a user/assistant exchange")

    def latest_summary_for_lane(
        self,
        *,
        user_id: int,
        persona_session_id: int,
    ) -> SessionSummary | None:
        row = self._connection.execute(
            """
            SELECT summary.*
            FROM session_summary AS summary
            JOIN session AS closed_session
                ON closed_session.id = summary.session_id
            WHERE summary.owner_user_id = ?
                AND closed_session.owner_user_id = ?
                AND closed_session.persona_session_id = ?
                AND summary.status = 'active'
            ORDER BY summary.id DESC
            LIMIT 1
            """,
            (user_id, user_id, persona_session_id),
        ).fetchone()
        return None if row is None else _summary_from_row(row)

    def persona_key_for_session(self, *, session_id: int, user_id: int) -> str:
        row = self._connection.execute(
            """
            SELECT lane.persona_key
            FROM session AS active_session
            JOIN persona_session AS lane
                ON lane.id = active_session.persona_session_id
            WHERE active_session.id = ? AND active_session.owner_user_id = ?
            """,
            (session_id, user_id),
        ).fetchone()
        if row is None:
            raise KeyError("session does not belong to owner")
        return str(row["persona_key"])

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


def _summary_from_row(row: sqlite3.Row) -> SessionSummary:
    return SessionSummary(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        owner_user_id=int(row["owner_user_id"]),
        content=str(row["content"]),
        source_turn_count=int(row["source_turn_count"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )
