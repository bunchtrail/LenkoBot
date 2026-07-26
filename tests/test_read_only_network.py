from datetime import datetime, timezone

import pytest

from lenkobot.read_only_network import (
    HttpFetchResponse,
    ReadOnlyUrlReader,
    UrlReadError,
)


class RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


class MappingResolver:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def resolve(self, hostname):
        self.calls.append(hostname)
        return self.mapping[hostname]


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def fetch(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def reader(resolver, transport, audit=None, **kwargs):
    return ReadOnlyUrlReader(
        audit or RecordingAudit(),
        resolver=resolver,
        transport=transport,
        now=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        **kwargs,
    )


def test_url_reader_pins_public_ip_and_returns_bounded_text():
    audit = RecordingAudit()
    resolver_impl = MappingResolver({"example.com": ("93.184.216.34",)})
    transport = QueueTransport(
        HttpFetchResponse(
            status=200,
            headers={"content-type": "text/plain; charset=utf-8"},
            body=b"hello",
        )
    )

    result = reader(resolver_impl, transport, audit).read(
        "https://example.com/hello",
        owner_user_id=42,
        lifecycle_epoch=3,
    )

    assert result.url == "https://example.com/hello"
    assert result.body == "hello"
    assert result.content_type == "text/plain"
    assert result.retrieved_at.isoformat() == "2026-07-23T12:00:00+00:00"
    assert transport.calls[0]["ip_address"] == "93.184.216.34"
    assert resolver_impl.calls == ["example.com"]
    assert audit.events[0].capability == "url_read"
    assert audit.events[0].policy_decision == "allow"
    assert audit.events[0].outcome == "success"
    assert len(audit.events[0].action_hash) == 64


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://example.com", "https_required"),
        ("https://127.0.0.1/", "private_address"),
        ("https://localhost/", "private_address"),
        ("https://[::1]/", "private_address"),
    ],
)
def test_url_reader_blocks_unsafe_targets_before_transport(url, code):
    audit = RecordingAudit()
    transport = QueueTransport()
    resolver_impl = MappingResolver({"example.com": ("93.184.216.34",)})

    with pytest.raises(UrlReadError) as exc_info:
        reader(resolver_impl, transport, audit).read(
            url,
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == code
    assert transport.calls == []
    assert audit.events[0].policy_decision == "deny"
    assert audit.events[0].outcome == "blocked"


def test_url_reader_rejects_private_or_mixed_dns_answers():
    audit = RecordingAudit()
    resolver_impl = MappingResolver(
        {"example.com": ("93.184.216.34", "10.0.0.8")}
    )
    transport = QueueTransport()

    with pytest.raises(UrlReadError) as exc_info:
        reader(resolver_impl, transport, audit).read(
            "https://example.com",
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == "dns_rebinding"
    assert transport.calls == []
    assert audit.events[0].outcome == "blocked"


def test_url_reader_revalidates_redirect_before_second_request():
    audit = RecordingAudit()
    resolver_impl = MappingResolver(
        {
            "example.com": ("93.184.216.34",),
            "internal.example": ("192.168.1.2",),
        }
    )
    transport = QueueTransport(
        HttpFetchResponse(
            status=302,
            headers={"location": "https://internal.example/private"},
            body=b"",
        )
    )

    with pytest.raises(UrlReadError) as exc_info:
        reader(resolver_impl, transport, audit).read(
            "https://example.com/start",
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == "private_address"
    assert len(transport.calls) == 1
    assert resolver_impl.calls == ["example.com", "internal.example"]
    assert audit.events[0].outcome == "blocked"


def test_url_reader_blocks_redirect_downgrade_to_http():
    audit = RecordingAudit()
    resolver_impl = MappingResolver({"example.com": ("93.184.216.34",)})
    transport = QueueTransport(
        HttpFetchResponse(
            status=302,
            headers={"location": "http://example.com/plain"},
            body=b"",
        )
    )

    with pytest.raises(UrlReadError) as exc_info:
        reader(resolver_impl, transport, audit).read(
            "https://example.com/start",
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == "https_required"
    assert len(transport.calls) == 1
    assert audit.events[0].policy_decision == "deny"
    assert audit.events[0].outcome == "blocked"


def test_url_reader_blocks_redirect_chain_over_limit():
    audit = RecordingAudit()
    resolver_impl = MappingResolver({"example.com": ("93.184.216.34",)})
    transport = QueueTransport(
        HttpFetchResponse(
            status=302,
            headers={"location": "https://example.com/next"},
            body=b"",
        )
    )

    with pytest.raises(UrlReadError) as exc_info:
        reader(resolver_impl, transport, audit, max_redirects=0).read(
            "https://example.com/start",
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == "unsafe_redirect"
    assert len(transport.calls) == 1
    assert audit.events[0].outcome == "blocked"


def test_url_reader_fails_closed_and_keeps_block_reason_when_audit_is_down():
    class FailingAudit:
        def record(self, event):
            raise RuntimeError("audit database is gone")

    transport = QueueTransport()
    resolver_impl = MappingResolver({"example.com": ("93.184.216.34",)})

    with pytest.raises(UrlReadError) as exc_info:
        reader(resolver_impl, transport, FailingAudit()).read(
            "http://example.com",
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == "audit_unavailable"
    assert isinstance(exc_info.value.__cause__, UrlReadError)
    assert exc_info.value.__cause__.code == "https_required"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            HttpFetchResponse(
                status=200,
                headers={"content-type": "application/octet-stream"},
                body=b"binary",
            ),
            "unsupported_content_type",
        ),
        (
            HttpFetchResponse(
                status=200,
                headers={"content-type": "text/plain"},
                body=b"12345",
            ),
            "body_too_large",
        ),
    ],
)
def test_url_reader_enforces_content_policy_and_body_limit(response, code):
    audit = RecordingAudit()
    resolver_impl = MappingResolver({"example.com": ("93.184.216.34",)})
    transport = QueueTransport(response)

    with pytest.raises(UrlReadError) as exc_info:
        reader(
            resolver_impl,
            transport,
            audit,
            max_body_bytes=4,
        ).read(
            "https://example.com/data",
            owner_user_id=42,
            lifecycle_epoch=1,
        )

    assert exc_info.value.code == code
    assert audit.events[0].outcome == "blocked"
