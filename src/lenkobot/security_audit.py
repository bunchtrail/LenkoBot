from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .sqlite_schema import open_state_database


@dataclass(frozen=True, slots=True)
class SecurityAuditEvent:
    owner_user_id: int
    lifecycle_epoch: int
    event_type: str
    capability: str
    action_hash: str | None
    policy_decision: str
    outcome: str
    started_at: str
    finished_at: str | None
    created_at: str | None = None


class SecurityAuditPort(Protocol):
    def record(self, event: SecurityAuditEvent) -> None: ...


class SQLiteSecurityAuditStore:
    def __init__(self, database_path: Path | str) -> None:
        self._connection = open_state_database(database_path)

    def record(self, event: SecurityAuditEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO security_audit (
                owner_user_id, lifecycle_epoch, event_type, action_hash,
                capability, policy_decision, outcome, started_at, finished_at,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.owner_user_id,
                event.lifecycle_epoch,
                event.event_type,
                event.action_hash,
                event.capability,
                event.policy_decision,
                event.outcome,
                event.started_at,
                event.finished_at,
                event.created_at or event.started_at,
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
