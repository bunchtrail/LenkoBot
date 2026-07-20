import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Protocol
from urllib.parse import urlsplit

from ddgs import DDGS
from ddgs.exceptions import RatelimitException

from .xai_provider import (
    HttpResponse,
    JsonHttpClient,
    ProviderRequestError,
    UrllibJsonHttpClient,
    XaiFunctionCall,
    XaiPrompt,
    XaiTextResponse,
    XaiToolOutput,
    XaiToolTurn,
)


_MAX_QUERY_CHARS = 300
_MAX_TITLE_CHARS = 200
_MAX_URL_CHARS = 2048
_MAX_SNIPPET_CHARS = 1200
_MAX_RESULTS_LIMIT = 10
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
_TEMPORAL_QUERY_MARKERS = (
    "latest",
    "current",
    "today",
    "recent",
    "newest",
    "now",
    "актуальн",
    "новост",
    "последн",
    "свеж",
    "сейчас",
    "сегодня",
)

WEB_SEARCH_TOOL: dict[str, object] = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the public web for current or missing information, including "
        "recent events, news, prices, weather, facts and links."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A concise, specific web search query. For current or latest "
                    "information, prefer primary sources and include the relevant date."
                ),
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
WEB_SEARCH_TOOLS = (WEB_SEARCH_TOOL,)


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class WebSearchError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WebSearchPort(Protocol):
    def search(self, query: str) -> tuple[SearchResult, ...]: ...


class _DdgsClient(Protocol):
    def text(self, query: str, *, max_results: int) -> object: ...


class ToolTurnProvider(Protocol):
    def respond_with_tools(
        self,
        prompt: XaiPrompt,
        *,
        tools: tuple[dict[str, object], ...],
    ) -> XaiToolTurn: ...

    def respond_with_tool_outputs(
        self,
        *,
        previous_response_id: str,
        tool_outputs: tuple[XaiToolOutput, ...],
        tools: tuple[dict[str, object], ...],
    ) -> XaiToolTurn: ...


@dataclass(frozen=True, slots=True)
class ToolLoopResult:
    response: XaiTextResponse
    sources: tuple[SearchResult, ...]


class DdgsWebSearch:
    def __init__(
        self,
        *,
        max_results: int = 5,
        ddgs_factory: Callable[[], _DdgsClient] | None = None,
    ) -> None:
        self._max_results = _validate_max_results(max_results)
        self._ddgs_factory = ddgs_factory or DDGS

    def search(self, query: str) -> tuple[SearchResult, ...]:
        normalized_query = _normalize_query(query)
        try:
            raw_results = self._ddgs_factory().text(
                normalized_query,
                max_results=self._max_results,
            )
            return _map_results(raw_results, url_key="href", snippet_key="body")
        except RatelimitException:
            raise WebSearchError(
                "web search rate limit reached",
                code="rate_limited",
            ) from None
        except WebSearchError:
            raise
        except Exception:
            raise WebSearchError("web search failed", code="search_failed") from None


