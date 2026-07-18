import pytest

from lenkobot.session_store import TranscriptTurn
from lenkobot.session_summary import XaiSummaryGenerator


class StructuredProvider:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def respond(self, prompt, *, schema_name, schema):
        self.calls.append((prompt, schema_name, schema))
        return type("Response", (), {"value": self.value})()


def turns():
    return (
        TranscriptTurn(1, 9, 1, "user", "Question", None, "now"),
        TranscriptTurn(2, 9, 2, "assistant", "Answer", "resp-1", "now"),
    )


def test_summary_generator_returns_bounded_typed_summary():
    provider = StructuredProvider({"summary": "The user asked a question."})

    summary = XaiSummaryGenerator(provider).generate(turns=turns())

    assert summary == "The user asked a question."
    assert provider.calls[0][1] == "session_summary"
    assert "UNTRUSTED" in provider.calls[0][0]


@pytest.mark.parametrize(
    "value",
    (
        {"summary": ""},
        {"summary": "x" * 4001},
        {"wrong": "summary"},
    ),
)
def test_summary_generator_rejects_invalid_output(value):
    with pytest.raises(ValueError, match="summary"):
        XaiSummaryGenerator(StructuredProvider(value)).generate(turns=turns())
