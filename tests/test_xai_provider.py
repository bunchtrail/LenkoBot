import json
from datetime import timezone

import pytest

from lenkobot.xai_provider import (
    ApiKeyCredentialSource,
    BearerCredential,
    CredentialPolicy,
    EntitlementDenied,
    HttpResponse,
    ProviderRequestError,
    UntrustedInferenceHost,
    UrllibJsonHttpClient,
    XaiProvider,
    XaiResponsesTransport,
    XaiTextResponse,
)


class RecordingHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        return self.response


class StaticCredentialSource:
    def __init__(self, credential):
        self.credential = credential
        self.call_count = 0

    def get_credential(self):
        self.call_count += 1
        return self.credential


class FailingCredentialSource:
    def __init__(self, error):
        self.error = error
        self.call_count = 0

    def get_credential(self):
        self.call_count += 1
        raise self.error


class SequenceTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, credential, model, prompt):
        self.calls.append((credential, model, prompt))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def credential(source_identity, token="secret-token"):
    return BearerCredential(
        token=token,
        expires_at=None,
        base_url="https://api.x.ai/v1",
        source_identity=source_identity,
    )


def response(source_identity):
    return XaiTextResponse(
        response_id="resp-1",
        model="grok-4.5",
        text="Hello",
        credential_source=source_identity,
    )


def test_api_key_source_returns_redacted_bearer_credential():
    source = ApiKeyCredentialSource("api-secret")

    result = source.get_credential()

    assert result.token == "api-secret"
    assert result.base_url == "https://api.x.ai/v1"
    assert result.source_identity == "xai_api_key"
    assert result.expires_at is None
    assert "api-secret" not in repr(result)


def test_responses_transport_sends_minimal_request_and_extracts_assistant_text():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={"x-request-id": "request-1"},
            body=json.dumps(
                {
                    "id": "resp-1",
                    "model": "grok-4.5",
                    "output": [
                        {"type": "reasoning", "content": []},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Hello"},
                                {"type": "output_text", "text": " world"},
                            ],
                        },
                    ],
                }
            ),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)
    bearer = credential("xai_oauth", token="oauth-secret")

    result = transport.complete(bearer, "grok-4.5", "Hi")

    assert result == XaiTextResponse(
        response_id="resp-1",
        model="grok-4.5",
        text="Hello world",
        credential_source="xai_oauth",
    )
    assert http_client.calls == [
        (
            "https://api.x.ai/v1/responses",
            {
                "Accept": "application/json",
                "Authorization": "Bearer oauth-secret",
                "Content-Type": "application/json",
            },
            {"model": "grok-4.5", "input": "Hi"},
        )
    ]


def test_responses_transport_rejects_untrusted_or_insecure_credential_host():
    http_client = RecordingHttpClient(HttpResponse(status=200, headers={}, body="{}"))
    transport = XaiResponsesTransport(http_client=http_client)

    for base_url in (
        "https://api.x.ai.attacker.test/v1",
        "https://api.x.ai:8443/v1",
        "http://api.x.ai/v1",
    ):
        bearer = BearerCredential(
            token="secret",
            expires_at=None,
            base_url=base_url,
            source_identity="xai_oauth",
        )
        with pytest.raises(UntrustedInferenceHost):
            transport.complete(bearer, "grok-4.5", "Hi")

    assert http_client.calls == []


def test_raw_403_stays_generic_without_confirmed_entitlement_classifier():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=403,
            headers={"x-request-id": "request-1"},
            body=json.dumps(
                {"error": {"code": "forbidden", "message": "Access denied"}}
            ),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)

    with pytest.raises(ProviderRequestError) as error:
        transport.complete(credential("xai_oauth"), "grok-4.5", "Hi")

    assert not isinstance(error.value, EntitlementDenied)
    assert error.value.status == 403
    assert error.value.code == "forbidden"
    assert error.value.raw_body == http_client.response.body


