from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleKind(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class LocalTimeResolutionError(ValueError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"local time is {kind}")
        self.kind = kind


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    kind: ScheduleKind
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    monthday: int | None = None
    count: int | None = None
    until_local: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScheduleKind):
            try:
                object.__setattr__(self, "kind", ScheduleKind(self.kind))
            except (TypeError, ValueError):
                raise ValueError("schedule kind is invalid") from None
        if (
            isinstance(self.interval, bool)
            or not isinstance(self.interval, int)
            or self.interval < 1
            or self.interval > 365
        ):
            raise ValueError("recurrence interval is invalid")
        if isinstance(self.weekdays, list):
            object.__setattr__(self, "weekdays", tuple(self.weekdays))
        if not isinstance(self.weekdays, tuple) or any(
            isinstance(day, bool) or not isinstance(day, int) or day < 0 or day > 6
            for day in self.weekdays
        ):
            raise ValueError("recurrence weekdays are invalid")
        normalized_weekdays = tuple(sorted(set(self.weekdays)))
        object.__setattr__(self, "weekdays", normalized_weekdays)
        if self.monthday is not None and (
            isinstance(self.monthday, bool)
            or not isinstance(self.monthday, int)
            or self.monthday < 1
            or self.monthday > 31
        ):
            raise ValueError("recurrence monthday is invalid")
        if self.count is not None and (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
            or self.count > 10000
        ):
            raise ValueError("recurrence count is invalid")
        if self.until_local is not None:
            _require_naive(self.until_local)
        if self.kind is ScheduleKind.ONCE and (
            self.interval != 1
            or self.weekdays
            or self.monthday is not None
            or self.count is not None
            or self.until_local is not None
        ):
            raise ValueError("once schedule cannot contain recurrence fields")
        if self.kind is ScheduleKind.DAILY and (
            self.weekdays or self.monthday is not None
        ):
            raise ValueError("daily recurrence shape is invalid")
        if self.kind is ScheduleKind.WEEKLY and self.monthday is not None:
            raise ValueError("weekly recurrence shape is invalid")
        if self.kind is ScheduleKind.MONTHLY and self.weekdays:
            raise ValueError("monthly recurrence shape is invalid")

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "kind": self.kind.value,
                "interval": self.interval,
                "weekdays": list(self.weekdays),
                "monthday": self.monthday,
                "count": self.count,
                "until_local": (
                    None if self.until_local is None else self.until_local.isoformat()
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> "RecurrenceRule":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            raise ValueError("recurrence rule JSON is invalid") from None
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("recurrence rule version is invalid")
        until = data.get("until_local")
        try:
            until_local = None if until is None else datetime.fromisoformat(until)
        except (TypeError, ValueError):
            raise ValueError("recurrence until_local is invalid") from None
        return cls(
            kind=data.get("kind"),
            interval=data.get("interval", 1),
            weekdays=tuple(data.get("weekdays", ())),
            monthday=data.get("monthday"),
            count=data.get("count"),
            until_local=until_local,
        )


def resolve_local_time(local_time: datetime, timezone_name: str) -> datetime:
    _require_naive(local_time)
    zone = _load_zone(timezone_name)
    candidates = set()
    for fold in (0, 1):
        aware = local_time.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        round_trip = utc.astimezone(zone).replace(tzinfo=None)
        if round_trip == local_time:
            candidates.add(utc)
    if not candidates:
        raise LocalTimeResolutionError("nonexistent")
    if len(candidates) > 1:
        raise LocalTimeResolutionError("ambiguous")
    return candidates.pop()


def next_occurrence(
    rule: RecurrenceRule,
    *,
    anchor_local: datetime,
    after_local: datetime | None = None,
    emitted_count: int = 0,
) -> datetime | None:
    _require_naive(anchor_local)
    if after_local is not None:
        _require_naive(after_local)
        if after_local < anchor_local:
            raise ValueError("recurrence cursor precedes anchor")
    if isinstance(emitted_count, bool) or emitted_count < 0:
        raise ValueError("recurrence emitted count is invalid")
    if rule.count is not None and emitted_count >= rule.count:
        return None
    if after_local is None:
        candidate = anchor_local
    elif rule.kind is ScheduleKind.ONCE:
        return None
    elif rule.kind is ScheduleKind.DAILY:
        candidate = after_local + timedelta(days=rule.interval)
    elif rule.kind is ScheduleKind.WEEKLY:
        candidate = _next_weekly(rule, anchor_local, after_local)
    else:
        candidate = _next_monthly(rule, anchor_local, after_local)
    if rule.until_local is not None and candidate > rule.until_local:
        return None
    return candidate


def quiet_hours_available_at(
    scheduled_for: datetime,
    *,
    timezone_name: str,
    start_minute: int | None,
    end_minute: int | None,
    urgent: bool = False,
) -> datetime:
    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled time must be timezone-aware")
    zone = _load_zone(timezone_name)
    scheduled_utc = scheduled_for.astimezone(timezone.utc)
    if start_minute is None and end_minute is None:
        return scheduled_utc
    _validate_quiet_hours(start_minute, end_minute)
    if urgent:
        return scheduled_utc
    local = scheduled_utc.astimezone(zone)
    minute = local.hour * 60 + local.minute
    if start_minute < end_minute:
        in_quiet_hours = start_minute <= minute < end_minute
        target_date = local.date()
    else:
        in_quiet_hours = minute >= start_minute or minute < end_minute
        target_date = (
            local.date() + timedelta(days=1)
            if minute >= start_minute
            else local.date()
        )
    if not in_quiet_hours:
        return scheduled_utc
    target_local = datetime.combine(target_date, _minute_to_time(end_minute))
    return resolve_local_time(target_local, timezone_name)


def _next_weekly(
    rule: RecurrenceRule,
    anchor_local: datetime,
    after_local: datetime,
) -> datetime:
    weekdays = rule.weekdays or (anchor_local.weekday(),)
    anchor_week = anchor_local.date() - timedelta(days=anchor_local.weekday())
    for day_offset in range(1, 3661):
        candidate_date = after_local.date() + timedelta(days=day_offset)
        week_number = (candidate_date - anchor_week).days // 7
        if week_number < 0 or week_number % rule.interval != 0:
            continue
        if candidate_date.weekday() not in weekdays:
            continue
        return datetime.combine(candidate_date, anchor_local.time())
    raise ValueError("weekly recurrence exceeds bounded search")


def _next_monthly(
    rule: RecurrenceRule,
    anchor_local: datetime,
    after_local: datetime,
) -> datetime:
    monthday = rule.monthday or anchor_local.day
    elapsed_months = (
        (after_local.year - anchor_local.year) * 12
        + after_local.month
        - anchor_local.month
    )
    step = (elapsed_months // rule.interval + 1) * rule.interval
    for _ in range(1200):
        year, month = _add_months(anchor_local.year, anchor_local.month, step)
        try:
            candidate_date = date(year, month, monthday)
        except ValueError:
            step += rule.interval
            continue
        return datetime.combine(candidate_date, anchor_local.time())
    raise ValueError("monthly recurrence exceeds bounded search")


def _add_months(year: int, month: int, amount: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + amount
    return absolute // 12, absolute % 12 + 1


def _minute_to_time(value: int) -> time:
    return time(hour=value // 60, minute=value % 60)


def _validate_quiet_hours(start: int | None, end: int | None) -> None:
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


def _load_zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone name cannot be empty")
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError:
        raise ValueError("timezone name is unknown") from None


def _require_naive(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("local wall time must be a naive datetime")
