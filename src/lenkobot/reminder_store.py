from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
import re
import secrets
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .reminder_schedule import (
    LocalTimeResolutionError,
    RecurrenceRule,
    next_occurrence,
    quiet_hours_available_at,
    resolve_local_time,
)
from .sqlite_schema import open_state_database


DEFAULT_GRACE_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 60
MAX_DELIVERY_ATTEMPTS = 3
_ERROR_KIND = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class TaskStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReminderJobStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReminderRunStatus(StrEnum):
    DUE = "due"
    MISSED = "missed"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReminderDraft:
    owner_user_id: int
    persona_id: int
    chat_id: int
    text: str
    local_start: datetime
    timezone_name: str
    recurrence: RecurrenceRule
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    quiet_start_minute: int | None = None
    quiet_end_minute: int | None = None
    urgent: bool = False


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    owner_user_id: int
    persona_id: int
    chat_id: int
    lifecycle_epoch: int
    text: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReminderJob:
    id: int
    task_id: int
    owner_user_id: int
    lifecycle_epoch: int
    status: ReminderJobStatus
    timezone_name: str
    local_start: datetime
    recurrence: RecurrenceRule
    next_scheduled_for: datetime | None
    emitted_count: int
    grace_seconds: int
    quiet_start_minute: int | None
    quiet_end_minute: int | None
    urgent: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DraftReminder:
    task: TaskRecord
    job: ReminderJob


@dataclass(frozen=True, slots=True)
class ProfileReminderPolicy:
    owner_user_id: int
    timezone_name: str
    quiet_start_minute: int | None
    quiet_end_minute: int | None


@dataclass(frozen=True, slots=True)
class ReminderRun:
    id: int
    job_id: int
    owner_user_id: int
    lifecycle_epoch: int
    scheduled_for: datetime
    status: ReminderRunStatus
    claimed_at: datetime | None
    delivered_at: datetime | None
    error_kind: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    id: int
    run_id: int
    owner_user_id: int
    lifecycle_epoch: int
    chat_id: int
    text: str
    scheduled_for: datetime
    available_at: datetime
    status: OutboxStatus
    attempt: int
    lease_token: str | None
    lease_until: datetime | None
    telegram_message_id: int | None
    error_kind: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryLease:
    outbox_id: int
    run_id: int
    task_id: int
    owner_user_id: int
    lifecycle_epoch: int
    chat_id: int
    text: str
    scheduled_for: datetime
    attempt: int
    lease_token: str
    lease_until: datetime


class SQLiteReminderStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection = open_state_database(database_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_draft(self, draft: ReminderDraft) -> DraftReminder:
        _validate_draft(draft)
        now = self._now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            profile = self._active_profile(draft.owner_user_id)
            persona = self._connection.execute(
                "SELECT id FROM persona WHERE id = ? AND status = 'active'",
                (draft.persona_id,),
            ).fetchone()
            if persona is None:
                raise ValueError("active reminder persona does not exist")
            task_cursor = self._connection.execute(
                """
                INSERT INTO task (
                    owner_user_id, persona_id, chat_id, lifecycle_epoch, text,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'awaiting_confirmation', ?, ?)
                """,
                (
                    draft.owner_user_id,
                    draft.persona_id,
                    draft.chat_id,
                    int(profile["lifecycle_epoch"]),
                    draft.text.strip(),
                    _format_timestamp(now),
                    _format_timestamp(now),
                ),
            )
            job_cursor = self._connection.execute(
                """
                INSERT INTO reminder_job (
                    task_id, owner_user_id, lifecycle_epoch, status,
                    timezone_name, local_start, recurrence_json,
                    next_scheduled_for, grace_seconds, quiet_start_minute,
                    quiet_end_minute, urgent, created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(task_cursor.lastrowid),
                    draft.owner_user_id,
                    int(profile["lifecycle_epoch"]),
                    draft.timezone_name.strip(),
                    draft.local_start.isoformat(),
                    draft.recurrence.to_json(),
                    draft.grace_seconds,
                    draft.quiet_start_minute,
                    draft.quiet_end_minute,
                    int(draft.urgent),
                    _format_timestamp(now),
                    _format_timestamp(now),
                ),
            )
            task = self._task_by_id(int(task_cursor.lastrowid))
            job = self._job_by_id(int(job_cursor.lastrowid))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return DraftReminder(_task_from_row(task), _job_from_row(job))

    def mark_awaiting_confirmation(
        self,
        *,
        task_id: int,
        owner_user_id: int,
    ) -> ReminderJob:
        now = self._now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            task, job = self._owned_task_and_job(task_id, owner_user_id)
            status = ReminderJobStatus(str(job["status"]))
            if status is ReminderJobStatus.DRAFT:
                self._connection.execute(
                    """
                    UPDATE reminder_job
                    SET status = 'awaiting_confirmation', updated_at = ?
                    WHERE id = ? AND status = 'draft'
                    """,
                    (_format_timestamp(now), int(job["id"])),
                )
                job = self._job_by_id(int(job["id"]))
            elif status not in {
                ReminderJobStatus.AWAITING_CONFIRMATION,
                ReminderJobStatus.ACTIVE,
                ReminderJobStatus.NEEDS_REVIEW,
            }:
                raise RuntimeError("reminder job cannot await confirmation")
            self._ensure_current_epoch(task)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _job_from_row(job)

    def activate(self, *, task_id: int, owner_user_id: int) -> ReminderJob:
        now = self._now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            task, job = self._owned_task_and_job(task_id, owner_user_id)
            self._ensure_current_epoch(task)
            status = ReminderJobStatus(str(job["status"]))
            if status in {
                ReminderJobStatus.ACTIVE,
                ReminderJobStatus.NEEDS_REVIEW,
            }:
                self._connection.commit()
                return _job_from_row(job)
            if status is not ReminderJobStatus.AWAITING_CONFIRMATION:
                raise RuntimeError("reminder job is not awaiting confirmation")
            local_start = _parse_local(str(job["local_start"]))
            rule = RecurrenceRule.from_json(str(job["recurrence_json"]))
            first_local = next_occurrence(rule, anchor_local=local_start)
            try:
                first_utc = (
                    None
                    if first_local is None
                    else resolve_local_time(first_local, str(job["timezone_name"]))
                )
            except LocalTimeResolutionError:
                self._connection.execute(
                    """
                    UPDATE reminder_job
                    SET status = 'needs_review', next_scheduled_for = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (_format_timestamp(now), int(job["id"])),
                )
                self._connection.execute(
                    """
                    UPDATE task SET status = 'needs_review', updated_at = ?
                    WHERE id = ?
                    """,
                    (_format_timestamp(now), task_id),
                )
            else:
                if first_utc is None:
                    raise RuntimeError("reminder recurrence has no first occurrence")
                self._connection.execute(
                    """
                    UPDATE reminder_job
                    SET status = 'active', next_scheduled_for = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _format_timestamp(first_utc),
                        _format_timestamp(now),
                        int(job["id"]),
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE task SET status = 'active', updated_at = ?
                    WHERE id = ?
                    """,
                    (_format_timestamp(now), task_id),
                )
            job = self._job_by_id(int(job["id"]))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _job_from_row(job)

    def get_task(self, *, task_id: int, owner_user_id: int) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM task WHERE id = ? AND owner_user_id = ?",
            (task_id, owner_user_id),
        ).fetchone()
        return None if row is None else _task_from_row(row)

    def materialize_due(self, *, limit: int = 100) -> tuple[ReminderRun, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("reminder materialization limit is invalid")
        now = self._now()
        timestamp = _format_timestamp(now)
        created: list[ReminderRun] = []
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            jobs = self._connection.execute(
                """
                SELECT job.*, task.chat_id, task.text
                FROM reminder_job AS job
                JOIN task ON task.id = job.task_id
                JOIN user_profile AS profile
                    ON profile.user_id = job.owner_user_id
                WHERE job.status = 'active'
                    AND job.next_scheduled_for <= ?
                    AND task.status = 'active'
                    AND task.lifecycle_epoch = job.lifecycle_epoch
                    AND profile.lifecycle_epoch = job.lifecycle_epoch
                    AND profile.lifecycle_state = 'active'
                ORDER BY job.next_scheduled_for ASC, job.id ASC
                LIMIT ?
                """,
                (timestamp, limit),
            ).fetchall()
            for job in jobs:
                scheduled_for = _parse_aware(str(job["next_scheduled_for"]))
                resolution_failed = False
                try:
                    available_at = quiet_hours_available_at(
                        scheduled_for,
                        timezone_name=str(job["timezone_name"]),
                        start_minute=(
                            None
                            if job["quiet_start_minute"] is None
                            else int(job["quiet_start_minute"])
                        ),
                        end_minute=(
                            None
                            if job["quiet_end_minute"] is None
                            else int(job["quiet_end_minute"])
                        ),
                        urgent=bool(job["urgent"]),
                    )
                except LocalTimeResolutionError:
                    available_at = scheduled_for
                    resolution_failed = True
                deadline = scheduled_for + timedelta(seconds=int(job["grace_seconds"]))
                missed = resolution_failed or now > deadline or available_at > deadline
                run_status = (
                    ReminderRunStatus.MISSED if missed else ReminderRunStatus.DUE
                )
                run_cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO reminder_run (
                        job_id, owner_user_id, lifecycle_epoch, scheduled_for,
                        status, error_kind, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(job["id"]),
                        int(job["owner_user_id"]),
                        int(job["lifecycle_epoch"]),
                        _format_timestamp(scheduled_for),
                        run_status.value,
                        "quiet_hours_invalid" if resolution_failed else None,
                        timestamp,
                        timestamp,
                    ),
                )
                if run_cursor.rowcount != 1:
                    continue
                run_id = int(run_cursor.lastrowid)
                self._connection.execute(
                    """
                    INSERT INTO delivery_outbox (
                        run_id, owner_user_id, lifecycle_epoch, chat_id, text,
                        scheduled_for, available_at, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        int(job["owner_user_id"]),
                        int(job["lifecycle_epoch"]),
                        int(job["chat_id"]),
                        str(job["text"]),
                        _format_timestamp(scheduled_for),
                        _format_timestamp(available_at),
                        (
                            OutboxStatus.CANCELLED.value
                            if missed
                            else OutboxStatus.PENDING.value
                        ),
                        timestamp,
                        timestamp,
                    ),
                )
                if resolution_failed:
                    self._mark_job_needs_review(job, timestamp)
                else:
                    self._advance_job_cursor(job, scheduled_for, timestamp)
                run = self._connection.execute(
                    "SELECT * FROM reminder_run WHERE id = ?",
                    (run_id,),
                ).fetchone()
                created.append(_run_from_row(run))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return tuple(created)

    def list_runs(
        self,
        *,
        job_id: int,
        owner_user_id: int,
    ) -> tuple[ReminderRun, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM reminder_run
            WHERE job_id = ? AND owner_user_id = ?
            ORDER BY scheduled_for ASC, id ASC
            """,
            (job_id, owner_user_id),
        ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def get_outbox(self, *, run_id: int, owner_user_id: int) -> OutboxEntry | None:
        row = self._connection.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE run_id = ? AND owner_user_id = ?
            """,
            (run_id, owner_user_id),
        ).fetchone()
        return None if row is None else _outbox_from_row(row)

    def lease_delivery(
        self,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> DeliveryLease | None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
            or lease_seconds > 3600
        ):
            raise ValueError("delivery lease duration is invalid")
        now = self._now()
        timestamp = _format_timestamp(now)
        lease_until = now + timedelta(seconds=lease_seconds)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            while True:
                row = self._connection.execute(
                    """
                    SELECT outbox.*, job.grace_seconds, job.task_id
                    FROM delivery_outbox AS outbox
                    JOIN reminder_run AS run ON run.id = outbox.run_id
                    JOIN reminder_job AS job ON job.id = run.job_id
                    JOIN user_profile AS profile
                        ON profile.user_id = outbox.owner_user_id
                    WHERE outbox.attempt < ?
                        AND outbox.available_at <= ?
                        AND (
                            outbox.status = 'pending'
                            OR (
                                outbox.status = 'leased'
                                AND outbox.lease_until <= ?
                            )
                        )
                        AND profile.lifecycle_epoch = outbox.lifecycle_epoch
                        AND profile.lifecycle_state = 'active'
                    ORDER BY outbox.available_at ASC, outbox.id ASC
                    LIMIT 1
                    """,
                    (MAX_DELIVERY_ATTEMPTS, timestamp, timestamp),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                scheduled_for = _parse_aware(str(row["scheduled_for"]))
                deadline = scheduled_for + timedelta(seconds=int(row["grace_seconds"]))
                if now > deadline:
                    self._connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status = 'cancelled', lease_token = NULL,
                            lease_until = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (timestamp, int(row["id"])),
                    )
                    self._connection.execute(
                        """
                        UPDATE reminder_run
                        SET status = 'missed', error_kind = 'grace_expired',
                            updated_at = ?
                        WHERE id = ? AND status IN ('due', 'claimed')
                        """,
                        (timestamp, int(row["run_id"])),
                    )
                    continue
                token = secrets.token_urlsafe(18)
                self._connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'leased', attempt = attempt + 1,
                        lease_token = ?, lease_until = ?, error_kind = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        token,
                        _format_timestamp(lease_until),
                        timestamp,
                        int(row["id"]),
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE reminder_run
                    SET status = 'claimed', claimed_at = ?, updated_at = ?
                    WHERE id = ? AND status IN ('due', 'claimed')
                    """,
                    (timestamp, timestamp, int(row["run_id"])),
                )
                leased = self._connection.execute(
                    """
                    SELECT outbox.*, job.task_id
                    FROM delivery_outbox AS outbox
                    JOIN reminder_run AS run ON run.id = outbox.run_id
                    JOIN reminder_job AS job ON job.id = run.job_id
                    WHERE outbox.id = ?
                    """,
                    (int(row["id"]),),
                ).fetchone()
                self._connection.commit()
                return _lease_from_row(leased)
        except Exception:
            self._connection.rollback()
            raise

    def delivery_is_current(self, lease: DeliveryLease) -> bool:
        row = self._connection.execute(
            """
            SELECT outbox.id
            FROM delivery_outbox AS outbox
            JOIN user_profile AS profile
                ON profile.user_id = outbox.owner_user_id
            WHERE outbox.id = ?
                AND outbox.owner_user_id = ?
                AND outbox.lifecycle_epoch = ?
                AND outbox.lease_token = ?
                AND outbox.status = 'leased'
                AND profile.lifecycle_epoch = outbox.lifecycle_epoch
                AND profile.lifecycle_state = 'active'
            """,
            (
                lease.outbox_id,
                lease.owner_user_id,
                lease.lifecycle_epoch,
                lease.lease_token,
            ),
        ).fetchone()
        return row is not None

    def mark_delivery_sent(
        self,
        lease: DeliveryLease,
        *,
        telegram_message_id: int,
    ) -> bool:
        if (
            isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id <= 0
        ):
            raise ValueError("Telegram message id must be positive")
        now = self._now()
        timestamp = _format_timestamp(now)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?",
                (lease.outbox_id,),
            ).fetchone()
            profile = self._connection.execute(
                """
                SELECT lifecycle_epoch, lifecycle_state FROM user_profile
                WHERE user_id = ?
                """,
                (lease.owner_user_id,),
            ).fetchone()
            current = (
                row is not None
                and int(row["owner_user_id"]) == lease.owner_user_id
                and int(row["lifecycle_epoch"]) == lease.lifecycle_epoch
                and str(row["lease_token"]) == lease.lease_token
                and profile is not None
                and int(profile["lifecycle_epoch"]) == lease.lifecycle_epoch
                and str(profile["lifecycle_state"]) == "active"
            )
            if current and str(row["status"]) == OutboxStatus.SENT.value:
                if int(row["telegram_message_id"]) != telegram_message_id:
                    raise RuntimeError("delivery was committed with another message id")
                self._connection.commit()
                return True
            if current and str(row["status"]) == OutboxStatus.LEASED.value:
                self._connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'sent', telegram_message_id = ?,
                        lease_until = NULL, updated_at = ?
                    WHERE id = ? AND lease_token = ? AND status = 'leased'
                    """,
                    (
                        telegram_message_id,
                        timestamp,
                        lease.outbox_id,
                        lease.lease_token,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE reminder_run
                    SET status = 'delivered', delivered_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, timestamp, lease.run_id),
                )
                self._connection.commit()
                return True
            self._record_external_commit(lease, timestamp)
            self._connection.commit()
            return False
        except Exception:
            self._connection.rollback()
            raise

    def release_delivery(
        self,
        lease: DeliveryLease,
        *,
        error_kind: str,
        retry_delay_seconds: int,
    ) -> bool:
        if not isinstance(error_kind, str) or _ERROR_KIND.fullmatch(error_kind) is None:
            raise ValueError("delivery error kind is invalid")
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int)
            or retry_delay_seconds < 1
            or retry_delay_seconds > 3600
        ):
            raise ValueError("delivery retry delay is invalid")
        now = self._now()
        timestamp = _format_timestamp(now)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT outbox.*, profile.lifecycle_epoch AS current_epoch,
                    profile.lifecycle_state
                FROM delivery_outbox AS outbox
                JOIN user_profile AS profile
                    ON profile.user_id = outbox.owner_user_id
                WHERE outbox.id = ? AND outbox.lease_token = ?
                    AND outbox.status = 'leased'
                """,
                (lease.outbox_id, lease.lease_token),
            ).fetchone()
            if (
                row is None
                or int(row["current_epoch"]) != lease.lifecycle_epoch
                or str(row["lifecycle_state"]) != "active"
            ):
                self._connection.rollback()
                return False
            terminal = int(row["attempt"]) >= MAX_DELIVERY_ATTEMPTS
            if terminal:
                outbox_status = OutboxStatus.FAILED.value
                run_status = ReminderRunStatus.FAILED.value
                available_at = str(row["available_at"])
            else:
                outbox_status = OutboxStatus.PENDING.value
                run_status = ReminderRunStatus.DUE.value
                available_at = _format_timestamp(
                    now + timedelta(seconds=retry_delay_seconds)
                )
            self._connection.execute(
                """
                UPDATE delivery_outbox
                SET status = ?, available_at = ?, lease_token = NULL,
                    lease_until = NULL, error_kind = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    outbox_status,
                    available_at,
                    error_kind,
                    timestamp,
                    lease.outbox_id,
                ),
            )
            self._connection.execute(
                """
                UPDATE reminder_run
                SET status = ?, error_kind = ?, updated_at = ?
                WHERE id = ?
                """,
                (run_status, error_kind, timestamp, lease.run_id),
            )
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def get_job(self, *, job_id: int, owner_user_id: int) -> ReminderJob | None:
        row = self._connection.execute(
            "SELECT * FROM reminder_job WHERE id = ? AND owner_user_id = ?",
            (job_id, owner_user_id),
        ).fetchone()
        return None if row is None else _job_from_row(row)

    def get_job_for_task(
        self,
        *,
        task_id: int,
        owner_user_id: int,
    ) -> ReminderJob | None:
        row = self._connection.execute(
            """
            SELECT * FROM reminder_job
            WHERE task_id = ? AND owner_user_id = ?
            ORDER BY id ASC LIMIT 1
            """,
            (task_id, owner_user_id),
        ).fetchone()
        return None if row is None else _job_from_row(row)

    def list_tasks(
        self,
        *,
        owner_user_id: int,
        limit: int = 100,
    ) -> tuple[TaskRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError("reminder task list limit is invalid")
        rows = self._connection.execute(
            """
            SELECT * FROM task
            WHERE owner_user_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (owner_user_id, limit),
        ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def ensure_profile(self, *, owner_user_id: int) -> ProfileReminderPolicy:
        if (
            isinstance(owner_user_id, bool)
            or not isinstance(owner_user_id, int)
            or owner_user_id <= 0
        ):
            raise ValueError("reminder owner must be a positive integer")
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO user_profile (user_id, created_at)
                VALUES (?, ?)
                """,
                (owner_user_id, _format_timestamp(self._now())),
            )
        return self.get_profile_policy(owner_user_id=owner_user_id)

    def persona_id_for_key(self, persona_key: str) -> int | None:
        if not isinstance(persona_key, str) or not persona_key.strip():
            raise ValueError("reminder persona key cannot be empty")
        row = self._connection.execute(
            """
            SELECT id FROM persona
            WHERE profile_id = 'default' AND key = ? AND status = 'active'
            """,
            (persona_key.strip(),),
        ).fetchone()
        return None if row is None else int(row["id"])

    def get_profile_policy(self, *, owner_user_id: int) -> ProfileReminderPolicy:
        row = self._connection.execute(
            """
            SELECT user_id, timezone, quiet_start_minute, quiet_end_minute
            FROM user_profile WHERE user_id = ?
            """,
            (owner_user_id,),
        ).fetchone()
        if row is None:
            raise KeyError("reminder owner profile does not exist")
        return _profile_policy_from_row(row)

    def set_profile_policy(
        self,
        *,
        owner_user_id: int,
        timezone_name: str,
        quiet_start_minute: int | None,
        quiet_end_minute: int | None,
    ) -> ProfileReminderPolicy:
        _validate_timezone(timezone_name)
        _validate_quiet_policy(quiet_start_minute, quiet_end_minute)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE user_profile
                SET timezone = ?, quiet_start_minute = ?, quiet_end_minute = ?
                WHERE user_id = ? AND lifecycle_state = 'active'
                """,
                (
                    timezone_name.strip(),
                    quiet_start_minute,
                    quiet_end_minute,
                    owner_user_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("active reminder owner profile does not exist")
        return self.get_profile_policy(owner_user_id=owner_user_id)

    def cancel_task(self, *, task_id: int, owner_user_id: int) -> TaskRecord:
        return self._finish_task(
            task_id=task_id,
            owner_user_id=owner_user_id,
            target=TaskStatus.CANCELLED,
        )

    def complete_task(self, *, task_id: int, owner_user_id: int) -> TaskRecord:
        return self._finish_task(
            task_id=task_id,
            owner_user_id=owner_user_id,
            target=TaskStatus.COMPLETED,
        )

    def snooze_task(
        self,
        *,
        task_id: int,
        owner_user_id: int,
        action_token: str,
        delay_seconds: int = 600,
    ) -> ReminderRun:
        if not isinstance(action_token, str) or not action_token.strip():
            raise ValueError("reminder snooze action token cannot be empty")
        if len(action_token) > 128:
            raise ValueError("reminder snooze action token is too long")
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, int)
            or delay_seconds < 60
            or delay_seconds > 604800
        ):
            raise ValueError("reminder snooze delay is invalid")
        now = self._now()
        timestamp = _format_timestamp(now)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                """
                SELECT run.*, job.task_id
                FROM reminder_run AS run
                JOIN reminder_job AS job ON job.id = run.job_id
                WHERE run.action_token = ?
                """,
                (action_token,),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["owner_user_id"]) != owner_user_id
                    or int(existing["task_id"]) != task_id
                ):
                    raise RuntimeError("reminder snooze action token was reused")
                self._connection.commit()
                return _run_from_row(existing)

            task, job = self._owned_task_and_job(task_id, owner_user_id)
            self._ensure_current_epoch(task)
            if TaskStatus(str(task["status"])) is not TaskStatus.ACTIVE:
                raise RuntimeError("only an active reminder can be snoozed")

            scheduled_for = now + timedelta(seconds=delay_seconds)
            available_at = quiet_hours_available_at(
                scheduled_for,
                timezone_name=str(job["timezone_name"]),
                start_minute=(
                    None
                    if job["quiet_start_minute"] is None
                    else int(job["quiet_start_minute"])
                ),
                end_minute=(
                    None
                    if job["quiet_end_minute"] is None
                    else int(job["quiet_end_minute"])
                ),
                urgent=bool(job["urgent"]),
            )
            missed = available_at > scheduled_for + timedelta(
                seconds=int(job["grace_seconds"])
            )
            run_status = (
                ReminderRunStatus.MISSED if missed else ReminderRunStatus.DUE
            )
            run_cursor = self._connection.execute(
                """
                INSERT INTO reminder_run (
                    job_id, owner_user_id, lifecycle_epoch, scheduled_for,
                    status, action_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(job["id"]),
                    owner_user_id,
                    int(job["lifecycle_epoch"]),
                    _format_timestamp(scheduled_for),
                    run_status.value,
                    action_token,
                    timestamp,
                    timestamp,
                ),
            )
            run_id = int(run_cursor.lastrowid)
            self._connection.execute(
                """
                INSERT INTO delivery_outbox (
                    run_id, owner_user_id, lifecycle_epoch, chat_id, text,
                    scheduled_for, available_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    owner_user_id,
                    int(job["lifecycle_epoch"]),
                    int(task["chat_id"]),
                    str(task["text"]),
                    _format_timestamp(scheduled_for),
                    _format_timestamp(available_at),
                    (
                        OutboxStatus.CANCELLED.value
                        if missed
                        else OutboxStatus.PENDING.value
                    ),
                    timestamp,
                    timestamp,
                ),
            )
            run = self._connection.execute(
                "SELECT * FROM reminder_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _run_from_row(run)

    def purge_owner(self, owner_user_id: int, lifecycle_epoch: int) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            profile = self._connection.execute(
                """
                SELECT lifecycle_epoch, lifecycle_state FROM user_profile
                WHERE user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
            if (
                profile is None
                or int(profile["lifecycle_epoch"]) != lifecycle_epoch
                or str(profile["lifecycle_state"]) != "reset_in_progress"
            ):
                raise RuntimeError("reminder purge lifecycle fence is invalid")
            self._connection.execute(
                "DELETE FROM task WHERE owner_user_id = ?",
                (owner_user_id,),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def close(self) -> None:
        self._connection.close()

    def _finish_task(
        self,
        *,
        task_id: int,
        owner_user_id: int,
        target: TaskStatus,
    ) -> TaskRecord:
        now = self._now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM task WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError("reminder task does not exist")
            if int(row["owner_user_id"]) != owner_user_id:
                raise PermissionError("reminder task belongs to another owner")
            self._ensure_current_epoch(row)
            current = TaskStatus(str(row["status"]))
            if current is target:
                self._connection.commit()
                return _task_from_row(row)
            if current in {TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
                raise RuntimeError("reminder task is already terminal")
            job_target = (
                ReminderJobStatus.CANCELLED
                if target is TaskStatus.CANCELLED
                else ReminderJobStatus.COMPLETED
            )
            timestamp = _format_timestamp(now)
            self._connection.execute(
                "UPDATE task SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, timestamp, task_id),
            )
            self._connection.execute(
                """
                UPDATE reminder_job SET status = ?, next_scheduled_for = NULL,
                    updated_at = ?
                WHERE task_id = ? AND status NOT IN ('cancelled', 'completed')
                """,
                (job_target.value, timestamp, task_id),
            )
            self._connection.execute(
                """
                UPDATE reminder_run SET status = 'cancelled', updated_at = ?
                WHERE job_id IN (SELECT id FROM reminder_job WHERE task_id = ?)
                    AND status IN ('due', 'claimed')
                """,
                (timestamp, task_id),
            )
            self._connection.execute(
                """
                UPDATE delivery_outbox SET status = 'cancelled', updated_at = ?,
                    lease_token = NULL, lease_until = NULL
                WHERE run_id IN (
                    SELECT run.id FROM reminder_run AS run
                    JOIN reminder_job AS job ON job.id = run.job_id
                    WHERE job.task_id = ?
                ) AND status IN ('pending', 'leased')
                """,
                (timestamp, task_id),
            )
            row = self._task_by_id(task_id)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return _task_from_row(row)

    def _advance_job_cursor(
        self,
        job: sqlite3.Row,
        scheduled_for: datetime,
        timestamp: str,
    ) -> None:
        rule = RecurrenceRule.from_json(str(job["recurrence_json"]))
        anchor = _parse_local(str(job["local_start"]))
        zone = ZoneInfo(str(job["timezone_name"]))
        current_local = scheduled_for.astimezone(zone).replace(tzinfo=None)
        emitted_count = int(job["emitted_count"]) + 1
        following_local = next_occurrence(
            rule,
            anchor_local=anchor,
            after_local=current_local,
            emitted_count=emitted_count,
        )
        if following_local is None:
            self._connection.execute(
                """
                UPDATE reminder_job
                SET status = 'completed', next_scheduled_for = NULL,
                    emitted_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (emitted_count, timestamp, int(job["id"])),
            )
            return
        try:
            following_utc = resolve_local_time(
                following_local,
                str(job["timezone_name"]),
            )
        except LocalTimeResolutionError:
            self._connection.execute(
                """
                UPDATE reminder_job
                SET status = 'needs_review', next_scheduled_for = NULL,
                    emitted_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (emitted_count, timestamp, int(job["id"])),
            )
            self._connection.execute(
                """
                UPDATE task SET status = 'needs_review', updated_at = ?
                WHERE id = ?
                """,
                (timestamp, int(job["task_id"])),
            )
            return
        self._connection.execute(
            """
            UPDATE reminder_job
            SET next_scheduled_for = ?, emitted_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                _format_timestamp(following_utc),
                emitted_count,
                timestamp,
                int(job["id"]),
            ),
        )

    def _mark_job_needs_review(self, job: sqlite3.Row, timestamp: str) -> None:
        self._connection.execute(
            """
            UPDATE reminder_job
            SET status = 'needs_review', next_scheduled_for = NULL,
                emitted_count = emitted_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, int(job["id"])),
        )
        self._connection.execute(
            """
            UPDATE task SET status = 'needs_review', updated_at = ?
            WHERE id = ?
            """,
            (timestamp, int(job["task_id"])),
        )

    def _record_external_commit(self, lease: DeliveryLease, timestamp: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO reminder_delivery_audit (
                owner_user_id, lifecycle_epoch, delivery_token,
                event_type, created_at
            ) VALUES (?, ?, ?, 'external_commit_after_reset', ?)
            """,
            (
                lease.owner_user_id,
                lease.lifecycle_epoch,
                lease.lease_token,
                timestamp,
            ),
        )

    def _active_profile(self, owner_user_id: int) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT lifecycle_epoch FROM user_profile
            WHERE user_id = ? AND lifecycle_state = 'active'
            """,
            (owner_user_id,),
        ).fetchone()
        if row is None:
            raise ValueError("active reminder owner profile does not exist")
        return row

    def _owned_task_and_job(
        self,
        task_id: int,
        owner_user_id: int,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        task = self._connection.execute(
            "SELECT * FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise KeyError("reminder task does not exist")
        if int(task["owner_user_id"]) != owner_user_id:
            raise PermissionError("reminder task belongs to another owner")
        job = self._connection.execute(
            "SELECT * FROM reminder_job WHERE task_id = ? ORDER BY id LIMIT 1",
            (task_id,),
        ).fetchone()
        if job is None:
            raise RuntimeError("reminder task has no job")
        return task, job

    def _ensure_current_epoch(self, task: sqlite3.Row) -> None:
        profile = self._active_profile(int(task["owner_user_id"]))
        if int(profile["lifecycle_epoch"]) != int(task["lifecycle_epoch"]):
            raise RuntimeError("reminder task lifecycle epoch is stale")

    def _task_by_id(self, task_id: int) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("reminder task disappeared")
        return row

    def _job_by_id(self, job_id: int) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM reminder_job WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("reminder job disappeared")
        return row

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("reminder clock must be timezone-aware")
        return value.astimezone(timezone.utc)


def _validate_draft(draft: ReminderDraft) -> None:
    for value, name in (
        (draft.owner_user_id, "owner"),
        (draft.persona_id, "persona"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"reminder {name} must be a positive integer")
    if isinstance(draft.chat_id, bool) or not isinstance(draft.chat_id, int):
        raise ValueError("reminder chat id must be an integer")
    if not isinstance(draft.text, str) or not draft.text.strip():
        raise ValueError("reminder text cannot be empty")
    if len(draft.text.strip()) > 1000:
        raise ValueError("reminder text exceeds the bounded limit")
    if not isinstance(draft.local_start, datetime) or draft.local_start.tzinfo is not None:
        raise ValueError("reminder local start must be a naive datetime")
    if not isinstance(draft.recurrence, RecurrenceRule):
        raise ValueError("reminder recurrence rule is invalid")
    _validate_timezone(draft.timezone_name)
    _validate_quiet_policy(draft.quiet_start_minute, draft.quiet_end_minute)
    if (
        isinstance(draft.grace_seconds, bool)
        or not isinstance(draft.grace_seconds, int)
        or draft.grace_seconds <= 0
        or draft.grace_seconds > 604800
    ):
        raise ValueError("reminder grace period is invalid")
    if not isinstance(draft.urgent, bool):
        raise ValueError("reminder urgent flag must be boolean")


def _validate_timezone(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reminder timezone cannot be empty")
    try:
        ZoneInfo(value.strip())
    except ZoneInfoNotFoundError:
        raise ValueError("reminder timezone is unknown") from None


def _validate_quiet_policy(start: int | None, end: int | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("quiet hours require both start and end")
    if start is None:
        return
    for value in (start, end):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value >= 1440
        ):
            raise ValueError("quiet hours minute is invalid")
    if start == end:
        raise ValueError("quiet hours range cannot be empty")


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=int(row["id"]),
        owner_user_id=int(row["owner_user_id"]),
        persona_id=int(row["persona_id"]),
        chat_id=int(row["chat_id"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        text=str(row["text"]),
        status=TaskStatus(str(row["status"])),
        created_at=_parse_aware(str(row["created_at"])),
        updated_at=_parse_aware(str(row["updated_at"])),
    )


def _job_from_row(row: sqlite3.Row) -> ReminderJob:
    scheduled = row["next_scheduled_for"]
    return ReminderJob(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        owner_user_id=int(row["owner_user_id"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        status=ReminderJobStatus(str(row["status"])),
        timezone_name=str(row["timezone_name"]),
        local_start=_parse_local(str(row["local_start"])),
        recurrence=RecurrenceRule.from_json(str(row["recurrence_json"])),
        next_scheduled_for=(None if scheduled is None else _parse_aware(str(scheduled))),
        emitted_count=int(row["emitted_count"]),
        grace_seconds=int(row["grace_seconds"]),
        quiet_start_minute=(
            None
            if row["quiet_start_minute"] is None
            else int(row["quiet_start_minute"])
        ),
        quiet_end_minute=(
            None if row["quiet_end_minute"] is None else int(row["quiet_end_minute"])
        ),
        urgent=bool(row["urgent"]),
        created_at=_parse_aware(str(row["created_at"])),
        updated_at=_parse_aware(str(row["updated_at"])),
    )


def _profile_policy_from_row(row: sqlite3.Row) -> ProfileReminderPolicy:
    return ProfileReminderPolicy(
        owner_user_id=int(row["user_id"]),
        timezone_name=str(row["timezone"]),
        quiet_start_minute=(
            None
            if row["quiet_start_minute"] is None
            else int(row["quiet_start_minute"])
        ),
        quiet_end_minute=(
            None if row["quiet_end_minute"] is None else int(row["quiet_end_minute"])
        ),
    )


def _run_from_row(row: sqlite3.Row) -> ReminderRun:
    return ReminderRun(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        owner_user_id=int(row["owner_user_id"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        scheduled_for=_parse_aware(str(row["scheduled_for"])),
        status=ReminderRunStatus(str(row["status"])),
        claimed_at=(
            None if row["claimed_at"] is None else _parse_aware(str(row["claimed_at"]))
        ),
        delivered_at=(
            None
            if row["delivered_at"] is None
            else _parse_aware(str(row["delivered_at"]))
        ),
        error_kind=None if row["error_kind"] is None else str(row["error_kind"]),
        created_at=_parse_aware(str(row["created_at"])),
        updated_at=_parse_aware(str(row["updated_at"])),
    )


def _outbox_from_row(row: sqlite3.Row) -> OutboxEntry:
    return OutboxEntry(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        owner_user_id=int(row["owner_user_id"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        chat_id=int(row["chat_id"]),
        text=str(row["text"]),
        scheduled_for=_parse_aware(str(row["scheduled_for"])),
        available_at=_parse_aware(str(row["available_at"])),
        status=OutboxStatus(str(row["status"])),
        attempt=int(row["attempt"]),
        lease_token=(
            None if row["lease_token"] is None else str(row["lease_token"])
        ),
        lease_until=(
            None if row["lease_until"] is None else _parse_aware(str(row["lease_until"]))
        ),
        telegram_message_id=(
            None
            if row["telegram_message_id"] is None
            else int(row["telegram_message_id"])
        ),
        error_kind=None if row["error_kind"] is None else str(row["error_kind"]),
        created_at=_parse_aware(str(row["created_at"])),
        updated_at=_parse_aware(str(row["updated_at"])),
    )


def _lease_from_row(row: sqlite3.Row) -> DeliveryLease:
    token = row["lease_token"]
    lease_until = row["lease_until"]
    if token is None or lease_until is None:
        raise RuntimeError("persisted delivery lease is incomplete")
    return DeliveryLease(
        outbox_id=int(row["id"]),
        run_id=int(row["run_id"]),
        task_id=int(row["task_id"]),
        owner_user_id=int(row["owner_user_id"]),
        lifecycle_epoch=int(row["lifecycle_epoch"]),
        chat_id=int(row["chat_id"]),
        text=str(row["text"]),
        scheduled_for=_parse_aware(str(row["scheduled_for"])),
        attempt=int(row["attempt"]),
        lease_token=str(token),
        lease_until=_parse_aware(str(lease_until)),
    )


def _parse_local(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is not None:
        raise ValueError("persisted reminder local time must be naive")
    return result


def _parse_aware(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("persisted reminder timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
