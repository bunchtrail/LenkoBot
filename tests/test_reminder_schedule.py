from datetime import datetime, timezone

import pytest

from lenkobot.reminder_schedule import (
    LocalTimeResolutionError,
    RecurrenceRule,
    ScheduleKind,
    next_occurrence,
    quiet_hours_available_at,
    resolve_local_time,
)


def test_resolve_local_time_returns_unique_utc_instant():
    result = resolve_local_time(
        datetime(2026, 7, 21, 9, 30),
        "Europe/Moscow",
    )

    assert result == datetime(2026, 7, 21, 6, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("local_time", "kind"),
    (
        (datetime(2026, 3, 29, 2, 30), "nonexistent"),
        (datetime(2026, 10, 25, 2, 30), "ambiguous"),
    ),
)
def test_resolve_local_time_rejects_dst_gap_and_fold(local_time, kind):
    with pytest.raises(LocalTimeResolutionError) as exc_info:
        resolve_local_time(local_time, "Europe/Berlin")

    assert exc_info.value.kind == kind


def test_resolve_local_time_rejects_unknown_timezone_and_aware_input():
    with pytest.raises(ValueError, match="timezone"):
        resolve_local_time(datetime(2026, 7, 21, 9), "Mars/Olympus")
    with pytest.raises(ValueError, match="naive"):
        resolve_local_time(
            datetime(2026, 7, 21, 9, tzinfo=timezone.utc),
            "UTC",
        )


def test_daily_recurrence_honors_interval_count_and_until():
    anchor = datetime(2026, 7, 21, 9)
    rule = RecurrenceRule(
        kind=ScheduleKind.DAILY,
        interval=2,
        count=3,
        until_local=datetime(2026, 7, 30, 9),
    )

    first = next_occurrence(rule, anchor_local=anchor)
    second = next_occurrence(
        rule,
        anchor_local=anchor,
        after_local=first,
        emitted_count=1,
    )
    third = next_occurrence(
        rule,
        anchor_local=anchor,
        after_local=second,
        emitted_count=2,
    )

    assert (first, second, third) == (
        datetime(2026, 7, 21, 9),
        datetime(2026, 7, 23, 9),
        datetime(2026, 7, 25, 9),
    )
    assert (
        next_occurrence(
            rule,
            anchor_local=anchor,
            after_local=third,
            emitted_count=3,
        )
        is None
    )


def test_weekly_recurrence_uses_selected_weekdays_and_interval():
    anchor = datetime(2026, 7, 20, 8)
    rule = RecurrenceRule(
        kind=ScheduleKind.WEEKLY,
        interval=2,
        weekdays=(0, 2),
    )

    first = next_occurrence(rule, anchor_local=anchor)
    second = next_occurrence(rule, anchor_local=anchor, after_local=first)
    third = next_occurrence(rule, anchor_local=anchor, after_local=second)

    assert first == datetime(2026, 7, 20, 8)
    assert second == datetime(2026, 7, 22, 8)
    assert third == datetime(2026, 8, 3, 8)


def test_monthly_recurrence_skips_calendar_invalid_date():
    anchor = datetime(2026, 1, 31, 9)
    rule = RecurrenceRule(
        kind=ScheduleKind.MONTHLY,
        monthday=31,
    )

    assert next_occurrence(
        rule,
        anchor_local=anchor,
        after_local=anchor,
    ) == datetime(2026, 3, 31, 9)


def test_once_rule_has_only_anchor_occurrence():
    anchor = datetime(2026, 7, 21, 9)
    rule = RecurrenceRule(kind=ScheduleKind.ONCE)

    assert next_occurrence(rule, anchor_local=anchor) == anchor
    assert next_occurrence(rule, anchor_local=anchor, after_local=anchor) is None


def test_recurrence_rule_round_trips_versioned_json():
    rule = RecurrenceRule(
        kind=ScheduleKind.WEEKLY,
        interval=2,
        weekdays=(0, 4),
        count=10,
        until_local=datetime(2026, 12, 31, 9),
    )

    assert RecurrenceRule.from_json(rule.to_json()) == rule


@pytest.mark.parametrize(
    "kwargs",
    (
        {"kind": ScheduleKind.DAILY, "interval": 0},
        {"kind": ScheduleKind.WEEKLY, "weekdays": (7,)},
        {"kind": ScheduleKind.MONTHLY, "monthday": 32},
        {"kind": ScheduleKind.ONCE, "count": 2},
    ),
)
def test_recurrence_rule_rejects_invalid_shape(kwargs):
    with pytest.raises(ValueError):
        RecurrenceRule(**kwargs)


def test_quiet_hours_shift_cross_midnight_without_changing_schedule():
    scheduled = datetime(2026, 7, 21, 20, 30, tzinfo=timezone.utc)

    available = quiet_hours_available_at(
        scheduled,
        timezone_name="Europe/Moscow",
        start_minute=22 * 60,
        end_minute=7 * 60,
    )

    assert scheduled == datetime(2026, 7, 21, 20, 30, tzinfo=timezone.utc)
    assert available == datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)


def test_quiet_hours_allow_daytime_and_urgent_override():
    daytime = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
    nighttime = datetime(2026, 7, 21, 21, 0, tzinfo=timezone.utc)

    assert quiet_hours_available_at(
        daytime,
        timezone_name="UTC",
        start_minute=22 * 60,
        end_minute=7 * 60,
    ) == daytime
    assert quiet_hours_available_at(
        nighttime,
        timezone_name="UTC",
        start_minute=20 * 60,
        end_minute=7 * 60,
        urgent=True,
    ) == nighttime