class TavilyWebSearch:
    def __init__(
        self,
        api_key: str,
        *,
        max_results: int = 5,
        http_client: JsonHttpClient | None = None,
        base_url: str = "https://api.tavily.com",
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Tavily API key cannot be empty")
        self._base_url = _validate_tavily_base_url(base_url)
        self._api_key = api_key.strip()
        self._max_results = _validate_max_results(max_results)
        self._http_client = http_client or UrllibJsonHttpClient()

    def search(self, query: str) -> tuple[SearchResult, ...]:
        normalized_query = _normalize_query(query)
        try:
            response = self._http_client.post_json(
                f"{self._base_url}/search",
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                {
                    "query": normalized_query,
                    "search_depth": "basic",
                    "max_results": self._max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
            )
        except ProviderRequestError:
            raise WebSearchError("web search failed", code="search_failed") from None
        if not 200 <= response.status < 300:
            raise WebSearchError(
                "web search request failed",
                code=_http_error_code(response.status),
            )
        body = _decode_search_response(response)
        raw_results = body.get("results")
        if not isinstance(raw_results, list):
            raise WebSearchError("web search returned invalid data", code="search_failed")
        return _map_results(raw_results, url_key="url", snippet_key="content")


class WebSearchToolLoop:
    def __init__(
        self,
        provider: ToolTurnProvider,
        search: WebSearchPort,
        *,
        max_searches: int = 2,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_searches, bool) or not isinstance(max_searches, int):
            raise ValueError("max searches must be a positive integer")
        if max_searches < 1 or max_searches > 5:
            raise ValueError("max searches must be between 1 and 5")
        self._provider = provider
        self._search = search
        self._max_searches = max_searches
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def respond(
        self,
        prompt: XaiPrompt,
        *,
        on_search_start: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolLoopResult:
        turn = await asyncio.to_thread(
            self._provider.respond_with_tools,
            prompt,
            tools=WEB_SEARCH_TOOLS,
        )
        sources: list[SearchResult] = []
        searches = 0
        rounds = 0
        while turn.tool_calls:
            if rounds >= self._max_searches + 1:
                raise _tool_loop_error(
                    "tool_loop_exhausted",
                    "web search tool loop did not produce a final answer",
                )
            if turn.response_id is None or not turn.response_id.strip():
                raise _tool_loop_error(
                    "invalid_response",
                    "xAI tool response contained no response ID",
                )
            outputs = []
            for call in turn.tool_calls:
                output, did_search = await self._execute_call(
                    call,
                    allow_search=searches < self._max_searches,
                    on_search_start=on_search_start,
                    sources=sources,
                )
                searches += int(did_search)
                outputs.append(output)
            turn = await asyncio.to_thread(
                self._provider.respond_with_tool_outputs,
                previous_response_id=turn.response_id,
                tool_outputs=tuple(outputs),
                tools=WEB_SEARCH_TOOLS,
            )
            rounds += 1
        if turn.text is None:
            raise _tool_loop_error(
                "tool_loop_exhausted",
                "web search tool loop did not produce a final answer",
            )
        return ToolLoopResult(
            response=XaiTextResponse(
                response_id=turn.response_id,
                model=turn.model,
                text=turn.text,
                credential_source=turn.credential_source,
                fallback_from=turn.fallback_from,
            ),
            sources=_deduplicate_sources(sources),
        )

    async def _execute_call(
        self,
        call: XaiFunctionCall,
        *,
        allow_search: bool,
        on_search_start: Callable[[str], Awaitable[None]] | None,
        sources: list[SearchResult],
    ) -> tuple[XaiToolOutput, bool]:
        if call.name != "web_search":
            return _error_output(call.call_id, "unknown_tool"), False
        query = _query_from_arguments(call.arguments)
        if query is None:
            return _error_output(call.call_id, "invalid_query"), False
        query = _add_freshness_context(query, self._now())
        if not allow_search:
            return _error_output(call.call_id, "search_limit_reached"), False
        if on_search_start is not None:
            try:
                await on_search_start(query)
            except Exception:
                pass
        try:
            results = await asyncio.to_thread(self._search.search, query)
        except Exception:
            return _error_output(call.call_id, "search_unavailable"), True
        sources.extend(results)
        payload = {
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "content": result.snippet,
                }
                for result in results
            ]
        }
        return (
            XaiToolOutput(
                call_id=call.call_id,
                output=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
            True,
        )


def _validate_max_results(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("web search max_results must be an integer")
    if value < 1 or value > _MAX_RESULTS_LIMIT:
        raise ValueError("web search max_results must be between 1 and 10")
    return value


def _normalize_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("web search query cannot be empty")
    return query.strip()[:_MAX_QUERY_CHARS]


def _map_results(
    value: object,
    *,
    url_key: str,
    snippet_key: str,
) -> tuple[SearchResult, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    results = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        url = item.get(url_key)
        snippet = item.get(snippet_key, "")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(url, str) or not _is_public_web_url(url):
            continue
        if not isinstance(snippet, str):
            snippet = ""
        results.append(
            SearchResult(
                title=title.strip()[:_MAX_TITLE_CHARS],
                url=url.strip()[:_MAX_URL_CHARS],
                snippet=snippet.strip()[:_MAX_SNIPPET_CHARS],
            )
        )
    return tuple(results)


def _is_public_web_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _validate_tavily_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("Tavily API target is not an allowed HTTPS host") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.tavily.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Tavily API target is not an allowed HTTPS host")
    return value.rstrip("/")


def _http_error_code(status: int) -> str:
    if status in {401, 403}:
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    return "search_failed"


def _decode_search_response(response: HttpResponse) -> dict[str, object]:
    try:
        body = json.loads(response.body)
    except json.JSONDecodeError:
        raise WebSearchError("web search returned invalid data", code="search_failed") from None
    if not isinstance(body, dict):
        raise WebSearchError("web search returned invalid data", code="search_failed")
    return body


def _query_from_arguments(arguments: str) -> str | None:
    try:
        value = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        return _normalize_query(value.get("query"))
    except ValueError:
        return None


def _add_freshness_context(query: str, now: datetime) -> str:
    lowered = query.casefold()
    if _YEAR_PATTERN.search(query):
        return query
    if not any(marker in lowered for marker in _TEMPORAL_QUERY_MARKERS):
        return query
    suffix = f" {now.year}"
    return query[: _MAX_QUERY_CHARS - len(suffix)].rstrip() + suffix


def _error_output(call_id: str, code: str) -> XaiToolOutput:
    return XaiToolOutput(
        call_id=call_id,
        output=json.dumps({"error": code}, separators=(",", ":")),
    )


def _deduplicate_sources(
    sources: list[SearchResult],
) -> tuple[SearchResult, ...]:
    unique = {}
    for source in sources:
        unique.setdefault(source.url, source)
    return tuple(unique.values())


def _tool_loop_error(code: str, message: str) -> ProviderRequestError:
    return ProviderRequestError(
        message,
        status=None,
        code=code,
        raw_body="",
        headers={},
    )
