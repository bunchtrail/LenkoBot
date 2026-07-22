from datetime import datetime, timezone

import pytest

from lenkobot.reminder_parser import (
    REMINDER_DRAFT_SCHEMA,
    ReminderParser,
    extract_reminder_request,
)
from lenkobot.reminder_schedule import RecurrenceRule, ScheduleKind


class StructuredResponse:
    def __init__(self, value):
        self.value = value


class RecordingStructuredProvider:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def respond(self, prompt, *, schema_name, schema):
        self.calls.append((prompt, schema_name, schema))
        return StructuredResponse(self.value)


def payload(**overrides):
    value = {
        "text": "Call the clinic",
        "local_start": "2026-07-22T18:00:00",
        "timezone_name": None,
        "kind": "weekly",
        "interval": 1,
        "weekdays": [2, 4],
        "monthday": None,
        "count": 4,
        "until_local": None,
        "urgent": False,
    }
    value.update(overrides)
    return value


def test_structured_parser_returns_bounded_typed_draft_with_profile_timezone():
    provider = RecordingStructuredProvider(payload())
    parser = ReminderParser(
        provider,
        clock=lambda: datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
    )

    parsed = parser.parse(
        "Call the clinic every Wednesday and Friday at 18:00 four times",
        default_timezone="Europe/Berlin",
    )

    assert parsed.text == "Call the clinic"
    assert parsed.local_start == datetime(2026, 7, 22, 18, 0)
    assert parsed.timezone_name == "Europe/Berlin"
    assert parsed.recurrence == RecurrenceRule(
        kind=ScheduleKind.WEEKLY,
        weekdays=(2, 4),
        count=4,
    )
    assert parsed.urgent is False
    prompt, schema_name, schema = provider.calls[0]
    assert "2026-07-21T08:00:00+00:00" in prompt
    assert "Europe/Berlin" in prompt
    assert schema_name == "reminder_draft"
    assert schema is REMINDER_DRAFT_SCHEMA


def test_parser_preserves_dst_invalid_wall_time_for_needs_review_transition():
    provider = RecordingStructuredProvider(
        payload(
            local_start="2026-03-29T02:30:00",
            timezone_name="Europe/Berlin",
            kind="once",
            weekdays=[],
            count=None,
        )
    )
    parser = ReminderParser(
        provider,
        clock=lambda: datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    parsed = parser.parse("Напомни 29 марта в 02:30", default_timezone="UTC")

    assert parsed.local_start == datetime(2026, 3, 29, 2, 30)
    assert parsed.timezone_name == "Europe/Berlin"
    assert parsed.recurrence.kind is ScheduleKind.ONCE


@pytest.mark.parametrize(
    "bad_payload",
    (
        payload(text=""),
        payload(timezone_name="Not/AZone"),
        payload(local_start="not-a-date"),
        payload(kind="cron"),
        payload(extra="unexpected"),
    ),
)
def test_parser_rejects_malformed_or_out_of_contract_provider_payload(bad_payload):
    parser = ReminderParser(
        RecordingStructuredProvider(bad_payload),
        clock=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError):
        parser.parse("remind me", default_timezone="UTC")


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("напомни позвонить маме", "позвонить маме"),
        ("  НАПОМНИ: купить хлеб ", "купить хлеб"),
        ("remind me to call the clinic", "call the clinic"),
        ("Remind me: renew passport", "renew passport"),
        ("это просто напомни внутри", None),
        ("напомни", ""),
    ),
)
def test_only_explicit_private_text_prefix_is_detected(text, expected):
    assert extract_reminder_request(text) == expected
