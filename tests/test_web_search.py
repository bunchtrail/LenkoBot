import asyncio
from datetime import datetime, timezone
import json

import pytest
from ddgs.exceptions import RatelimitException

from lenkobot.web_search import (
    DdgsWebSearch,
    SearchResult,
    TavilyWebSearch,
    WebSearchError,
    WebSearchToolLoop,
)
from lenkobot.xai_provider import (
    HttpResponse,
    ProviderRequestError,
    XaiFunctionCall,
    XaiToolTurn,
)


class FakeDdgs:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def text(self, query, *, max_results):
        self.calls.append((query, max_results))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class RecordingHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SequenceToolProvider:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.initial_calls = []
        self.continuation_calls = []

    def respond_with_tools(self, prompt, *, tools):
        self.initial_calls.append((prompt, tools))
        return self.turns.pop(0)

    def respond_with_tool_outputs(
        self,
        *,
        previous_response_id,
        tool_outputs,
        tools,
    ):
        self.continuation_calls.append(
            (previous_response_id, tool_outputs, tools)
        )
        return self.turns.pop(0)


class RecordingSearch:
    def __init__(self, outcome):
        self.outcome = outcome
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def tool_turn(
    response_id,
    *,
    text=None,
    calls=(),
    fallback_from=None,
):
    return XaiToolTurn(
        response_id=response_id,
        model="grok-4.5",
        text=text,
        tool_calls=calls,
        credential_source="xai_oauth",
        fallback_from=fallback_from,
    )


def web_call(call_id="call-1", query="курс доллара"):
    return XaiFunctionCall(
        call_id=call_id,
        name="web_search",
        arguments=json.dumps({"query": query}),
    )


def test_ddgs_search_maps_and_bounds_results():
    ddgs = FakeDdgs(
        [
            {
                "title": "ЦБ РФ",
                "href": "https://cbr.ru/currency_base/daily/",
                "body": "x" * 2000,
            },
            {"title": "missing URL", "body": "ignored"},
        ]
    )
    search = DdgsWebSearch(max_results=3, ddgs_factory=lambda: ddgs)

    result = search.search("  курс доллара  ")

    assert ddgs.calls == [("курс доллара", 3)]
    assert result == (
        SearchResult(
            title="ЦБ РФ",
            url="https://cbr.ru/currency_base/daily/",
            snippet="x" * 1200,
        ),
    )


def test_ddgs_search_maps_rate_limit_to_typed_error():
    search = DdgsWebSearch(
        ddgs_factory=lambda: FakeDdgs(RatelimitException("429"))
    )

    with pytest.raises(WebSearchError) as exc_info:
        search.search("news")

    assert exc_info.value.code == "rate_limited"
    assert "429" not in str(exc_info.value)


def test_tavily_search_posts_basic_request_and_maps_results():
    client = RecordingHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.com/a",
                            "content": "Fresh result",
                        }
                    ]
                }
            ),
        )
    )
    search = TavilyWebSearch("tvly-secret", max_results=4, http_client=client)

    result = search.search("latest release")

    url, headers, payload = client.calls[0]
    assert url == "https://api.tavily.com/search"
    assert headers["Authorization"] == "Bearer tvly-secret"
    assert payload == {
        "query": "latest release",
        "search_depth": "basic",
        "max_results": 4,
        "include_answer": False,
        "include_raw_content": False,
    }
    assert result == (
        SearchResult(
            title="Example",
            url="https://example.com/a",
            snippet="Fresh result",
        ),
    )


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "auth_failed"), (429, "rate_limited"), (500, "search_failed")],
)
def test_tavily_search_maps_http_errors(status, code):
    search = TavilyWebSearch(
        "tvly-secret",
        http_client=RecordingHttpClient(
            HttpResponse(status=status, headers={}, body='{"detail":"secret"}')
        ),
    )

    with pytest.raises(WebSearchError) as exc_info:
        search.search("query")

    assert exc_info.value.code == code
    assert "secret" not in str(exc_info.value)


def test_tavily_search_rejects_empty_key_and_untrusted_host():
    with pytest.raises(ValueError):
        TavilyWebSearch("")
    with pytest.raises(ValueError):
        TavilyWebSearch("key", base_url="https://example.com")


def test_tool_loop_returns_direct_answer_without_search():
    provider = SequenceToolProvider(
        tool_turn("resp-1", text="already know", fallback_from="xai_oauth")
    )
    search = RecordingSearch(())
    loop = WebSearchToolLoop(provider, search)

    result = asyncio.run(loop.respond("hello"))

    assert result.response.text == "already know"
    assert result.response.fallback_from == "xai_oauth"
    assert result.sources == ()
    assert search.queries == []


