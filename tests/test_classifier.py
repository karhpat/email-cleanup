"""Tests for backend/classifier.py."""
from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from backend.classifier import (
    Classifier,
    EmailForClassification,
    FakeClassifier,
    _BatchOutput,
    _Item,
)
from backend.models import CategoryDef


def categories():
    return [
        CategoryDef(key="finance", label_name="Finance", description="bank statements"),
        CategoryDef(key="receipts", label_name="Receipts & Orders", description="purchases"),
        CategoryDef(key="travel", label_name="Travel", description="flights, hotels"),
        CategoryDef(key="health", label_name="Health", description="doctors, pharmacy"),
        CategoryDef(key="home", label_name="Home", description="utilities, rent"),
        CategoryDef(key="work", label_name="Work & Career", description="job applications"),
        CategoryDef(key="personal", label_name="Personal", description="friends, family"),
        CategoryDef(key="government", label_name="Government & Legal", description="DMV, taxes"),
        CategoryDef(key="accounts", label_name="Accounts & Security", description="sign-in alerts"),
        CategoryDef(key="skip", label_name=None, description="marketing or newsletters"),
    ]


def make_email(mid="m1", sender="a@example.com", name="A", subject="hi", snippet="body", date="2026-01-01"):
    return EmailForClassification(
        message_id=mid, sender=sender, display_name=name, subject=subject, snippet=snippet, date=date,
    )


def usage_ns(input_tokens=100, output_tokens=20, cache_read_input_tokens=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


class FakeMessagesAPI:
    """Records every .parse() call's kwargs and returns queued responses.

    `responder` is a callable(kwargs) -> response object (SimpleNamespace with
    parsed_output/stop_reason/usage). If a list is passed instead, responses
    are returned in order (one per call).
    """

    def __init__(self, responder):
        self.calls = []
        self._responder = responder
        self._queue = None
        if isinstance(responder, list):
            self._queue = list(responder)
            self._responder = None

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._queue is not None:
            return self._queue.pop(0)
        return self._responder(kwargs)


class FakeAnthropicClient:
    def __init__(self, responder):
        self.messages = FakeMessagesAPI(responder)


def make_response(items, stop_reason="end_turn", usage=None):
    parsed = _BatchOutput(results=[_Item(**it) for it in items]) if items is not None else None
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=usage or usage_ns(),
    )


# ---------- basic request shape ----------

