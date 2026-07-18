from dataclasses import dataclass
from pathlib import Path

from .sqlite_schema import open_state_database


@dataclass(frozen=True, slots=True)
class TurnDeletionResult:
    turn_id: int
    deleted_memory_count: int
    invalidated_summary: bool
    deleted_turn_count: int


class SQLiteSessionDataDeletionService:
    def __init__(self, database_path: Path | str) -> None:
        self._connection = open_state_database(database_path)

    def delete_turn(
        self,
        *,
        owner_user_id: int,
        session_id: int,
        turn_id: int,
    ) -> TurnDeletionResult:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            turn = self._connection.execute(
                """
                SELECT turn.id, turn.role, turn.sequence
                FROM transcript_turn AS turn
                JOIN session AS active_session
                    ON active_session.id = turn.session_id
                WHERE turn.id = ? AND turn.session_id = ?
                    AND active_session.owner_user_id = ?
                    AND active_session.status = 'active'
                """,
                (turn_id, session_id, owner_user_id),
            ).fetchone()
            if turn is None:
                raise KeyError("transcript turn not found for owner")
            deleted_turn_ids = [int(turn["id"])]
            if turn["role"] == "user":
                assistant = self._connection.execute(
                    """
                    SELECT id
                    FROM transcript_turn
                    WHERE session_id = ? AND sequence = ? AND role = 'assistant'
                    """,
                    (session_id, int(turn["sequence"]) + 1),
                ).fetchone()
                if assistant is not None:
                    deleted_turn_ids.append(int(assistant["id"]))
            deleted_memory = self._connection.execute(
                """
                DELETE FROM memory
                WHERE user_id = ? AND provenance_turn_id = ?
                """,
                (owner_user_id, turn_id),
            )
            self._connection.execute(
                """
                DELETE FROM memory_extraction_run
                WHERE owner_user_id = ? AND session_id = ? AND source_turn_id = ?
                """,
                (owner_user_id, session_id, turn_id),
            )
            invalidated_summary = self._connection.execute(
                """
                UPDATE session_summary
                SET status = 'invalidated'
                WHERE session_id = ? AND owner_user_id = ? AND status = 'active'
                """,
                (session_id, owner_user_id),
            ).rowcount == 1
            for deleted_turn_id in deleted_turn_ids:
                self._connection.execute(
                    "DELETE FROM transcript_turn WHERE id = ? AND session_id = ?",
                    (deleted_turn_id, session_id),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return TurnDeletionResult(
            turn_id=turn_id,
            deleted_memory_count=deleted_memory.rowcount,
            invalidated_summary=invalidated_summary,
            deleted_turn_count=len(deleted_turn_ids),
        )

    def close(self) -> None:
        self._connection.close()
