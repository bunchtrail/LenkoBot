import json

import pytest

from lenkobot.xai_provider import (
    ApiKeyCredentialSource,
    BearerCredential,
    CredentialPolicy,
    CredentialUnavailable,
    EntitlementDenied,
    HttpResponse,
    ProviderRequestError,
    UntrustedInferenceHost,
    UrllibJsonHttpClient,
    XaiFunctionCall,
    XaiProvider,
    XaiResponsesTransport,
    XaiStructuredProvider,
    XaiStructuredResponse,
    XaiTextResponse,
    XaiToolOutput,
    XaiToolTurn,
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


class RecordingToolTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.tool_calls = []
        self.output_calls = []

    def complete_with_tools(self, credential, model, prompt, *, tools):
        self.tool_calls.append((credential, model, prompt, tools))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def complete_with_tool_output(
        self,
        credential,
        model,
        *,
        previous_response_id,
        tool_outputs,
        tools,
    ):
        self.output_calls.append(
            (credential, model, previous_response_id, tool_outputs, tools)
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_WEB_SEARCH_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": "Search the public web for current information.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}


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


def test_responses_transport_sends_structured_schema_and_parses_only_output_text():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "id": "resp-structured",
                    "model": "grok-4.5",
                    "output": [
                        {"type": "reasoning", "content": []},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"number": 2}',
                                }
                            ],
                        },
                    ],
                }
            ),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)
    schema = {
        "type": "object",
        "properties": {"number": {"type": "integer"}},
        "required": ["number"],
        "additionalProperties": False,
    }

    result = transport.complete_structured(
        credential("xai_oauth"),
        "grok-4.5",
        "Return the number 2.",
        schema_name="number_v1",
        schema=schema,
    )

    assert result == XaiStructuredResponse(
        response_id="resp-structured",
        model="grok-4.5",
        value={"number": 2},
        credential_source="xai_oauth",
    )
    assert http_client.calls[0][2] == {
        "model": "grok-4.5",
        "input": "Return the number 2.",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "number_v1",
                "schema": schema,
                "strict": True,
            }
        },
    }


def test_structured_provider_uses_only_configured_oauth_source():
    credential_source = StaticCredentialSource(credential("xai_oauth"))

    class StructuredTransport:
        def __init__(self):
            self.calls = []

        def complete_structured(
            self,
            credential,
            model,
            prompt,
            *,
            schema_name,
            schema,
        ):
            self.calls.append((credential, model, prompt, schema_name, schema))
            return XaiStructuredResponse(
                response_id="resp-1",
                model=model,
                value={"ok": True},
                credential_source=credential.source_identity,
            )

    transport = StructuredTransport()
    provider = XaiStructuredProvider(
        transport,
        oauth_source=credential_source,
        model="grok-4.5",
    )
    schema = {"type": "object"}

    result = provider.respond(
        "Return an object.",
        schema_name="object_v1",
        schema=schema,
    )

    assert result.value == {"ok": True}
    assert credential_source.call_count == 1
    assert transport.calls[0][0].source_identity == "xai_oauth"


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

def test_complete_with_tools_sends_tools_and_parses_function_call():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "id": "resp-9",
                    "model": "grok-4.5",
                    "output": [
                        {"type": "reasoning", "content": []},
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "web_search",
                            "arguments": '{"query":"usd rub rate"}',
                            "status": "completed",
                        },
                    ],
                }
            ),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)

    turn = transport.complete_with_tools(
        credential("xai_oauth"),
        "grok-4.5",
        "курс доллара?",
        tools=(_WEB_SEARCH_TOOL,),
    )

    _, _, payload = http_client.calls[0]
    assert payload["tools"] == [_WEB_SEARCH_TOOL]
    assert payload["parallel_tool_calls"] is False
    assert payload["input"] == "курс доллара?"
    assert turn.response_id == "resp-9"
    assert turn.text is None
    assert turn.credential_source == "xai_oauth"
    assert turn.fallback_from is None
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.call_id == "call-1"
    assert call.name == "web_search"
    assert call.arguments == '{"query":"usd rub rate"}'


def test_complete_with_tools_returns_text_turn_when_model_answers():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "id": "resp-10",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "привет"},
                            ],
                        },
                    ],
                }
            ),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)

    turn = transport.complete_with_tools(
        credential("xai_oauth"),
        "grok-4.5",
        "привет",
        tools=(_WEB_SEARCH_TOOL,),
    )

    assert turn.text == "привет"
    assert turn.model == "grok-4.5"
    assert turn.tool_calls == ()


