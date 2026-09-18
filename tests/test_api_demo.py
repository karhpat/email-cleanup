"""API smoke tests in DEMO=1 mode via fastapi TestClient.

DEMO and DATA_DIR must be set before backend.api (and backend.config) import,
since Config reads the environment at import time.
"""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import sys

    for mod in list(sys.modules):
        if mod == "backend" or mod.startswith("backend."):
            del sys.modules[mod]

    from fastapi.testclient import TestClient

    import backend.api as api_module

    with TestClient(api_module.app) as c:
        yield c, api_module


def _wait_for_job(client, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get("/api/status")
        job = resp.json().get("job")
        if job is None or job["state"] != "running":
            return job
        time.sleep(0.05)
    raise TimeoutError("job did not finish in time")


def test_health(client):
    c, _ = client
    resp = c.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_status_demo_mode(client):
    c, _ = client
    resp = c.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "demo"
    assert data["authenticated"] is True
    assert data["counts"]["messages"] > 0
    assert data["counts"]["senders"] > 0
    assert data["anthropic_configured"] is False


def test_overview_demo_mode(client):
    c, _ = client
    resp = c.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_total"] > 0
    assert data["senders_total"] > 0
    assert "promotions" in data["by_category"] or "updates" in data["by_category"]
    assert isinstance(data["top_senders"], list)
    assert len(data["top_senders"]) > 0


def test_senders_with_filters(client):
    c, _ = client
    resp = c.get("/api/senders", params={"min_total": 3, "max_open_rate": 0.1, "status": "active"})
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert isinstance(data["items"], list)
    for item in data["items"]:
        assert item["total"] >= 3
        assert item["open_rate"] <= 0.1

    resp2 = c.get("/api/senders", params={"categories": "promotions,updates", "min_total": 1})
    assert resp2.status_code == 200
    for item in resp2.json()["items"]:
        assert item["category"] in ("promotions", "updates")


def test_sender_messages(client):
    c, _ = client
    senders = c.get("/api/senders", params={"min_total": 1, "limit": 1, "status": "active"}).json()["items"]
    assert senders
    sender = senders[0]["sender"]
    resp = c.get(f"/api/senders/{sender}/messages", params={"limit": 5})
    assert resp.status_code == 200
    assert len(resp.json()["items"]) <= 5


def test_actions_preview_execute_log_and_undo(client):
    c, _ = client
    senders = c.get(
        "/api/senders", params={"subscription_only": "true", "min_total": 1, "status": "active", "limit": 3}
    ).json()["items"]
    assert senders
    sender_names = [s["sender"] for s in senders[:2]]

    preview_resp = c.post(
        "/api/actions/preview",
        json={"action": "stop", "senders": sender_names, "unsubscribe": True, "block": True},
    )
    assert preview_resp.status_code == 200
    preview_data = preview_resp.json()
    assert preview_data["action"] == "stop"
    assert len(preview_data["items"]) == len(sender_names)

    exec_resp = c.post(
        "/api/actions/execute",
        json={"action": "stop", "senders": sender_names, "unsubscribe": True, "block": True},
    )
    assert exec_resp.status_code == 200
    job_id = exec_resp.json()["job_id"]
    assert job_id
    job_state = _wait_for_job(c)
    assert job_state["state"] == "done"

    log_resp = c.get("/api/actions/log", params={"limit": 50, "offset": 0})
    assert log_resp.status_code == 200
    log_data = log_resp.json()
    assert log_data["total"] >= 1
    block_rows = [i for i in log_data["items"] if i["action"] == "block"]
    assert block_rows
    assert all(i["status"] == "simulated" for i in block_rows)

    undoable = [i for i in block_rows if i["undoable"] and not i["undone"]]
    assert undoable
    undo_resp = c.post(f"/api/actions/undo/{undoable[0]['id']}")
    assert undo_resp.status_code == 200
    assert undo_resp.json() == {"ok": True}

    # a second undo of the same row must fail with 400
    undo_again = c.post(f"/api/actions/undo/{undoable[0]['id']}")
    assert undo_again.status_code == 400
    assert "error" in undo_again.json()


def test_execute_job_busy_returns_409(client):
    c, _ = client
    senders = c.get("/api/senders", params={"min_total": 1, "status": "active", "limit": 5}).json()["items"]
    sender_names = [s["sender"] for s in senders]
    body = {"action": "keep", "senders": sender_names}
    r1 = c.post("/api/actions/execute", json=body)
    assert r1.status_code == 200
    r2 = c.post("/api/actions/execute", json=body)
    assert r2.status_code == 409
    assert "error" in r2.json()
    _wait_for_job(c)


def test_scan_endpoint_runs_in_demo(client):
    c, _ = client
    resp = c.post("/api/scan", json={"window_months": 12, "full": False})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert job_id
    job_state = _wait_for_job(c)
    assert job_state["state"] in ("done", "error")


def test_settings_roundtrip(client):
    c, _ = client
    resp = c.get("/api/settings")
    assert resp.status_code == 200
    current = resp.json()
    assert current["window_months"] == 12

    put_resp = c.put("/api/settings", json={"window_months": 6})
    assert put_resp.status_code == 200
    assert put_resp.json()["window_months"] == 6

    resp2 = c.get("/api/settings")
    assert resp2.json()["window_months"] == 6


def test_labels_endpoint(client):
    c, _ = client
    resp = c.get("/api/labels")
    assert resp.status_code == 200
    assert isinstance(resp.json()["items"], list)


def test_organize_routes(client):
    """backend.organizer is owned by another engineer and may or may not be
    present; either way api.py must behave per contract (503 vs full flow)."""
    c, _ = client
    try:
        import backend.organizer  # noqa: F401

        organizer_available = True
    except ImportError:
        organizer_available = False

    resp = c.get("/api/organize/categories")
    if not organizer_available:
        assert resp.status_code == 503
        assert resp.json() == {"error": "organizer not available"}
        return

    assert resp.status_code == 200
    cats = resp.json()["items"]
    assert any(cat["key"] == "personal" for cat in cats)

    est = c.get("/api/organize/estimate", params={"scope": "inbox", "days": 365})
    assert est.status_code == 200
    assert est.json()["candidates"] >= 0

    classify_resp = c.post("/api/organize/classify", json={"scope": "inbox", "days": 365, "limit": 20})
    assert classify_resp.status_code == 200
    job_state = _wait_for_job(c)
    assert job_state["state"] == "done"

    results_resp = c.get("/api/organize/results", params={"limit": 50, "offset": 0})
    assert results_resp.status_code == 200
    items = results_resp.json()["items"]
    assert isinstance(items, list)

    if items:
        mid = items[0]["message_id"]
        override_resp = c.put(f"/api/organize/results/{mid}", json={"category": "work"})
        assert override_resp.status_code == 200

    apply_resp = c.post("/api/organize/apply", json={"archive": False, "mark_read": False})
    assert apply_resp.status_code == 200
    apply_state = _wait_for_job(c)
    assert apply_state["state"] == "done"


def test_index_route(client):
    c, api_module = client
    resp = c.get("/")
    if (api_module.FRONTEND_DIR / "index.html").exists():
        assert resp.status_code == 200
    else:
        assert resp.status_code == 404
        assert "error" in resp.json()


def test_put_categories_round_trip(client):
    client, _api = client
    r = client.get("/api/organize/categories")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["key"] == "skip" for i in items)

    # Rename one label and drop "skip" from the payload; the API must accept
    # plain dicts and the organizer must keep a skip entry regardless.
    edited = [i for i in items if i["key"] != "skip"]
    edited[0]["label_name"] = "Cleanup/Renamed"
    r = client.put("/api/organize/categories", json={"items": edited})
    assert r.status_code == 200, r.text
    saved = {i["key"]: i for i in r.json()["items"]}
    assert saved[edited[0]["key"]]["label_name"] == "Cleanup/Renamed"
    assert "skip" in saved and saved["skip"]["label_name"] is None

    r = client.put("/api/organize/categories", json={"items": [{"nope": 1}]})
    assert r.status_code == 400


def test_demo_scan_preserves_seeded_mailbox(client):
    """A demo scan runs the real scanner against the seeded mailbox and must
    leave the counts and read state as they were (regression: it used to
    mark every message gone because the demo client listed no ids)."""
    c, _ = client
    before = c.get("/api/overview").json()
    assert before["messages_total"] > 0

    resp = c.post("/api/scan", json={"window_months": 0, "full": False})
    assert resp.status_code == 200
    state = _wait_for_job(c, timeout=60)
    assert state["state"] == "done", state

    after = c.get("/api/overview").json()
    assert after["messages_total"] == before["messages_total"]
    assert after["unread_total"] == before["unread_total"]
    assert after["senders_total"] == before["senders_total"]

    # A full scan refetches metadata through the demo client and must agree too.
    resp = c.post("/api/scan", json={"window_months": 0, "full": True})
    assert resp.status_code == 200
    state = _wait_for_job(c, timeout=120)
    assert state["state"] == "done", state
    again = c.get("/api/overview").json()
    assert again["messages_total"] == before["messages_total"]
    assert again["unread_total"] == before["unread_total"]
