import pytest
import requests

from beetsplug.crossref import http
from beetsplug.crossref.cache import MISSING, Cache
from beetsplug.crossref.http import JsonClient, SourceUnavailable


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / "sub" / "c.db", 30)
    yield c
    c.close()


def test_get_unknown_is_missing(cache):
    assert cache.get("ns", "k") is MISSING


def test_none_is_cached_and_returned(cache):
    calls = []
    assert cache.remember("ns", "k", lambda: calls.append(1)) is None
    assert cache.remember("ns", "k", lambda: calls.append(1)) is None
    assert cache.get("ns", "k") is None
    assert len(calls) == 1


def test_remember_does_not_store_missing(cache):
    assert cache.remember("ns", "k", lambda: MISSING) is MISSING
    assert cache.get("ns", "k") is MISSING
    assert cache.remember("ns", "k", lambda: ["x"]) == ["x"]


def test_namespaces_are_separate(cache):
    cache.put("a", "k", 1)
    assert cache.get("b", "k") is MISSING


def test_expiry_by_max_age_days(cache):
    cache.put("ns", "old", {"a": 1})
    cache.put("ns", "new", {"a": 2})
    cache.conn.execute("UPDATE entries SET fetched_at = fetched_at - ? WHERE key='old'", (31 * 86400,))
    assert cache.get("ns", "old") is MISSING
    assert cache.get("ns", "new") == {"a": 2}


def test_persists_across_instances(tmp_path):
    first = Cache(tmp_path / "c.db")
    first.put("ns", "k", [1, 2])
    first.close()
    second = Cache(tmp_path / "c.db")
    assert second.get("ns", "k") == [1, 2]
    second.close()


# --- http.JsonClient ---------------------------------------------------------

class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def client(monkeypatch):
    sleeps = []
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    c = JsonClient("svc", "https://x.test/api", attempts=3, max_wait=10)
    c.sleeps = sleeps
    return c


def script(client, monkeypatch, *responses):
    queue = list(responses)
    urls = []

    def fake_get(url, **kwargs):
        urls.append(url)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client.session, "get", fake_get)
    return urls


def test_http_ok_returns_json_and_builds_url(client, monkeypatch):
    urls = script(client, monkeypatch, Resp(200, {"a": 1}))
    assert client.get("/album/1", q="x") == {"a": 1}
    assert urls == ["https://x.test/api/album/1"]


def test_http_404_is_none(client, monkeypatch):
    script(client, monkeypatch, Resp(404))
    assert client.get("album/1") is None
    assert client.calls == 1


def test_http_429_long_retry_after_raises_without_sleeping(client, monkeypatch):
    script(client, monkeypatch, Resp(429, headers={"Retry-After": "3600"}))
    with pytest.raises(SourceUnavailable):
        client.get("album/1")
    assert client.sleeps == []


def test_http_429_short_retry_after_is_honoured(client, monkeypatch):
    script(client, monkeypatch, Resp(429, headers={"Retry-After": "3"}), Resp(200, {"ok": True}))
    assert client.get("album/1") == {"ok": True}
    assert client.sleeps == [3.0]


def test_http_5xx_retried_then_missing(client, monkeypatch):
    script(client, monkeypatch, Resp(503), Resp(500), Resp(502))
    assert client.get("album/1") is MISSING
    assert client.calls == 3
    assert client.sleeps == [2.0, 4.0, 8.0]


def test_http_5xx_then_success(client, monkeypatch):
    script(client, monkeypatch, Resp(503), Resp(200, [1]))
    assert client.get("album/1") == [1]


def test_http_network_error_retried(client, monkeypatch):
    script(client, monkeypatch, requests.ConnectionError("down"), Resp(200, {"a": 1}))
    assert client.get("album/1") == {"a": 1}
    assert client.sleeps == [2.0]


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_refusal_raises(client, monkeypatch, status):
    script(client, monkeypatch, Resp(status))
    with pytest.raises(SourceUnavailable):
        client.get("album/1")
    assert client.calls == 1


def test_http_other_4xx_returns_json_body_or_missing(client, monkeypatch):
    script(client, monkeypatch, Resp(400, {"error": 6}), Resp(400, None))
    assert client.get("album/1") == {"error": 6}
    monkeypatch.setattr(Resp, "json", lambda self: (_ for _ in ()).throw(ValueError("no json")))
    script(client, monkeypatch, Resp(400))
    assert client.get("album/2") is MISSING