def test_confirmed_entitlement_classifier_produces_typed_denial():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=403,
            headers={},
            body=json.dumps(
                {
                    "error": {
                        "code": "account_not_entitled",
                        "message": "Upgrade required",
                    }
                }
            ),
        )
    )
    transport = XaiResponsesTransport(
        http_client=http_client,
        entitlement_classifier=lambda error: error.code == "account_not_entitled",
    )

    with pytest.raises(EntitlementDenied):
        transport.complete(credential("xai_oauth"), "grok-4.5", "Hi")


def test_oauth_then_api_key_falls_back_only_after_typed_entitlement_denial():
    oauth = StaticCredentialSource(credential("xai_oauth"))
    api_key = StaticCredentialSource(credential("xai_api_key"))
    transport = SequenceTransport(
        EntitlementDenied(
            "OAuth account is not entitled",
            status=403,
            code="account_not_entitled",
            raw_body="{}",
            headers={},
        ),
        response("xai_api_key"),
    )
    provider = XaiProvider(
        transport=transport,
        policy=CredentialPolicy.OAUTH_THEN_API_KEY,
        oauth_source=oauth,
        api_key_source=api_key,
    )

    result = provider.respond("Hi")

    assert result.fallback_from == "xai_oauth"
    assert result.credential_source == "xai_api_key"
    assert [call[0].source_identity for call in transport.calls] == [
        "xai_oauth",
        "xai_api_key",
    ]


def test_typed_entitlement_denial_from_oauth_source_can_fall_back():
    oauth = FailingCredentialSource(
        EntitlementDenied(
            "OAuth refresh denied",
            status=403,
            code="account_not_entitled",
            raw_body="{}",
            headers={},
        )
    )
    api_key = StaticCredentialSource(credential("xai_api_key"))
    transport = SequenceTransport(response("xai_api_key"))
    provider = XaiProvider(
        transport=transport,
        policy=CredentialPolicy.OAUTH_THEN_API_KEY,
        oauth_source=oauth,
        api_key_source=api_key,
    )

    result = provider.respond("Hi")

    assert result.fallback_from == "xai_oauth"
    assert result.credential_source == "xai_api_key"
    assert [call[0].source_identity for call in transport.calls] == ["xai_api_key"]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_oauth_then_api_key_does_not_fallback_for_generic_provider_errors(status):
    oauth = StaticCredentialSource(credential("xai_oauth"))
    api_key = StaticCredentialSource(credential("xai_api_key"))
    transport = SequenceTransport(
        ProviderRequestError(
            "request failed",
            status=status,
            code="generic_error",
            raw_body="{}",
            headers={},
        )
    )
    provider = XaiProvider(
        transport=transport,
        policy=CredentialPolicy.OAUTH_THEN_API_KEY,
        oauth_source=oauth,
        api_key_source=api_key,
    )

    with pytest.raises(ProviderRequestError):
        provider.respond("Hi")

    assert len(transport.calls) == 1
    assert api_key.call_count == 0


def test_urllib_client_serializes_json_and_preserves_response_metadata(monkeypatch):
    observed = {}

    class UrlResponse:
        status = 200
        headers = {"x-request-id": "request-1"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def read(self):
            return b'{"output": []}'

    def fake_urlopen(request, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return UrlResponse()

    monkeypatch.setattr("lenkobot.xai_provider.urlopen", fake_urlopen)
    client = UrllibJsonHttpClient(timeout_seconds=12)

    result = client.post_json(
        "https://api.x.ai/v1/responses",
        {"Authorization": "Bearer secret"},
        {"model": "grok-4.5", "input": "Hi"},
    )

    assert result == HttpResponse(
        status=200,
        headers={"x-request-id": "request-1"},
        body='{"output": []}',
    )
    assert observed["request"].full_url == "https://api.x.ai/v1/responses"
    assert json.loads(observed["request"].data) == {
        "model": "grok-4.5",
        "input": "Hi",
    }
    assert observed["timeout"] == 12
