from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .sqlite_schema import open_state_database


PurgeHook = Callable[[int, int], None]
QuiesceHook = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class ResetResult:
    owner_user_id: int
    previous_epoch: int
    lifecycle_epoch: int


class ResetCoordinator:
    def __init__(
        self,
        database_path: Path | str,
        *,
        required_hooks: tuple[str, ...] = (),
    ) -> None:
        self._connection = open_state_database(database_path)
        self._required_hooks = frozenset(required_hooks)
        self._purge_hooks: dict[str, PurgeHook] = {}
        self._quiesce_hooks: list[QuiesceHook] = []

    def register_purge_hook(self, name: str, hook: PurgeHook) -> None:
        if not name or name in self._purge_hooks:
            raise ValueError("purge hook name is invalid or already registered")
        self._purge_hooks[name] = hook

    def register_quiesce_hook(self, hook: QuiesceHook) -> None:
        self._quiesce_hooks.append(hook)

    def reset(self, *, owner_user_id: int) -> ResetResult:
        missing = self._required_hooks.difference(self._purge_hooks)
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(f"reset purge hooks are not registered: {names}")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            profile = self._connection.execute(
                """
                SELECT lifecycle_epoch, lifecycle_state
                FROM user_profile WHERE user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
            if profile is None:
                raise KeyError("owner profile not found")
            if profile["lifecycle_state"] != "active":
                raise RuntimeError("reset is already in progress")
            previous_epoch = int(profile["lifecycle_epoch"])
            lifecycle_epoch = previous_epoch + 1
            self._connection.execute(
                """
                UPDATE user_profile
                SET lifecycle_epoch = ?, lifecycle_state = 'reset_in_progress'
                WHERE user_id = ? AND lifecycle_epoch = ?
                    AND lifecycle_state = 'active'
                """,
                (lifecycle_epoch, owner_user_id, previous_epoch),
            )
            self._connection.execute(
                "DELETE FROM security_audit WHERE owner_user_id = ?",
                (owner_user_id,),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

        for hook in self._quiesce_hooks:
            hook(owner_user_id, previous_epoch)
        for hook in self._purge_hooks.values():
            hook(owner_user_id, lifecycle_epoch)

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._purge_canonical_data(owner_user_id)
            self._connection.execute(
                """
                INSERT INTO security_audit (
                    owner_user_id, lifecycle_epoch, event_type, created_at
                ) VALUES (?, ?, 'reset_completed', datetime('now'))
                """,
                (owner_user_id, lifecycle_epoch),
            )
            self._connection.execute(
                """
                UPDATE user_profile
                SET lifecycle_state = 'active'
                WHERE user_id = ? AND lifecycle_epoch = ?
                    AND lifecycle_state = 'reset_in_progress'
                """,
                (owner_user_id, lifecycle_epoch),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return ResetResult(owner_user_id, previous_epoch, lifecycle_epoch)

    def _purge_canonical_data(self, owner_user_id: int) -> None:
        owned_lanes = self._connection.execute(
            """
            SELECT DISTINCT persona_session_id
            FROM session
            WHERE owner_user_id = ?
            """,
            (owner_user_id,),
        ).fetchall()
        owned_conversations = self._connection.execute(
            """
            SELECT DISTINCT lane.conversation_id
            FROM session AS owned_session
            JOIN persona_session AS lane
                ON lane.id = owned_session.persona_session_id
            WHERE owned_session.owner_user_id = ?
            """,
            (owner_user_id,),
        ).fetchall()
        self._connection.execute(
            "DELETE FROM memory WHERE user_id = ?",
            (owner_user_id,),
        )
        self._connection.execute(
            "DELETE FROM relationship WHERE user_id = ?",
            (owner_user_id,),
        )
        self._connection.execute(
            "DELETE FROM session WHERE owner_user_id = ?",
            (owner_user_id,),
        )
        for lane in owned_lanes:
            self._connection.execute(
                "DELETE FROM persona_session WHERE id = ?",
                (int(lane["persona_session_id"]),),
            )
        for conversation in owned_conversations:
            self._connection.execute(
                """
                DELETE FROM conversation
                WHERE id = ? AND NOT EXISTS (
                    SELECT 1 FROM persona_session WHERE conversation_id = conversation.id
                )
                """,
                (int(conversation["conversation_id"]),),
            )
        self._connection.execute(
            """
            UPDATE user_profile
            SET timezone = 'UTC'
            WHERE user_id = ?
            """,
            (owner_user_id,),
        )

    def close(self) -> None:
        self._connection.close()
