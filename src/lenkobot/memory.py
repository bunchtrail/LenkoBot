from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
import re
import sqlite3
from typing import Protocol

from .personas import Persona
from .persona_store import ensure_persona_version, persona_version_from_row
from .sqlite_schema import open_state_database


class MemoryScope(StrEnum):
    SHARED = "shared"
    PERSONA_PRIVATE = "persona_private"
    RELATIONSHIP = "relationship"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class MemorySource(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class MemoryCategory(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    GOAL = "goal"
    CONSTRAINT = "constraint"
    RELATIONSHIP = "relationship"
    EVENT = "event"


class ExtractionRunStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True)
class NewMemory:
    user_id: int
    scope: MemoryScope | str
    kind: str
    content: str
    persona_id: int | None = None
    relationship_id: int | None = None
    provenance_session_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    source: MemorySource | str = MemorySource.MANUAL
    category: MemoryCategory | str | None = None
    confidence: float | None = None
    provenance_turn_id: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: int
    user_id: int
    scope: MemoryScope
    kind: str
    content: str
    persona_id: int | None
    relationship_id: int | None
    provenance_session_id: int | None
    status: MemoryStatus
    created_at: str
    updated_at: str
    version: int
    source: MemorySource
    category: MemoryCategory | None
    confidence: float | None
    provenance_turn_id: int | None


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    id: int
    memory_id: int
    owner_user_id: int
    version: int
    content: str
    category: MemoryCategory | None
    confidence: float | None
    changed_at: str


@dataclass(frozen=True, slots=True)
class Relationship:
    id: int
    user_id: int
    persona_id: int
    summary: str
    state_json: str
    version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class MemoryExtractionRun:
    id: int
    owner_user_id: int
    session_id: int
    source_turn_id: int
    lifecycle_epoch: int
    status: ExtractionRunStatus
    attempt: int
    error_kind: str | None
    created_at: str
    updated_at: str


class MemoryExtractionRunReader(Protocol):
    def has_blocking_extraction_runs(
        self,
        *,
        owner_user_id: int,
        session_id: int,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    shared: int = 20
    persona_private: int = 20
    relationship: int = 20
    total: int = 60

    def __post_init__(self) -> None:
        values = (self.shared, self.persona_private, self.relationship, self.total)
        if any(value < 0 for value in values):
            raise ValueError("memory limits cannot be negative")


@dataclass(frozen=True, slots=True)
class MemoryContext:
    shared: tuple[MemoryRecord, ...]
    persona_private: tuple[MemoryRecord, ...]
    relationship: tuple[MemoryRecord, ...]
    relationship_state: Relationship | None

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(
            sorted(
                self.shared + self.persona_private + self.relationship,
                key=lambda record: (record.updated_at, record.id),
                reverse=True,
            )
        )


class RelationshipVersionConflict(RuntimeError):
    pass


class MemoryVersionConflict(RuntimeError):
    pass


_EXTRACTION_ERROR_KIND = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class SQLiteMemoryStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        profile_id: str = "default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not profile_id.strip():
            raise ValueError("profile_id cannot be empty")
        self._profile_id = profile_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._connection = open_state_database(database_path)

    def register_persona(self, persona: Persona) -> int:
        with self._connection:
            persona_id, _ = ensure_persona_version(
                self._connection,
                persona,
                profile_id=self._profile_id,
                created_at=self._timestamp(),
            )
        return persona_id

    def persona_version_for_identity(
        self,
        key: str,
        identity_version: int,
    ):
        row = self._connection.execute(
            """
            SELECT version.id, version.persona_id, persona.key,
                version.display_name, version.identity_prompt,
                version.identity_version, version.voice_json,
                version.content_hash, version.created_at
            FROM persona_version AS version
            JOIN persona ON persona.id = version.persona_id
            WHERE persona.profile_id = ? AND persona.key = ?
                AND version.identity_version = ?
            """,
            (self._profile_id, key, identity_version),
        ).fetchone()
        return None if row is None else persona_version_from_row(row)

    def persona_id_for_key(self, key: str) -> int | None:
        row = self._connection.execute(
            "SELECT id FROM persona WHERE profile_id = ? AND key = ?",
            (self._profile_id, key),
        ).fetchone()
        return None if row is None else int(row["id"])

    def activate_extraction(
        self,
        run_id: int,
        *,
        owner_user_id: int,
        memories: tuple[NewMemory, ...],
    ) -> tuple[MemoryRecord, ...]:
        for memory in memories:
            self._normalize_memory_metadata(memory)
        timestamp = self._timestamp()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            run = self._connection.execute(
                """
                SELECT extraction.*, profile.lifecycle_epoch AS current_epoch,
                    profile.lifecycle_state AS current_state
                FROM memory_extraction_run AS extraction
                JOIN user_profile AS profile
                    ON profile.user_id = extraction.owner_user_id
                WHERE extraction.id = ? AND extraction.owner_user_id = ?
                """,
                (run_id, owner_user_id),
            ).fetchone()
            if run is None:
                raise KeyError("extraction run not found for owner")
            if (
                run["current_state"] != "active"
                or int(run["current_epoch"]) != int(run["lifecycle_epoch"])
            ):
                raise RuntimeError("extraction run is stale")
            if run["status"] != ExtractionRunStatus.PROCESSING.value:
                raise RuntimeError("extraction run is not processing")
            for memory in memories:
                if memory.user_id != owner_user_id:
                    raise PermissionError("automatic memory owner does not match run")
                if MemorySource(memory.source) is not MemorySource.AUTOMATIC:
                    raise ValueError("extraction activation requires automatic memory")
                if memory.provenance_session_id != int(run["session_id"]):
                    raise ValueError("automatic memory session provenance does not match run")
                if memory.provenance_turn_id != int(run["source_turn_id"]):
                    raise ValueError("automatic memory turn provenance does not match run")
            rows = tuple(
                self._create_in_transaction(memory, timestamp=timestamp)
                for memory in memories
            )
            cursor = self._connection.execute(
                """
                UPDATE memory_extraction_run
                SET status = 'completed', error_kind = NULL, updated_at = ?
                WHERE id = ? AND owner_user_id = ? AND status = 'processing'
                    AND lifecycle_epoch = ?
                """,
                (
                    timestamp,
                    run_id,
                    owner_user_id,
                    int(run["lifecycle_epoch"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("extraction run completion was lost")
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return tuple(self._memory_from_row(row) for row in rows)

    def ensure_relationship(self, *, user_id: int, persona_id: int) -> Relationship:
        timestamp = self._timestamp()
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO relationship (
                    user_id, persona_id, summary, state_json, version, updated_at
                )
                VALUES (?, ?, '', '{}', 1, ?)
                """,
                (user_id, persona_id, timestamp),
            )
        relationship = self.get_relationship(user_id=user_id, persona_id=persona_id)
        if relationship is None:
            raise RuntimeError("relationship was not created")
        return relationship

    def ensure_extraction_run(
        self,
        *,
        owner_user_id: int,
        session_id: int,
        source_turn_id: int,
    ) -> MemoryExtractionRun:
        _validate_positive_ids(owner_user_id, session_id, source_turn_id)
        timestamp = self._timestamp()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            session = self._connection.execute(
                """
                SELECT active_session.owner_user_id, active_session.status,
                    profile.lifecycle_epoch, profile.lifecycle_state
                FROM session AS active_session
                JOIN user_profile AS profile
                    ON profile.user_id = active_session.owner_user_id
                WHERE active_session.id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None or int(session["owner_user_id"]) != owner_user_id:
                raise KeyError("session not found for owner")
            if session["status"] != "active":
                raise RuntimeError("extraction session is not active")
            if session["lifecycle_state"] != "active":
                raise RuntimeError("owner lifecycle is not active")
            source_turn = self._connection.execute(
                """
                SELECT id FROM transcript_turn
                WHERE id = ? AND session_id = ?
                """,
                (source_turn_id, session_id),
            ).fetchone()
            if source_turn is None:
                raise ValueError("source turn does not belong to session")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO memory_extraction_run (
                    owner_user_id, session_id, source_turn_id, lifecycle_epoch,
                    status, attempt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    owner_user_id,
                    session_id,
                    source_turn_id,
                    int(session["lifecycle_epoch"]),
                    timestamp,
                    timestamp,
                ),
            )
            row = self._connection.execute(
                """
                SELECT * FROM memory_extraction_run
                WHERE session_id = ? AND source_turn_id = ?
                    AND lifecycle_epoch = ?
                """,
                (
                    session_id,
                    source_turn_id,
                    int(session["lifecycle_epoch"]),
                ),
            ).fetchone()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        if row is None:
            raise RuntimeError("extraction run was not created")
        return _extraction_run_from_row(row)

    def get_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
    ) -> MemoryExtractionRun | None:
        _validate_positive_ids(run_id, owner_user_id)
        row = self._connection.execute(
            """
            SELECT * FROM memory_extraction_run
            WHERE id = ? AND owner_user_id = ?
            """,
            (run_id, owner_user_id),
        ).fetchone()
        return None if row is None else _extraction_run_from_row(row)

    def list_extraction_runs(
        self,
        *,
        owner_user_id: int,
        session_id: int,
    ) -> tuple[MemoryExtractionRun, ...]:
        _validate_positive_ids(owner_user_id, session_id)
        rows = self._connection.execute(
            """
            SELECT * FROM memory_extraction_run
            WHERE owner_user_id = ? AND session_id = ?
            ORDER BY id ASC
            """,
            (owner_user_id, session_id),
        ).fetchall()
        return tuple(_extraction_run_from_row(row) for row in rows)

    def list_extraction_runs_for_lane(
        self,
        *,
        owner_user_id: int,
        persona_session_id: int,
    ) -> tuple[MemoryExtractionRun, ...]:
        _validate_positive_ids(owner_user_id, persona_session_id)
        rows = self._connection.execute(
            """
            SELECT extraction.*
            FROM memory_extraction_run AS extraction
            JOIN session ON session.id = extraction.session_id
            WHERE extraction.owner_user_id = ?
                AND session.owner_user_id = ?
                AND session.persona_session_id = ?
            ORDER BY extraction.id ASC
            """,
            (owner_user_id, owner_user_id, persona_session_id),
        ).fetchall()
        return tuple(_extraction_run_from_row(row) for row in rows)

    def extraction_lane_ids_for_user(self, *, owner_user_id: int) -> tuple[int, ...]:
        _validate_positive_ids(owner_user_id)
        rows = self._connection.execute(
            """
            SELECT DISTINCT session.persona_session_id
            FROM memory_extraction_run AS extraction
            JOIN session ON session.id = extraction.session_id
            WHERE extraction.owner_user_id = ? AND session.owner_user_id = ?
            ORDER BY session.persona_session_id ASC
            """,
            (owner_user_id, owner_user_id),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def claim_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
    ) -> MemoryExtractionRun:
        return self._transition_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
            expected_status=ExtractionRunStatus.PENDING,
            status=ExtractionRunStatus.PROCESSING,
            attempt_increment=1,
            error_kind=None,
            conflict_message="extraction run is not claimable",
        )

    def retry_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
        max_attempts: int,
    ) -> MemoryExtractionRun:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        run = self.get_extraction_run(run_id, owner_user_id=owner_user_id)
        if run is None:
            raise KeyError("extraction run not found for owner")
        if run.status is not ExtractionRunStatus.FAILED:
            raise RuntimeError("only failed extraction runs can be retried")
        if run.attempt >= max_attempts:
            raise RuntimeError("extraction retry budget is exhausted")
        return self._transition_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
            expected_status=ExtractionRunStatus.FAILED,
            status=ExtractionRunStatus.PENDING,
            attempt_increment=0,
            error_kind=None,
            conflict_message="extraction run is not retryable",
        )

    def complete_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
    ) -> MemoryExtractionRun:
        return self._transition_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
            expected_status=ExtractionRunStatus.PROCESSING,
            status=ExtractionRunStatus.COMPLETED,
            attempt_increment=0,
            error_kind=None,
            conflict_message="extraction run is terminal or not processing",
        )

    def fail_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
        error_kind: str,
    ) -> MemoryExtractionRun:
        if not isinstance(error_kind, str) or _EXTRACTION_ERROR_KIND.fullmatch(error_kind) is None:
            raise ValueError("extraction error_kind must be a bounded identifier")
        return self._transition_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
            expected_status=ExtractionRunStatus.PROCESSING,
            status=ExtractionRunStatus.FAILED,
            attempt_increment=0,
            error_kind=error_kind,
            conflict_message="extraction run is terminal or not processing",
        )

    def discard_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
    ) -> MemoryExtractionRun:
        return self._transition_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
            expected_status=ExtractionRunStatus.PROCESSING,
            status=ExtractionRunStatus.DISCARDED,
            attempt_increment=0,
            error_kind=None,
            conflict_message="extraction run is terminal or not processing",
        )

    def recover_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
    ) -> MemoryExtractionRun:
        return self._transition_extraction_run(
            run_id,
            owner_user_id=owner_user_id,
            expected_status=ExtractionRunStatus.PROCESSING,
            status=ExtractionRunStatus.PENDING,
            attempt_increment=0,
            error_kind=None,
            conflict_message="extraction run is not recoverable",
        )

    def has_blocking_extraction_runs(
        self,
        *,
        owner_user_id: int,
        session_id: int,
    ) -> bool:
        _validate_positive_ids(owner_user_id, session_id)
        row = self._connection.execute(
            """
            SELECT 1
            FROM memory_extraction_run AS extraction
            JOIN session AS active_session
                ON active_session.id = extraction.session_id
            WHERE extraction.session_id = ?
                AND extraction.owner_user_id = ?
                AND active_session.owner_user_id = ?
                AND extraction.status IN ('pending', 'processing', 'failed')
            LIMIT 1
            """,
            (session_id, owner_user_id, owner_user_id),
        ).fetchone()
        return row is not None

    def get_relationship(
        self,
        *,
        user_id: int,
        persona_id: int,
    ) -> Relationship | None:
        row = self._connection.execute(
            """
            SELECT id, user_id, persona_id, summary, state_json, version, updated_at
            FROM relationship
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()
        return None if row is None else self._relationship_from_row(row)

    def update_relationship(
        self,
        *,
        user_id: int,
        persona_id: int,
        summary: str,
        state_json: str,
        expected_version: int | None = None,
    ) -> Relationship:
        json.loads(state_json)
        parameters: list[object] = [
            summary,
            state_json,
            self._timestamp(),
            user_id,
            persona_id,
        ]
        version_clause = ""
        if expected_version is not None:
            version_clause = " AND version = ?"
            parameters.append(expected_version)
        with self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE relationship
                SET summary = ?, state_json = ?, version = version + 1, updated_at = ?
                WHERE user_id = ? AND persona_id = ?{version_clause}
                """,
                parameters,
            )
        if cursor.rowcount == 0:
            current = self.get_relationship(user_id=user_id, persona_id=persona_id)
            if current is None:
                raise KeyError("relationship not found")
            raise RelationshipVersionConflict("relationship version changed")
        updated = self.get_relationship(user_id=user_id, persona_id=persona_id)
        if updated is None:
            raise RuntimeError("relationship disappeared after update")
        return updated

    def create(self, memory: NewMemory) -> MemoryRecord:
        self._normalize_memory_metadata(memory)
        created_at = memory.created_at or self._timestamp()
        with self._connection:
            row = self._create_in_transaction(memory, timestamp=created_at)
        return self._memory_from_row(row)

    def _create_in_transaction(
        self,
        memory: NewMemory,
        *,
        timestamp: str,
    ) -> sqlite3.Row:
        scope = MemoryScope(memory.scope)
        source = MemorySource(memory.source)
        category = None if memory.category is None else MemoryCategory(memory.category)
        created_at = memory.created_at or timestamp
        updated_at = memory.updated_at or created_at
        self._connection.execute(
            """
            INSERT OR IGNORE INTO user_profile (user_id, created_at)
            VALUES (?, ?)
            """,
            (memory.user_id, created_at),
        )
        if source is MemorySource.AUTOMATIC:
            existing = self._connection.execute(
                """
                SELECT
                    id, user_id, scope, persona_id, relationship_id, kind,
                    content, provenance_session_id, status, created_at,
                    updated_at, version, source, category, confidence,
                    provenance_turn_id
                FROM memory
                WHERE user_id = ? AND scope = ? AND persona_id IS ?
                    AND relationship_id IS ? AND kind = ? AND content = ?
                    AND provenance_session_id IS ? AND status = 'active'
                    AND source = 'automatic' AND category = ?
                    AND confidence IS ? AND provenance_turn_id = ?
                """,
                (
                    memory.user_id,
                    scope.value,
                    memory.persona_id,
                    memory.relationship_id,
                    memory.kind,
                    memory.content,
                    memory.provenance_session_id,
                    None if category is None else category.value,
                    memory.confidence,
                    memory.provenance_turn_id,
                ),
            ).fetchone()
            if existing is not None:
                return existing
        cursor = self._connection.execute(
            """
            INSERT INTO memory (
                user_id, scope, persona_id, relationship_id, kind, content,
                provenance_session_id, status, created_at, updated_at,
                version, source, category, confidence, provenance_turn_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                memory.user_id,
                scope.value,
                memory.persona_id,
                memory.relationship_id,
                memory.kind,
                memory.content,
                memory.provenance_session_id,
                created_at,
                updated_at,
                source.value,
                None if category is None else category.value,
                memory.confidence,
                memory.provenance_turn_id,
            ),
        )
        row = self._select_memory(cursor.lastrowid, memory.user_id)
        if row is None:
            raise RuntimeError("memory record was not created")
        return row

    @staticmethod
    def _normalize_memory_metadata(memory: NewMemory) -> None:
        MemoryScope(memory.scope)
        source = MemorySource(memory.source)
        category = None if memory.category is None else MemoryCategory(memory.category)
        _validate_memory_metadata(
            source=source,
            category=category,
            confidence=memory.confidence,
            provenance_turn_id=memory.provenance_turn_id,
        )

    def get(self, memory_id: int, *, user_id: int) -> MemoryRecord | None:
        row = self._select_memory(memory_id, user_id)
        return None if row is None else self._memory_from_row(row)

    def list_for_user(
        self,
        *,
        user_id: int,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[MemoryRecord, ...]:
        if page < 1:
            raise ValueError("memory page must be positive")
        if page_size < 1:
            raise ValueError("memory page size must be positive")
        offset = (page - 1) * page_size
        rows = self._connection.execute(
            """
            SELECT
                id, user_id, scope, persona_id, relationship_id, kind, content,
                provenance_session_id, status, created_at, updated_at,
                version, source, category, confidence, provenance_turn_id
            FROM memory
            WHERE user_id = ? AND status = 'active'
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, page_size, offset),
        ).fetchall()
        return tuple(self._memory_from_row(row) for row in rows)

    def count_for_user(self, *, user_id: int) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM memory
            WHERE user_id = ? AND status = 'active'
            """,
            (user_id,),
        ).fetchone()
        return int(row["total"])

    def update(
        self,
        memory_id: int,
        *,
        user_id: int,
        content: str,
        kind: str | None = None,
        category: MemoryCategory | str | None = None,
        confidence: float | None = None,
        expected_version: int | None = None,
    ) -> MemoryRecord:
        current = self.get(memory_id, user_id=user_id)
        if current is None:
            raise KeyError("memory record not found")
        selected_category = (
            current.category if category is None else MemoryCategory(category)
        )
        selected_confidence = (
            current.confidence if confidence is None else confidence
        )
        _validate_memory_metadata(
            source=current.source,
            category=selected_category,
            confidence=selected_confidence,
            provenance_turn_id=current.provenance_turn_id,
        )
        expected = current.version if expected_version is None else expected_version
        timestamp = self._timestamp()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO user_profile (user_id, created_at)
                VALUES (?, ?)
                """,
                (user_id, timestamp),
            )
            cursor = self._connection.execute(
                """
                UPDATE memory
                SET kind = ?, content = ?, category = ?, confidence = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'active' AND version = ?
                """,
                (
                    current.kind if kind is None else kind,
                    content,
                    None if selected_category is None else selected_category.value,
                    selected_confidence,
                    timestamp,
                    memory_id,
                    user_id,
                    expected,
                ),
            )
            if cursor.rowcount == 0:
                raise MemoryVersionConflict("memory version changed")
            updated = self._connection.execute(
                """
                SELECT
                    id, user_id, scope, persona_id, relationship_id, kind, content,
                    provenance_session_id, status, created_at, updated_at,
                    version, source, category, confidence, provenance_turn_id
                FROM memory WHERE id = ?
                """,
                (memory_id,),
            ).fetchone()
            if updated is None:
                raise RuntimeError("memory record disappeared after update")
            self._connection.execute(
                """
                INSERT INTO memory_revision (
                    memory_id, owner_user_id, version, content, category,
                    confidence, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    user_id,
                    int(updated["version"]),
                    str(updated["content"]),
                    updated["category"],
                    updated["confidence"],
                    timestamp,
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self._memory_from_row(updated)

    def list_revisions(
        self,
        memory_id: int,
        *,
        user_id: int,
    ) -> tuple[MemoryRevision, ...]:
        _validate_positive_ids(memory_id, user_id)
        rows = self._connection.execute(
            """
            SELECT id, memory_id, owner_user_id, version, content, category,
                confidence, changed_at
            FROM memory_revision
            WHERE memory_id = ? AND owner_user_id = ?
            ORDER BY version ASC
            """,
            (memory_id, user_id),
        ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def delete(self, memory_id: int, *, user_id: int) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memory WHERE id = ? AND user_id = ?",
                (memory_id, user_id),
            )
        return cursor.rowcount == 1

    def delete_by_provenance_turn(
        self,
        source_turn_id: int,
        *,
        user_id: int,
    ) -> int:
        _validate_positive_ids(source_turn_id, user_id)
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM memory
                WHERE provenance_turn_id = ? AND user_id = ?
                """,
                (source_turn_id, user_id),
            )
        return cursor.rowcount

    def promote_to_shared(self, memory_id: int, *, user_id: int) -> MemoryRecord:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE memory
                SET scope = 'shared', persona_id = NULL, relationship_id = NULL,
                    updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'active'
                """,
                (self._timestamp(), memory_id, user_id),
            )
        if cursor.rowcount == 0:
            raise KeyError("memory record not found")
        promoted = self.get(memory_id, user_id=user_id)
        if promoted is None:
            raise RuntimeError("memory record disappeared after promotion")
        return promoted

    def list_for_context(
        self,
        *,
        user_id: int,
        persona_id: int,
        limits: MemoryLimits | None = None,
    ) -> MemoryContext:
        selected_limits = limits or MemoryLimits()
        shared = self._list_records(
            "m.user_id = ? AND m.scope = 'shared'",
            (user_id,),
            selected_limits.shared,
        )
        persona_private = self._list_records(
            "m.user_id = ? AND m.scope = 'persona_private' AND m.persona_id = ?",
            (user_id, persona_id),
            selected_limits.persona_private,
        )
        relationship = self._list_records(
            """
            m.user_id = ? AND m.scope = 'relationship'
            AND EXISTS (
                SELECT 1
                FROM relationship AS r
                WHERE r.id = m.relationship_id
                    AND r.user_id = m.user_id
                    AND r.persona_id = ?
            )
            """,
            (user_id, persona_id),
            selected_limits.relationship,
        )
        all_records = sorted(
            shared + persona_private + relationship,
            key=lambda record: (record.updated_at, record.id),
            reverse=True,
        )[: selected_limits.total]
        included_ids = {record.id for record in all_records}
        return MemoryContext(
            shared=tuple(record for record in shared if record.id in included_ids),
            persona_private=tuple(
                record for record in persona_private if record.id in included_ids
            ),
            relationship=tuple(
                record for record in relationship if record.id in included_ids
            ),
            relationship_state=self.get_relationship(
                user_id=user_id,
                persona_id=persona_id,
            ),
        )

    def memory_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM memory").fetchone()[0])

    def relationship_count(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(*) FROM relationship").fetchone()[0]
        )

    def close(self) -> None:
        self._connection.close()

    def _transition_extraction_run(
        self,
        run_id: int,
        *,
        owner_user_id: int,
        expected_status: ExtractionRunStatus,
        status: ExtractionRunStatus,
        attempt_increment: int,
        error_kind: str | None,
        conflict_message: str,
    ) -> MemoryExtractionRun:
        _validate_positive_ids(run_id, owner_user_id)
        timestamp = self._timestamp()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT extraction.*, profile.lifecycle_epoch AS current_epoch,
                    profile.lifecycle_state AS current_state
                FROM memory_extraction_run AS extraction
                JOIN user_profile AS profile
                    ON profile.user_id = extraction.owner_user_id
                WHERE extraction.id = ? AND extraction.owner_user_id = ?
                """,
                (run_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise KeyError("extraction run not found for owner")
            if (
                row["current_state"] != "active"
                or int(row["current_epoch"]) != int(row["lifecycle_epoch"])
            ):
                raise RuntimeError("extraction run is stale")
            if row["status"] != expected_status.value:
                raise RuntimeError(conflict_message)
            self._connection.execute(
                """
                UPDATE memory_extraction_run
                SET status = ?, attempt = attempt + ?, error_kind = ?, updated_at = ?
                WHERE id = ? AND owner_user_id = ? AND status = ?
                """,
                (
                    status.value,
                    attempt_increment,
                    error_kind,
                    timestamp,
                    run_id,
                    owner_user_id,
                    expected_status.value,
                ),
            )
            updated = self._connection.execute(
                "SELECT * FROM memory_extraction_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        if updated is None:
            raise RuntimeError("extraction run disappeared after transition")
        return _extraction_run_from_row(updated)

    def _list_records(
        self,
        where_clause: str,
        parameters: tuple[object, ...],
        limit: int,
    ) -> list[MemoryRecord]:
        rows = self._connection.execute(
            f"""
            SELECT
                m.id, m.user_id, m.scope, m.persona_id, m.relationship_id,
                m.kind, m.content, m.provenance_session_id, m.status,
                m.created_at, m.updated_at, m.version, m.source, m.category,
                m.confidence, m.provenance_turn_id
            FROM memory AS m
            WHERE m.status = 'active' AND {where_clause}
            ORDER BY m.updated_at DESC, m.id DESC
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def _select_memory(self, memory_id: int, user_id: int) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT
                id, user_id, scope, persona_id, relationship_id, kind, content,
                provenance_session_id, status, created_at, updated_at,
                version, source, category, confidence, provenance_turn_id
            FROM memory
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (memory_id, user_id),
        ).fetchone()

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            scope=MemoryScope(row["scope"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            persona_id=row["persona_id"],
            relationship_id=row["relationship_id"],
            provenance_session_id=row["provenance_session_id"],
            status=MemoryStatus(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=int(row["version"]),
            source=MemorySource(row["source"]),
            category=(
                None
                if row["category"] is None
                else MemoryCategory(row["category"])
            ),
            confidence=(
                None if row["confidence"] is None else float(row["confidence"])
            ),
            provenance_turn_id=row["provenance_turn_id"],
        )

    @staticmethod
    def _relationship_from_row(row: sqlite3.Row) -> Relationship:
        return Relationship(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            persona_id=int(row["persona_id"]),
            summary=str(row["summary"]),
            state_json=str(row["state_json"]),
            version=int(row["version"]),
            updated_at=str(row["updated_at"]),
        )

    def _timestamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_positive_ids(*values: int) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
        raise ValueError("memory extraction identifiers must be positive integers")


def _validate_memory_metadata(
    *,
    source: MemorySource,
    category: MemoryCategory | None,
    confidence: float | None,
    provenance_turn_id: int | None,
) -> None:
    if source is MemorySource.AUTOMATIC and category is None:
        raise ValueError("automatic memory requires a category")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (float, int))
        or not 0.0 <= confidence <= 1.0
    ):
        raise ValueError("memory confidence must be between 0 and 1")
    if provenance_turn_id is not None:
        _validate_positive_ids(provenance_turn_id)
    if source is MemorySource.AUTOMATIC and provenance_turn_id is None:
        raise ValueError("automatic memory requires turn provenance")


def _revision_from_row(row: sqlite3.Row) -> MemoryRevision:
    return MemoryRevision(
        id=int(row["id"]),
        memory_id=int(row["memory_id"]),
        owner_user_id=int(row["owner_user_id"]),
        version=int(row["version"]),
        content=str(row["content"]),
        category=(
            None if row["category"] is None else MemoryCategory(row["category"])
        ),
        confidence=(None if row["confidence"] is None else float(row["confidence"])),
        changed_at=str(row["changed_at"]),
    )


def _extraction_run_from_row(row: sqlite3.Row) -> MemoryExtractionRun:
    return MemoryExtractionRun(
        id=int(row["id"]),
        owner_user_id=int(row["owner_user_id"]),
        session_id=int(row["session_id"]),
        source_turn_id=int(row["source_turn_id"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        status=ExtractionRunStatus(str(row["status"])),
        attempt=int(row["attempt"]),
        error_kind=(None if row["error_kind"] is None else str(row["error_kind"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