def test_classify_uses_documented_request_shape_for_cheap_model():
    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        return make_response([{"id": i, "category": "finance", "confidence": 0.95, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    emails = [make_email("m1")]
    results = clf.classify(emails)

    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == 4096
    assert call["output_format"] is _BatchOutput
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Haiku must not get thinking or output_config (no effort param).
    assert "thinking" not in call
    assert "output_config" not in call

    assert results[0].category == "finance"
    assert results[0].confidence == 0.95
    assert results[0].model == "claude-haiku-4-5"


def test_sonnet_gets_output_config_effort_low():
    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        conf = 0.2 if kwargs["model"] == "claude-haiku-4-5" else 0.99
        return make_response([{"id": i, "category": "finance", "confidence": conf, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    clf.classify([make_email("m1")])

    assert len(client.messages.calls) == 2
    cheap_call, strong_call = client.messages.calls
    assert cheap_call["model"] == "claude-haiku-4-5"
    assert "output_config" not in cheap_call
    assert strong_call["model"] == "claude-sonnet-5"
    assert strong_call["output_config"] == {"effort": "low"}


# ---------- escalation ----------

def test_low_confidence_result_is_escalated_and_replaced():
    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        if kwargs["model"] == "claude-haiku-4-5":
            return make_response([{"id": i, "category": "finance", "confidence": 0.3, "reason": "unsure"} for i in ids])
        return make_response([{"id": i, "category": "travel", "confidence": 0.99, "reason": "sure"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    results = clf.classify([make_email("m1")])

    assert len(results) == 1
    assert results[0].category == "travel"
    assert results[0].confidence == 0.99
    assert results[0].model == "claude-sonnet-5"
    assert clf.usage["escalated"] == 1
    assert clf.usage["requests"] == 2


def test_high_confidence_result_is_not_escalated():
    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        return make_response([{"id": i, "category": "finance", "confidence": 0.9, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    clf.classify([make_email("m1")])

    assert len(client.messages.calls) == 1
    assert clf.usage["escalated"] == 0


def test_on_progress_and_should_cancel():
    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        return make_response([{"id": i, "category": "finance", "confidence": 0.9, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    emails = [make_email(f"m{i}") for i in range(45)]  # 3 batches of 20/20/5
    progress_calls = []

    def on_progress(done, total, message):
        progress_calls.append((done, total))

    results = clf.classify(emails, on_progress=on_progress)
    assert len(results) == 45
    assert progress_calls == [(20, 45), (40, 45), (45, 45)]

    # should_cancel stops early, before all batches run.
    call_count = {"n": 0}

    def should_cancel():
        call_count["n"] += 1
        return call_count["n"] > 1

    client2 = FakeAnthropicClient(responder)
    clf2 = Classifier(client2, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)
    results2 = clf2.classify(emails, should_cancel=should_cancel)
    assert len(results2) < 45


# ---------- validation ----------

def test_unknown_category_becomes_skip():
    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        return make_response([{"id": i, "category": "not-a-real-category", "confidence": 0.9, "reason": "x"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    results = clf.classify([make_email("m1")])
    assert results[0].category == "skip"
    assert results[0].confidence == 0.0


def test_missing_id_becomes_skip():
    def responder(kwargs):
        # Always answer with a different id than what was asked.
        return make_response([{"id": "does-not-exist", "category": "finance", "confidence": 0.9, "reason": "x"}])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    results = clf.classify([make_email("m1")])
    assert len(results) == 1
    assert results[0].message_id == "m1"
    assert results[0].category == "skip"
    assert results[0].confidence == 0.0


def test_refusal_marks_whole_batch_skip_refused():
    def responder(kwargs):
        return make_response(None, stop_reason="refusal")

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    results = clf.classify([make_email("m1"), make_email("m2")])
    assert len(results) == 2
    for r in results:
        assert r.category == "skip"
        assert r.reason == "refused"


def test_max_tokens_splits_batch_and_retries():
    calls = {"n": 0}

    def responder(kwargs):
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        calls["n"] += 1
        if len(ids) > 1:
            # First call over the full batch hits max_tokens; force a split.
            return make_response(None, stop_reason="max_tokens")
        return make_response([{"id": i, "category": "finance", "confidence": 0.9, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    emails = [make_email("m1"), make_email("m2")]
    results = clf.classify(emails)

    assert len(results) == 2
    assert all(r.category == "finance" for r in results)
    # One failed call over the pair, then two successful single-email calls.
    assert calls["n"] == 3


# ---------- error handling ----------

def test_rate_limit_error_retries_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("backend.classifier.time.sleep", lambda s: sleeps.append(s))

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(429, request=req, headers={"retry-after": "7"})
    rate_limit_error = anthropic.RateLimitError("rate limited", response=resp, body=None)

    attempts = {"n": 0}

    def responder(kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise rate_limit_error
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        return make_response([{"id": i, "category": "finance", "confidence": 0.9, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    results = clf.classify([make_email("m1")])
    assert results[0].category == "finance"
    assert sleeps == [7, 7]


def test_server_error_backs_off_and_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("backend.classifier.time.sleep", lambda s: sleeps.append(s))

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(500, request=req)
    server_error = anthropic.APIStatusError("boom", response=resp, body=None)

    attempts = {"n": 0}

    def responder(kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise server_error
        ids = [e["id"] for e in __import__("json").loads(kwargs["messages"][0]["content"])]
        return make_response([{"id": i, "category": "finance", "confidence": 0.9, "reason": "ok"} for i in ids])

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    results = clf.classify([make_email("m1")])
    assert results[0].category == "finance"
    assert sleeps == [2]


def test_client_error_is_raised_not_retried():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(400, request=req)
    bad_request = anthropic.APIStatusError("bad request", response=resp, body=None)

    def responder(kwargs):
        raise bad_request

    client = FakeAnthropicClient(responder)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)

    with pytest.raises(anthropic.APIStatusError):
        clf.classify([make_email("m1")])


# ---------- cost estimate ----------

def test_estimate_cost_usd_zero():
    client = FakeAnthropicClient(lambda kwargs: None)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)
    assert clf.estimate_cost_usd(0) == 0.0


def test_estimate_cost_usd_one():
    client = FakeAnthropicClient(lambda kwargs: None)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)
    # 1 email: 165 input + 30 output tokens on haiku ($1/$5 per M),
    # plus 0.15 escalated email's worth on sonnet ($2/$10 per M).
    cheap = (165 / 1_000_000) * 1.00 + (30 / 1_000_000) * 5.00
    strong = (0.15 * 165 / 1_000_000) * 2.00 + (0.15 * 30 / 1_000_000) * 10.00
    import math
    expected = math.ceil((cheap + strong) * 100) / 100
    assert clf.estimate_cost_usd(1) == expected
    assert clf.estimate_cost_usd(1) == 0.01


def test_estimate_cost_usd_one_thousand():
    client = FakeAnthropicClient(lambda kwargs: None)
    clf = Classifier(client, categories(), "claude-haiku-4-5", "claude-sonnet-5", escalate_below=0.6)
    cheap = (1000 * 165 / 1_000_000) * 1.00 + (1000 * 30 / 1_000_000) * 5.00
    strong = (150 * 165 / 1_000_000) * 2.00 + (150 * 30 / 1_000_000) * 10.00
    import math
    expected = math.ceil((cheap + strong) * 100) / 100
    assert clf.estimate_cost_usd(1000) == expected


# ---------- FakeClassifier ----------

@pytest.mark.parametrize("subject,snippet,expected", [
    ("Your order has shipped", "", "receipts"),
    ("", "Your account statement is ready, payment due soon", "finance"),
    ("Flight itinerary confirmed", "", "travel"),
    ("Appointment reminder", "see your doctor", "health"),
    ("Your utility bill is ready", "", "home"),
    ("Interview request", "we'd like to schedule an interview", "work"),
    ("Verify your email", "sign-in attempt detected", "accounts"),
    ("Jury duty notice", "", "government"),
    ("Buy now, 50% off!", "limited time offer", "skip"),
])
def test_fake_classifier_keyword_rules(subject, snippet, expected):
    clf = FakeClassifier(categories(), escalate_below=0.6)
    email = make_email(subject=subject, snippet=snippet, sender="biz@company.com")
    result = clf.classify([email])[0]
    assert result.category == expected
    assert result.model == "fake"
    if expected == "skip":
        assert result.confidence == 0.4
    else:
        assert result.confidence == 0.9


def test_fake_classifier_personal_sender():
    clf = FakeClassifier(categories(), escalate_below=0.6)
    email = make_email(subject="Re: dinner next week", snippet="sounds great!", sender="friend@gmail.com")
    result = clf.classify([email])[0]
    assert result.category == "personal"
    assert result.confidence == 0.9


def test_fake_classifier_estimate_cost_is_zero():
    clf = FakeClassifier(categories())
    assert clf.estimate_cost_usd(1000) == 0.0
