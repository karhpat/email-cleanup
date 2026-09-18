"""Claude-based email classification (Organize step).

See docs/ARCHITECTURE.md -> "Classifier contract" for the exact request
shape, validation rules, error handling and cost formula. This module must
not deviate from that contract.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

import anthropic
from pydantic import BaseModel

from backend.models import CategoryDef

BATCH_SIZE = 20

# {model: (input $ / 1M tokens, output $ / 1M tokens)}
PRICE_PER_MILLION = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
}

# Cost estimate constants (see ARCHITECTURE.md -> Classifier contract).
INPUT_TOKENS_PER_EMAIL = 130
SYSTEM_TOKENS_PER_BATCH = 700
OUTPUT_TOKENS_PER_EMAIL = 30
ESCALATION_FRACTION = 0.15


@dataclass
class EmailForClassification:
    message_id: str
    sender: str
    display_name: str
    subject: str
    snippet: str
    date: str


@dataclass
class ClassificationResult:
    message_id: str
    category: str
    confidence: float
    reason: str
    model: str


# ---------- Anthropic structured-output schema ----------

class _Item(BaseModel):
    id: str
    category: str
    confidence: float
    reason: str


class _BatchOutput(BaseModel):
    results: list[_Item]


def _build_system_prompt(categories: list[CategoryDef]) -> str:
    """Stable, cacheable system prompt built once from the category list.

    No timestamps or other volatile content - the same categories always
    produce byte-identical text so the prompt cache prefix stays valid.
    """
    lines = [
        "You are an email triage assistant. Classify each email into exactly "
        "one of the following categories, identified by its key:",
        "",
    ]
    for cat in categories:
        lines.append(f"- {cat.key}: {cat.description}")
    lines.extend([
        "",
        'Use the category "skip" for marketing, newsletters, promotions, or '
        "anything else not worth labeling.",
        "Every email must be classified into exactly one of the category keys "
        'listed above (use "skip" when none of the substantive categories fit).',
        "confidence is a number from 0 to 1: your honest probability that the "
        "label you chose is correct. Do not default to a fixed number - vary it "
        "with how sure you actually are.",
        "reason is a short justification of 12 words or fewer.",
        "Return exactly one result for every input id, and never invent ids "
        "that were not given to you.",
    ])
    return "\n".join(lines)


def _email_to_json_dict(email: EmailForClassification) -> dict:
    return {
        "id": email.message_id,
        "from": email.sender,
        "name": email.display_name,
        "subject": email.subject,
        "snippet": email.snippet,
        "date": email.date,
    }


class Classifier:
    def __init__(
        self,
        client: "anthropic.Anthropic",
        categories: list[CategoryDef],
        cheap_model: str,
        strong_model: str,
        escalate_below: float,
    ):
        self.client = client
        self.categories = categories
        self.category_keys = {c.key for c in categories}
        self.cheap_model = cheap_model
        self.strong_model = strong_model
        self.escalate_below = escalate_below
        self.system_prompt = _build_system_prompt(categories)
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "requests": 0,
            "escalated": 0,
        }

    # ---------- public interface ----------

    def classify(
        self,
        emails: list[EmailForClassification],
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> list[ClassificationResult]:
        total = len(emails)
        done = 0
        by_id: dict[str, EmailForClassification] = {e.message_id: e for e in emails}
        results: dict[str, ClassificationResult] = {}

        batches = [emails[i:i + BATCH_SIZE] for i in range(0, len(emails), BATCH_SIZE)]

        for batch in batches:
            if should_cancel is not None and should_cancel():
                break

            batch_results = self._classify_batch(batch, self.cheap_model)
            for r in batch_results:
                results[r.message_id] = r

            done += len(batch)
            if on_progress is not None:
                on_progress(done, total, f"classified {done}/{total}")

        # Escalation pass: anything below threshold, re-run (batched) on the
        # strong model. The strong result replaces the cheap one.
        to_escalate = [
            by_id[mid] for mid, r in results.items()
            if r.confidence < self.escalate_below and mid in by_id
        ]

        if to_escalate and not (should_cancel is not None and should_cancel()):
            esc_batches = [
                to_escalate[i:i + BATCH_SIZE] for i in range(0, len(to_escalate), BATCH_SIZE)
            ]
            esc_done = 0
            for batch in esc_batches:
                if should_cancel is not None and should_cancel():
                    break
                batch_results = self._classify_batch(batch, self.strong_model)
                for r in batch_results:
                    results[r.message_id] = r
                    self.usage["escalated"] += 1
                esc_done += len(batch)
                if on_progress is not None:
                    on_progress(done, total, f"escalated {esc_done}/{len(to_escalate)}")

        # Preserve original input order.
        return [results[e.message_id] for e in emails if e.message_id in results]

    def estimate_cost_usd(self, n_emails: int) -> float:
        if n_emails <= 0:
            return 0.0

        # Per-email token estimate: 130 input tokens for the email content,
        # plus the 700-token system prompt amortised over a batch of 20
        # (35/email), plus 30 output tokens.
        input_tokens_per_email = INPUT_TOKENS_PER_EMAIL + (SYSTEM_TOKENS_PER_BATCH / BATCH_SIZE)
        output_tokens_per_email = OUTPUT_TOKENS_PER_EMAIL

        total_input_tokens = n_emails * input_tokens_per_email
        total_output_tokens = n_emails * output_tokens_per_email

        cheap_in_price, cheap_out_price = PRICE_PER_MILLION[self.cheap_model]
        cost = (total_input_tokens / 1_000_000) * cheap_in_price
        cost += (total_output_tokens / 1_000_000) * cheap_out_price

        # 15% of emails are assumed to get escalated to the strong model;
        # that fraction is priced again at the strong model's rate.
        escalated_n = n_emails * ESCALATION_FRACTION
        esc_input_tokens = escalated_n * input_tokens_per_email
        esc_output_tokens = escalated_n * output_tokens_per_email

        strong_in_price, strong_out_price = PRICE_PER_MILLION[self.strong_model]
        cost += (esc_input_tokens / 1_000_000) * strong_in_price
        cost += (esc_output_tokens / 1_000_000) * strong_out_price

        return math.ceil(cost * 100) / 100

    # ---------- internals ----------

    def _classify_batch(
        self, batch: list[EmailForClassification], model: str
    ) -> list[ClassificationResult]:
        if not batch:
            return []
        return self._classify_batch_with_retry(batch, model)

    def _classify_batch_with_retry(
        self, batch: list[EmailForClassification], model: str
    ) -> list[ClassificationResult]:
        response = self._call_with_retry(batch, model)

        if response is None:
            # Exhausted retries without a usable response: treat as refused.
            return self._skip_all(batch, "refused")

        if response.stop_reason == "refusal":
            return self._skip_all(batch, "refused")

        if response.stop_reason == "max_tokens":
            if len(batch) <= 1:
                return self._skip_all(batch, "refused")
            mid = len(batch) // 2
            left = self._classify_batch_with_retry(batch[:mid], model)
            right = self._classify_batch_with_retry(batch[mid:], model)
            return left + right

        return self._validate(batch, response, model)

    def _call_with_retry(self, batch: list[EmailForClassification], model: str):
        import json as _json

        user_content = _json.dumps([_email_to_json_dict(e) for e in batch])

        extra = {}
        if model == self.strong_model:
            extra["output_config"] = {"effort": "low"}

        rate_limit_retries = 0
        server_error_retries = 0

        while True:
            try:
                response = self.client.messages.parse(
                    model=model,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": self.system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_content}],
                    output_format=_BatchOutput,
                    **extra,
                )
                self.usage["requests"] += 1
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                    self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
                    self.usage["cache_read_input_tokens"] += (
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    )
                return response
            except anthropic.RateLimitError as e:
                rate_limit_retries += 1
                if rate_limit_retries > 5:
                    return None
                retry_after = 20
                try:
                    retry_after = int(e.response.headers.get("retry-after", "20"))
                except Exception:
                    retry_after = 20
                time.sleep(retry_after)
            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    server_error_retries += 1
                    if server_error_retries > 3:
                        return None
                    time.sleep(2 ** server_error_retries)
                else:
                    raise
            except anthropic.APIConnectionError:
                server_error_retries += 1
                if server_error_retries > 3:
                    return None
                time.sleep(2 ** server_error_retries)

    def _validate(
        self, batch: list[EmailForClassification], response, model: str
    ) -> list[ClassificationResult]:
        parsed = getattr(response, "parsed_output", None)
        batch_ids = {e.message_id for e in batch}

        by_result_id: dict[str, _Item] = {}
        if parsed is not None:
            for item in parsed.results:
                by_result_id[item.id] = item

        out: list[ClassificationResult] = []
        for email in batch:
            item = by_result_id.get(email.message_id)
            if item is None:
                out.append(ClassificationResult(
                    message_id=email.message_id,
                    category="skip",
                    confidence=0.0,
                    reason="",
                    model=model,
                ))
                continue

            if item.category not in self.category_keys:
                out.append(ClassificationResult(
                    message_id=email.message_id,
                    category="skip",
                    confidence=0.0,
                    reason=item.reason,
                    model=model,
                ))
                continue

            out.append(ClassificationResult(
                message_id=email.message_id,
                category=item.category,
                confidence=float(item.confidence),
                reason=item.reason,
                model=model,
            ))

        # Ignore any ids in the response that weren't in the batch.
        _ = batch_ids
        return out

    def _skip_all(
        self, batch: list[EmailForClassification], reason: str
    ) -> list[ClassificationResult]:
        return [
            ClassificationResult(
                message_id=e.message_id,
                category="skip",
                confidence=0.0,
                reason=reason,
                model=self.cheap_model,
            )
            for e in batch
        ]


# ---------- Fake classifier (DEMO mode / tests, no network) ----------

_KEYWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"order|shipped|receipt|invoice", re.I), "receipts"),
    (re.compile(r"statement|payment due|transaction|balance", re.I), "finance"),
    (re.compile(r"flight|hotel|itinerary|reservation", re.I), "travel"),
    (re.compile(r"appointment|doctor|pharmacy|clinic", re.I), "health"),
    (re.compile(r"utility|electric|water bill|mortgage|lease", re.I), "home"),
    (re.compile(r"interview|application|recruit|offer letter", re.I), "work"),
    (re.compile(r"verify|password|sign-in|security alert", re.I), "accounts"),
    (re.compile(r"tax|dmv|toll|jury|court", re.I), "government"),
]

_PERSONAL_LOOKING_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}
_RE_SUBJECT = re.compile(r"^\s*re:", re.I)


class FakeClassifier:
    """Deterministic keyword-rule classifier. Same public interface as
    Classifier, no network calls. Used in DEMO mode (when no API key is
    configured) and in tests.
    """

    def __init__(self, categories: list[CategoryDef], escalate_below: float = 0.6):
        self.categories = categories
        self.category_keys = {c.key for c in categories}
        self.escalate_below = escalate_below
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "requests": 0,
            "escalated": 0,
        }

    def classify(
        self,
        emails: list[EmailForClassification],
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> list[ClassificationResult]:
        total = len(emails)
        out: list[ClassificationResult] = []

        for i in range(0, total, BATCH_SIZE):
            if should_cancel is not None and should_cancel():
                break
            batch = emails[i:i + BATCH_SIZE]
            for email in batch:
                out.append(self._classify_one(email))
            if on_progress is not None:
                on_progress(len(out), total, f"classified {len(out)}/{total}")

        return out

    def _classify_one(self, email: EmailForClassification) -> ClassificationResult:
        haystack = " ".join([email.subject or "", email.snippet or ""])

        for pattern, category in _KEYWORD_RULES:
            if pattern.search(haystack) and category in self.category_keys:
                return ClassificationResult(
                    message_id=email.message_id,
                    category=category,
                    confidence=0.9,
                    reason=f"keyword match for {category}",
                    model="fake",
                )

        domain = (email.sender or "").rsplit("@", 1)[-1].lower()
        if domain in _PERSONAL_LOOKING_DOMAINS and _RE_SUBJECT.search(email.subject or ""):
            if "personal" in self.category_keys:
                return ClassificationResult(
                    message_id=email.message_id,
                    category="personal",
                    confidence=0.9,
                    reason="personal-looking sender, re: subject",
                    model="fake",
                )

        return ClassificationResult(
            message_id=email.message_id,
            category="skip",
            confidence=0.4,
            reason="no keyword match",
            model="fake",
        )

    def estimate_cost_usd(self, n_emails: int) -> float:
        return 0.0