def test_complete_with_tool_output_sends_continuation_payload():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "id": "resp-11",
                    "model": "grok-4.5",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "курс 80"},
                            ],
                        },
                    ],
                }
            ),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)

    turn = transport.complete_with_tool_output(
        credential("xai_oauth"),
        "grok-4.5",
        previous_response_id="resp-9",
        tool_outputs=(
            XaiToolOutput(call_id="call-1", output='{"results": []}'),
        ),
        tools=(_WEB_SEARCH_TOOL,),
    )

    _, _, payload = http_client.calls[0]
    assert payload["previous_response_id"] == "resp-9"
    assert payload["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"results": []}',
        }
    ]
    assert payload["tools"] == [_WEB_SEARCH_TOOL]
    assert payload["parallel_tool_calls"] is False
    assert turn.response_id == "resp-11"
    assert turn.text == "курс 80"


def test_tool_turn_without_text_or_calls_raises_invalid_response():
    http_client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps({"id": "resp-12", "model": "grok-4.5", "output": []}),
        )
    )
    transport = XaiResponsesTransport(http_client=http_client)

    with pytest.raises(ProviderRequestError) as exc_info:
        transport.complete_with_tools(
            credential("xai_oauth"),
            "grok-4.5",
            "hi",
            tools=(_WEB_SEARCH_TOOL,),
        )

    assert exc_info.value.code == "invalid_response"


def test_tool_turn_rejects_invalid_arguments():
    transport = XaiResponsesTransport(http_client=RecordingHttpClient(None))

    with pytest.raises(ValueError):
        transport.complete_with_tools(
            credential("xai_oauth"), "grok-4.5", "hi", tools=()
        )
    with pytest.raises(ValueError):
        transport.complete_with_tools(
            credential("xai_oauth"),
            "grok-4.5",
            "hi",
            tools=({"type": "function"},),
        )
    with pytest.raises(ValueError):
        transport.complete_with_tool_output(
            credential("xai_oauth"),
            "grok-4.5",
            previous_response_id="",
            tool_outputs=(XaiToolOutput(call_id="c", output="{}"),),
            tools=(_WEB_SEARCH_TOOL,),
        )
    with pytest.raises(ValueError):
        transport.complete_with_tool_output(
            credential("xai_oauth"),
            "grok-4.5",
            previous_response_id="resp-9",
            tool_outputs=(),
            tools=(_WEB_SEARCH_TOOL,),
        )


def test_provider_respond_with_tools_uses_oauth_only_policy():
    tool_turn = XaiToolTurn(
        response_id="resp-9",
        model="grok-4.5",
        text=None,
        tool_calls=(
            XaiFunctionCall(call_id="call-1", name="web_search", arguments="{}"),
        ),
        credential_source="xai_oauth",
    )
    transport = RecordingToolTransport(tool_turn)
    oauth_source = StaticCredentialSource(credential("xai_oauth"))
    provider = XaiProvider(
        transport,
        CredentialPolicy.OAUTH_ONLY,
        oauth_source=oauth_source,
    )

    result = provider.respond_with_tools("hi", tools=(_WEB_SEARCH_TOOL,))

    assert result is tool_turn
    assert oauth_source.call_count == 1
    credential_used, model, prompt, tools = transport.tool_calls[0]
    assert credential_used.source_identity == "xai_oauth"
    assert model == "grok-4.5"
    assert prompt == "hi"
    assert tools == (_WEB_SEARCH_TOOL,)


def test_provider_respond_with_tool_outputs_uses_oauth_only_policy():
    final_turn = XaiToolTurn(
        response_id="resp-11",
        model="grok-4.5",
        text="done",
        tool_calls=(),
        credential_source="xai_oauth",
    )
    transport = RecordingToolTransport(final_turn)
    provider = XaiProvider(
        transport,
        CredentialPolicy.OAUTH_ONLY,
        oauth_source=StaticCredentialSource(credential("xai_oauth")),
    )

    result = provider.respond_with_tool_outputs(
        previous_response_id="resp-9",
        tool_outputs=(XaiToolOutput(call_id="call-1", output="{}"),),
        tools=(_WEB_SEARCH_TOOL,),
    )

    assert result is final_turn
    _, model, previous_id, outputs, tools = transport.output_calls[0]
    assert model == "grok-4.5"
    assert previous_id == "resp-9"
    assert outputs == (XaiToolOutput(call_id="call-1", output="{}"),)
    assert tools == (_WEB_SEARCH_TOOL,)


def test_provider_tool_turns_propagate_credential_unavailable():
    provider = XaiProvider(
        RecordingToolTransport(),
        CredentialPolicy.OAUTH_ONLY,
        oauth_source=FailingCredentialSource(
            CredentialUnavailable("no state")
        ),
    )

    with pytest.raises(CredentialUnavailable):
        provider.respond_with_tools("hi", tools=(_WEB_SEARCH_TOOL,))
    with pytest.raises(CredentialUnavailable):
        provider.respond_with_tool_outputs(
            previous_response_id="resp-9",
            tool_outputs=(XaiToolOutput(call_id="c", output="{}"),),
            tools=(_WEB_SEARCH_TOOL,),
        )
