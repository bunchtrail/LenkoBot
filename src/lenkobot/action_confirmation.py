from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import secrets

from .sqlite_schema import open_state_database


DEFAULT_CONFIRMATION_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ConfirmationAction:
    token: str
    action_type: str
    payload: dict


class ConfirmationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ConfirmationResolution:
    action: ConfirmationAction
    outcome: ConfirmationOutcome
    first_resolution: bool


def _payload_hash(owner_user_id: int, action_type: str, payload_json: str) -> str:
    material = f"{owner_user_id}\n{action_type}\n{payload_json}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class SQLiteActionConfirmationStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("confirmation ttl must be positive")
        self._connection = open_state_database(database_path)
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create(
        self,
        *,
        owner_user_id: int,
        action_type: str,
        payload: dict,
    ) -> str:
        if (
            isinstance(owner_user_id, bool)
            or not isinstance(owner_user_id, int)
            or owner_user_id <= 0
        ):
            raise ValueError("confirmation owner must be a positive integer")
        if not isinstance(action_type, str) or not action_type.strip():
            raise ValueError("confirmation action type cannot be empty")
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = self._now()
        expires_at = now + self._ttl
        token = secrets.token_urlsafe(16)
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM action_confirmation
                WHERE owner_user_id = ? AND expires_at <= ?
                """,
                (owner_user_id, _format_timestamp(now)),
            )
            self._connection.execute(
                """
                INSERT INTO action_confirmation (
                    token, owner_user_id, action_type, payload_json,
                    payload_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    owner_user_id,
                    action_type,
                    payload_json,
                    _payload_hash(owner_user_id, action_type, payload_json),
                    _format_timestamp(now),
                    _format_timestamp(expires_at),
                ),
            )
        return token

    def consume(self, *, token: str, owner_user_id: int) -> ConfirmationAction | None:
        resolution = self.resolve(
            token=token,
            owner_user_id=owner_user_id,
            outcome=ConfirmationOutcome.CONFIRMED,
        )
        if resolution is None or not resolution.first_resolution:
            return None
        return resolution.action

    def resolve(
        self,
        *,
        token: str,
        owner_user_id: int,
        outcome: ConfirmationOutcome,
    ) -> ConfirmationResolution | None:
        try:
            normalized_outcome = ConfirmationOutcome(outcome)
        except (TypeError, ValueError):
            raise ValueError("confirmation outcome is invalid") from None
        now_iso = _format_timestamp(self._now())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                """
                SELECT owner_user_id, action_type, payload_json, payload_hash,
                       expires_at, consumed_at, outcome, resolved_at
                FROM action_confirmation
                WHERE token = ?
                """,
                (token,),
            ).fetchone()
            if (
                row is None
                or row["owner_user_id"] != owner_user_id
                or row["payload_hash"]
                != _payload_hash(owner_user_id, row["action_type"], row["payload_json"])
            ):
                self._connection.rollback()
                return None
            action = ConfirmationAction(
                token=token,
                action_type=row["action_type"],
                payload=json.loads(row["payload_json"]),
            )
            if row["outcome"] is not None:
                self._connection.commit()
                if row["outcome"] != normalized_outcome.value:
                    return None
                return ConfirmationResolution(
                    action=action,
                    outcome=normalized_outcome,
                    first_resolution=False,
                )
            if row["consumed_at"] is not None or row["expires_at"] <= now_iso:
                self._connection.rollback()
                return None
            cursor = self._connection.execute(
                """
                UPDATE action_confirmation
                SET consumed_at = ?, outcome = ?, resolved_at = ?
                WHERE token = ? AND consumed_at IS NULL AND outcome IS NULL
                """,
                (now_iso, normalized_outcome.value, now_iso, token),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return None
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return ConfirmationResolution(
            action=action,
            outcome=normalized_outcome,
            first_resolution=True,
        )

    def close(self) -> None:
        self._connection.close()

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


class ActionConfirmationService:
    def __init__(self, store: SQLiteActionConfirmationStore) -> None:
        self._store = store

    def request(
        self,
        *,
        owner_user_id: int,
        action_type: str,
        payload: dict,
    ) -> str:
        return self._store.create(
            owner_user_id=owner_user_id,
            action_type=action_type,
            payload=payload,
        )

    def resolve(
        self,
        *,
        token: str,
        owner_user_id: int,
        confirmed: bool,
    ) -> ConfirmationResolution | None:
        if not isinstance(confirmed, bool):
            raise ValueError("confirmation decision must be boolean")
        return self._store.resolve(
            token=token,
            owner_user_id=owner_user_id,
            outcome=(
                ConfirmationOutcome.CONFIRMED
                if confirmed
                else ConfirmationOutcome.CANCELLED
            ),
        )
