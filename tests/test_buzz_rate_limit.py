import httpx
import pytest

from team_builder import buzz as buzz_module
from team_builder.buzz import Buzz
from team_builder.nostr import key

LIMITED = {"error": "rate-limited: quota exceeded; retry in 7s"}


def client(responses, **kwargs):
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    instance = Buzz("http://relay:3000", key(), "0" * 64, **kwargs)
    instance.client = httpx.Client(transport=httpx.MockTransport(handler))
    return instance, seen


def test_rate_limited_calls_wait_for_the_window_and_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(buzz_module.time, "sleep", sleeps.append)
    instance, seen = client([(429, LIMITED), (200, [])])
    assert instance.call("POST", "/query", [{"kinds": [1]}]) == []
    assert sleeps == [8]
    assert len(seen) == 2 and seen[0] != seen[1], "each attempt needs fresh NIP-98 auth"


def test_rate_limit_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(buzz_module.time, "sleep", lambda seconds: None)
    instance, seen = client([(429, LIMITED)] * 3)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        instance.call("POST", "/query", [])
    assert len(seen) == buzz_module.RATE_LIMIT_RETRIES + 1


def test_observer_style_clients_fail_fast(monkeypatch):
    monkeypatch.setattr(
        buzz_module.time, "sleep", lambda seconds: pytest.fail("unexpected wait")
    )
    instance, seen = client([(429, LIMITED)], wait_on_rate_limit=False)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        instance.call("POST", "/query", [])
    assert len(seen) == 1


def test_retry_wait_is_capped_and_defaults_without_a_hint():
    too_long = httpx.Response(429, json={"error": "retry in 600s"})
    assert buzz_module.retry_after(too_long) == buzz_module.RATE_LIMIT_MAX_WAIT
    assert buzz_module.retry_after(httpx.Response(429, text="slow down")) == 5