def test_tool_loop_searches_reports_progress_and_returns_sources():
    provider = SequenceToolProvider(
        tool_turn("resp-1", calls=(web_call(),)),
        tool_turn("resp-2", text="доллар стоит 80"),
    )
    source = SearchResult(
        title="ЦБ РФ",
        url="https://cbr.ru/currency_base/daily/",
        snippet="USD 80",
    )
    search = RecordingSearch((source,))
    progress = []

    async def on_search_start(query):
        progress.append(query)

    result = asyncio.run(
        WebSearchToolLoop(provider, search).respond(
            "курс?",
            on_search_start=on_search_start,
        )
    )

    assert progress == ["курс доллара"]
    assert search.queries == ["курс доллара"]
    assert result.response.text == "доллар стоит 80"
    assert result.sources == (source,)
    previous_id, outputs, _ = provider.continuation_calls[0]
    assert previous_id == "resp-1"
    output = json.loads(outputs[0].output)
    assert output == {
        "results": [
            {
                "title": "ЦБ РФ",
                "url": "https://cbr.ru/currency_base/daily/",
                "content": "USD 80",
            }
        ]
    }


def test_tool_loop_adds_current_year_to_time_sensitive_query():
    provider = SequenceToolProvider(
        tool_turn(
            "resp-1",
            calls=(web_call(query="latest stable Python 3.13 version"),),
        ),
        tool_turn("resp-2", text="Python 3.13.13"),
    )
    search = RecordingSearch(())
    progress = []

    async def on_search_start(query):
        progress.append(query)

    asyncio.run(
        WebSearchToolLoop(
            provider,
            search,
            now=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
        ).respond("latest?", on_search_start=on_search_start)
    )

    assert progress == ["latest stable Python 3.13 version 2026"]
    assert search.queries == ["latest stable Python 3.13 version 2026"]


def test_tool_loop_returns_search_failure_to_model():
    provider = SequenceToolProvider(
        tool_turn("resp-1", calls=(web_call(),)),
        tool_turn("resp-2", text="поиск не сработал, отвечаю без него"),
    )
    search = RecordingSearch(WebSearchError("failed", code="rate_limited"))

    result = asyncio.run(WebSearchToolLoop(provider, search).respond("query"))

    assert result.response.text == "поиск не сработал, отвечаю без него"
    assert result.sources == ()
    _, outputs, _ = provider.continuation_calls[0]
    assert json.loads(outputs[0].output) == {"error": "search_unavailable"}


def test_tool_loop_rejects_unknown_tool_without_side_effect():
    provider = SequenceToolProvider(
        tool_turn(
            "resp-1",
            calls=(
                XaiFunctionCall(
                    call_id="call-1",
                    name="shell",
                    arguments='{"command":"dir"}',
                ),
            ),
        ),
        tool_turn("resp-2", text="cannot do that"),
    )
    search = RecordingSearch(())

    result = asyncio.run(WebSearchToolLoop(provider, search).respond("run shell"))

    assert result.response.text == "cannot do that"
    assert search.queries == []
    _, outputs, _ = provider.continuation_calls[0]
    assert json.loads(outputs[0].output) == {"error": "unknown_tool"}


def test_tool_loop_is_bounded_and_deduplicates_sources():
    first = SearchResult("First", "https://example.com/a", "one")
    duplicate = SearchResult("Duplicate", "https://example.com/a", "two")
    provider = SequenceToolProvider(
        tool_turn("resp-1", calls=(web_call("call-1", "one"),)),
        tool_turn("resp-2", calls=(web_call("call-2", "two"),)),
        tool_turn("resp-3", calls=(web_call("call-3", "three"),)),
        tool_turn("resp-4", text="done"),
    )
    search = RecordingSearch((first, duplicate))

    result = asyncio.run(
        WebSearchToolLoop(provider, search, max_searches=2).respond("query")
    )

    assert search.queries == ["one", "two"]
    assert result.sources == (first,)
    _, outputs, _ = provider.continuation_calls[2]
    assert json.loads(outputs[0].output) == {"error": "search_limit_reached"}


def test_tool_loop_requires_response_id_and_final_text():
    missing_id = SequenceToolProvider(
        tool_turn(None, calls=(web_call(),)),
    )
    with pytest.raises(ProviderRequestError) as missing_id_error:
        asyncio.run(WebSearchToolLoop(missing_id, RecordingSearch(())).respond("q"))
    assert missing_id_error.value.code == "invalid_response"

    exhausted = SequenceToolProvider(
        tool_turn("resp-1", calls=(web_call("call-1"),)),
        tool_turn("resp-2", calls=(web_call("call-2"),)),
        tool_turn("resp-3", calls=(web_call("call-3"),)),
        tool_turn("resp-4", calls=(web_call("call-4"),)),
    )
    with pytest.raises(ProviderRequestError) as exhausted_error:
        asyncio.run(
            WebSearchToolLoop(
                exhausted,
                RecordingSearch(()),
                max_searches=2,
            ).respond("q")
        )
    assert exhausted_error.value.code == "tool_loop_exhausted"
