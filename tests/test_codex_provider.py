import json

import pytest

from lenkobot.codex_provider import (
    CODEX_MODEL,
    CodexProvider,
    CodexStructuredProvider,
    CodexTurnOutcome,
    SdkCodexThreadRunner,
)
from lenkobot.xai_provider import (
    ProviderRequestError,
    XaiInputMessage,
    XaiStructuredResponse,
    XaiTextResponse,
)


class RecordingRunner:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [CodexTurnOutcome(id="turn-1", text="ok")]
        self.calls = []

    def run_turn(self, *, instructions, prompt, output_schema):
        self.calls.append(
            {
                "instructions": instructions,
                "prompt": prompt,
                "output_schema": output_schema,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FailingRunner:
    def __init__(self, error):
        self.error = error

    def run_turn(self, **_):
        raise self.error


def test_text_provider_returns_domain_response_without_sdk_types():
    runner = RecordingRunner(CodexTurnOutcome(id="turn-7", text="привет"))

    response = CodexProvider(runner).respond("hello")

    assert isinstance(response, XaiTextResponse)
    assert response.text == "привет"
    assert response.model == CODEX_MODEL
    assert response.response_id == "turn-7"
    assert response.credential_source == "codex_oauth"
    assert response.fallback_from is None
    assert runner.calls[0]["prompt"] == "hello"
    assert runner.calls[0]["instructions"] is None
    assert runner.calls[0]["output_schema"] is None


def test_text_provider_declares_no_tool_support():
    assert CodexProvider(RecordingRunner()).supports_tools is False
    assert CodexProvider(RecordingRunner()).supports_message_input is True


def test_message_prompt_sends_identity_as_instructions_and_marks_transcript_untrusted():
    runner = RecordingRunner()

    CodexProvider(runner).respond(
        (
            XaiInputMessage(role="system", content="You are Lenko."),
            XaiInputMessage(role="user", content="привет"),
            XaiInputMessage(role="assistant", content="здоров"),
            XaiInputMessage(role="user", content="как дела"),
        )
    )

    call = runner.calls[0]
    assert call["instructions"] == "You are Lenko."
    assert "UNTRUSTED" in call["prompt"]
    assert "привет" in call["prompt"]
    assert "здоров" in call["prompt"]
    assert "как дела" in call["prompt"]
    assert "You are Lenko." not in call["prompt"]


def test_message_prompt_joins_multiple_identity_sections():
    runner = RecordingRunner()

    CodexProvider(runner).respond(
        (
            XaiInputMessage(role="system", content="Identity."),
            XaiInputMessage(role="system", content="Voice."),
            XaiInputMessage(role="user", content="hi"),
        )
    )

    assert runner.calls[0]["instructions"] == "Identity.\n\nVoice."


def test_text_provider_rejects_empty_prompt():
    with pytest.raises(ValueError):
        CodexProvider(RecordingRunner()).respond("   ")


def test_structured_provider_parses_schema_payload():
    runner = RecordingRunner(
        CodexTurnOutcome(id="turn-9", text=json.dumps({"summary": "готово"}))
    )
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}

    response = CodexStructuredProvider(runner).respond(
        "summarize",
        schema_name="session_summary",
        schema=schema,
    )

    assert isinstance(response, XaiStructuredResponse)
    assert response.value == {"summary": "готово"}
    assert response.model == CODEX_MODEL
    assert response.credential_source == "codex_oauth"
    assert runner.calls[0]["output_schema"] == schema


def test_structured_provider_tolerates_fenced_json():
    runner = RecordingRunner(
        CodexTurnOutcome(id="t", text='```json\n{"summary":"ok"}\n```')
    )

    response = CodexStructuredProvider(runner).respond(
        "summarize",
        schema_name="session_summary",
        schema={"type": "object"},
    )

    assert response.value == {"summary": "ok"}


def test_structured_provider_maps_invalid_json_to_controlled_error():
    runner = RecordingRunner(CodexTurnOutcome(id="t", text="not json at all"))

    with pytest.raises(ProviderRequestError) as exc_info:
        CodexStructuredProvider(runner).respond(
            "summarize",
            schema_name="session_summary",
            schema={"type": "object"},
        )

    assert exc_info.value.code == "invalid_json"
    assert exc_info.value.raw_body == ""
    assert "not json at all" not in str(exc_info.value)


def test_provider_maps_sdk_failure_to_controlled_error_without_leaking_detail():
    class SdkBoom(Exception):
        pass

    provider = CodexProvider(FailingRunner(SdkBoom("bearer sk-secret-token leaked")))

    with pytest.raises(ProviderRequestError) as exc_info:
        provider.respond("hello")

    assert "sk-secret-token" not in str(exc_info.value)
    assert exc_info.value.raw_body == ""


def test_runner_pins_minimal_authority_and_ephemeral_thread():
    observed = {}

    class FakeThread:
        def run(self, prompt, **kwargs):
            observed["run"] = {"prompt": prompt, **kwargs}
            return type(
                "Result",
                (),
                {"id": "turn-3", "final_response": "done", "status": None, "error": None},
            )()

    class FakeCodex:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            observed["closed"] = True

        def thread_start(self, **kwargs):
            observed["thread_start"] = kwargs
            return FakeThread()

    runner = SdkCodexThreadRunner(codex_factory=FakeCodex)
    outcome = runner.run_turn(instructions="be brief", prompt="hi", output_schema=None)

    assert outcome.text == "done"
    assert observed["thread_start"]["sandbox"].value == "read-only"
    assert observed["thread_start"]["approval_mode"].value == "deny_all"
    assert observed["thread_start"]["ephemeral"] is True
    assert observed["thread_start"]["model"] == CODEX_MODEL
    assert observed["thread_start"]["base_instructions"] == "be brief"
    assert observed["closed"] is True


def test_runner_passes_output_schema_through_to_turn():
    schema = {"type": "object"}
    observed = {}

    class FakeThread:
        def run(self, prompt, **kwargs):
            observed.update(kwargs)
            return type(
                "Result",
                (),
                {"id": "t", "final_response": "{}", "status": None, "error": None},
            )()

    class FakeCodex:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def thread_start(self, **kwargs):
            return FakeThread()

    SdkCodexThreadRunner(codex_factory=FakeCodex).run_turn(
        instructions=None,
        prompt="p",
        output_schema=schema,
    )

    assert observed["output_schema"] == schema


def test_runner_reports_missing_final_response_as_controlled_error():
    class FakeThread:
        def run(self, prompt, **kwargs):
            return type(
                "Result",
                (),
                {"id": "t", "final_response": None, "status": None, "error": None},
            )()

    class FakeCodex:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def thread_start(self, **kwargs):
            return FakeThread()

    with pytest.raises(ProviderRequestError):
        SdkCodexThreadRunner(codex_factory=FakeCodex).run_turn(
            instructions=None,
            prompt="p",
            output_schema=None,
        )


def test_codex_home_defaults_outside_the_operator_interactive_home(monkeypatch):
    from lenkobot.codex_provider import codex_home_path

    monkeypatch.delenv("LENKOBOT_CODEX_HOME", raising=False)

    path = codex_home_path()

    assert path.parts[-2:] == (".lenkobot", "codex")


def test_codex_home_honours_explicit_override(monkeypatch, tmp_path):
    from lenkobot.codex_provider import codex_home_path

    monkeypatch.setenv("LENKOBOT_CODEX_HOME", str(tmp_path / "own"))

    assert codex_home_path() == tmp_path / "own"


def test_codex_binary_override_is_optional(monkeypatch):
    from lenkobot.codex_provider import codex_binary_override

    monkeypatch.delenv("LENKOBOT_CODEX_BIN", raising=False)
    assert codex_binary_override() is None

    monkeypatch.setenv("LENKOBOT_CODEX_BIN", "/usr/local/bin/codex")
    assert codex_binary_override() == "/usr/local/bin/codex"


def test_codex_config_isolates_home_and_keeps_process_environment(monkeypatch, tmp_path):
    from lenkobot.codex_provider import _codex_config

    monkeypatch.setenv("LENKOBOT_CODEX_HOME", str(tmp_path / "own"))
    monkeypatch.setenv("PATH_MARKER_FOR_TEST", "kept")

    config = _codex_config()

    assert config.env["CODEX_HOME"] == str(tmp_path / "own")
    assert config.env["PATH_MARKER_FOR_TEST"] == "kept"
    assert (tmp_path / "own").is_dir()


class _AccountCodex:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def account(self):
        return self.response


def test_verify_credentials_fails_closed_when_signed_out():
    from lenkobot.codex_provider import verify_codex_credentials
    from lenkobot.xai_provider import CredentialUnavailable

    signed_out = type("Resp", (), {"account": None, "requires_openai_auth": True})()

    with pytest.raises(CredentialUnavailable):
        verify_codex_credentials(codex_factory=lambda: _AccountCodex(signed_out))


def test_verify_credentials_accepts_account_that_reports_requires_openai_auth():
    """A healthy ChatGPT subscription login reports this flag as true.

    Gating on it rejected a valid Plus account on the real server, so the
    presence of an account is the only sign-in signal.
    """
    from lenkobot.codex_provider import verify_codex_credentials

    signed_in = type("Resp", (), {"account": object(), "requires_openai_auth": True})()

    verify_codex_credentials(codex_factory=lambda: _AccountCodex(signed_in))


def test_verify_credentials_accepts_signed_in_account():
    from lenkobot.codex_provider import verify_codex_credentials

    signed_in = type("Resp", (), {"account": object(), "requires_openai_auth": False})()

    verify_codex_credentials(codex_factory=lambda: _AccountCodex(signed_in))
