"""Cloud binding status must describe the current connection, not an old worker."""
import copy

import pytest
import requests

import cloud_report as report


CLOUD = {"enabled": True, "url": "https://cloud.example.test", "token": "test-token",
         "site_name": "Site A", "site_id": "site-a", "site_type": "air"}


class IdleThread:
    def __init__(self, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class Session:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_reporter(monkeypatch):
    monkeypatch.setattr(report, "_GEN", 0)
    monkeypatch.setattr(report, "_thread", None)
    monkeypatch.setattr(report, "_STATUS", {})
    monkeypatch.setattr(report.threading, "Thread", IdleThread)
    report.start({}, lambda: {}, lambda hours: [])


def start(cloud=None):
    report.start({"cloud": copy.deepcopy(CLOUD if cloud is None else cloud)},
                 lambda: {}, lambda hours: [])
    return report._GEN


@pytest.mark.parametrize("enabled", [True, False])
def test_new_connection_and_stop_clear_previous_results(enabled):
    old = start()
    report._set_status(old, last_ok=123, last_try=125, last_customers_ok=124,
                       last_err="old failure", fails=4, state="error")
    gen = start(dict(CLOUD, enabled=enabled, token="new-token"))
    state = report.status()
    assert gen > old and state["generation"] == gen
    assert state["state"] == ("pending" if enabled else "disconnected")
    assert state["last_ok"] == state["last_try"] == state["last_customers_ok"] == 0
    assert state["last_err"] == "" and state["fails"] == 0
    assert report.is_alive() == enabled


@pytest.mark.parametrize("enabled,fail", [(True, False), (False, False), (True, True)])
def test_old_inflight_summary_cannot_change_new_connection(monkeypatch, enabled, fail):
    gen = start()
    session = Session()
    monkeypatch.setattr(report, "_session", lambda c: session)
    calls = []

    def post(c, path, payload, sess):
        calls.append(path)
        start(dict(CLOUD, enabled=enabled, site_id="site-b", token="new-token"))
        if fail:
            raise requests.ConnectionError("old connection failed")

    monkeypatch.setattr(report, "_post", post)
    report._loop(gen, CLOUD, "site-a", lambda: {"ok": True, "scanned": True}, lambda h: [])
    state = report.status()
    assert state["generation"] > gen
    assert state["state"] == ("pending" if enabled else "disconnected")
    assert state["last_ok"] == state["last_try"] == state["last_customers_ok"] == 0
    assert not state["last_err"] and state["fails"] == 0
    assert calls == ["/api/ingest/summary"]
    assert session.closed


def test_old_inflight_customer_success_is_also_ignored(monkeypatch):
    gen = start()
    session = Session()
    monkeypatch.setattr(report, "_session", lambda c: session)
    calls = []

    def post(c, path, payload, sess):
        calls.append(path)
        if path == "/api/ingest/customers":
            start(dict(CLOUD, site_id="site-b"))

    monkeypatch.setattr(report, "_post", post)
    report._loop(gen, CLOUD, "site-a", lambda: {"ok": True, "scanned": True}, lambda h: [])
    assert calls == ["/api/ingest/summary", "/api/ingest/customers"]
    assert report.status()["state"] == "pending"
    assert report.status()["last_ok"] == report.status()["last_customers_ok"] == 0
    assert session.closed


@pytest.mark.parametrize("scanned,fail,expected", [
    (False, False, "waiting_scan"), (True, False, "connected"), (True, True, "error"),
])
def test_live_status_distinguishes_first_scan_success_and_retry(monkeypatch, scanned, fail, expected):
    gen = start()
    session = Session()
    monkeypatch.setattr(report, "_session", lambda c: session)
    sent, observed = [], []

    def post(c, path, payload, sess):
        sent.append(path)
        if fail:
            raise requests.ConnectionError("test unavailable")

    def end_iteration(current, seconds):
        observed.append(report.status())
        report._GEN += 1

    monkeypatch.setattr(report, "_post", post)
    monkeypatch.setattr(report, "_sleep", end_iteration)
    report._loop(gen, CLOUD, "site-a", lambda: {"ok": True, "scanned": scanned}, lambda h: [])
    state = observed[0]
    assert state["state"] == expected and state["generation"] == gen
    assert bool(state["last_ok"]) == (expected == "connected")
    assert bool(state["fails"]) == fail
    if not scanned:
        assert not sent and state["last_try"] == 0
    assert session.closed


def test_session_initialization_error_is_visible(monkeypatch):
    gen = start()

    def bad_session(c):
        raise ValueError("invalid session configuration")

    monkeypatch.setattr(report, "_session", bad_session)
    report._loop(gen, CLOUD, "site-a", lambda: {}, lambda h: [])
    assert report.status()["state"] == "error"
    assert report.status()["fails"] == 1 and not report.status()["last_ok"]

