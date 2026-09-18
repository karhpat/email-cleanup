"""Synthetic mailbox for DEMO=1. Fictional brands/people and domains only.

Idempotent: seed_demo() does nothing to `messages` if any already exist, but
always makes sure the `categories` table has the contract's defaults.
"""
from __future__ import annotations

import random
import time

from backend.store import Store

DEFAULT_CATEGORIES = [
    ("finance", "Finance", "bank and card statements, transaction alerts, investments, taxes, insurance"),
    ("receipts", "Receipts & Orders", "purchase confirmations, shipping, returns, invoices"),
    ("travel", "Travel", "flights, hotels, rentals, itineraries, loyalty programs"),
    ("health", "Health", "doctors, appointments, pharmacy, insurance claims, fitness"),
    ("home", "Home", "utilities, mortgage or rent, home services, HOA, real estate"),
    ("work", "Work & Career", "job applications, recruiters, professional correspondence"),
    ("personal", "Personal", "real people writing to you: friends, family, acquaintances"),
    ("government", "Government & Legal", "taxes, DMV, tolls, legal notices, voting"),
    ("accounts", "Accounts & Security", "password resets, sign-in alerts, account notices, subscriptions billing"),
    ("skip", None, "marketing or newsletters that slipped through, or nothing worth labeling"),
]

_ADJ = [
    "Loom", "Verdant", "Pixel", "Crest", "Nimbus", "Amber", "Frost", "Cedar", "Marble", "Coral",
    "Quartz", "Willow", "Ember", "Slate", "Harbor", "Meadow", "Tidal", "Lantern", "Birch", "Copper",
]
_NOUN = [
    "light", "market", "aisle", "wear", "cloud", "goods", "supply", "studio", "trail", "kitchen",
    "provisions", "outfitters", "labs", "collective", "post", "works", "society", "depot", "atelier", "co",
]
_TLDS = ["com", "io", "co", "shop", "net"]

_FIRST = [
    "Maya", "Jordan", "Priya", "Noah", "Elena", "Sam", "Diego", "Ava", "Leo", "Mira",
    "Theo", "Nina", "Owen", "Sasha", "Kavi", "Ruth", "Marco", "Alina", "Ben", "Zoe",
    "Ravi", "Iris", "Felix", "Dana", "Yusuf", "Clara", "Amir",
]
_LAST = [
    "Chen", "Ellis", "Novak", "Reyes", "Patel", "Brandt", "Kovac", "Adler", "Okafor", "Lindqvist",
    "Moreau", "Suzuki", "Haddad", "Petrov", "Nakamura", "Silva", "Fischer", "Doyle", "Hassan", "Larsen",
]
_PERSON_DOMAINS = ["windmail.net", "driftpost.com", "clearvane.io", "haventext.com", "brightloop.net"]

_FORUM_NAMES = [
    "PixelBoard Forums", "Hobbyist Circle", "TrailNet Community", "OpenBench Makers",
    "GardenPatch Forum", "CircuitScrap Community", "InkWell Writers Group", "TrailNet Social",
    "Basecamp Riders", "Weekend Coders",
]

_PROMO_SUBJECTS = [
    "Flash sale ends tonight", "{b}: 20% off just for you", "New arrivals you'll love",
    "Your cart is waiting", "Last chance: free shipping today", "This weekend only",
    "Members get early access", "Back in stock: your favorites", "The sale you've been waiting for",
    "Today only: extra 15% off", "Meet the new collection", "Deals inside — don't miss out",
]
_UPDATE_SUBJECTS = [
    "Your order has shipped", "Order confirmation #{n}", "Your package is out for delivery",
    "Your statement is ready", "Sign-in alert from a new device", "Payment received",
    "Appointment reminder", "Your delivery is arriving today", "Receipt for your recent purchase",
    "Action needed: verify your account", "Your monthly summary is ready", "Return processed",
]
_PRIMARY_SUBJECTS = [
    "Re: Saturday plans", "Quick question", "Dinner next week?", "Following up", "Photos from the trip",
    "Re: your email", "Can you take a look?", "Long time no talk", "Re: schedule for next week",
    "Thank you!", "Happy birthday!", "Are you free Friday?",
]
_SOCIAL_SUBJECTS = [
    "You have 3 new notifications", "New reply to your post", "Weekly digest", "Someone mentioned you",
    "Your post is getting attention", "New members joined this week", "Trending in your group",
    "Reminder: event this weekend",
]

_SNIPPETS = [
    "Hi there, just wanted to follow up on this...",
    "Thanks for being a valued customer! Here's what's new...",
    "Your recent activity summary is ready to view.",
    "We noticed you left something in your cart.",
    "Here's a quick update on your recent order.",
    "Don't miss out — offer ends soon.",
    "See what everyone's talking about this week.",
    "A quick note before the weekend.",
]


def _seed_categories(store: Store) -> None:
    with store.lock:
        existing = store.conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
        if existing:
            return
        store.conn.executemany(
            "INSERT INTO categories (key, label_name, description, sort_order) VALUES (?, ?, ?, ?)",
            [(k, label, desc, i) for i, (k, label, desc) in enumerate(DEFAULT_CATEGORIES)],
        )
        store.conn.commit()


def _brand_names(rng: random.Random, n: int) -> list[str]:
    combos = [(a, b) for a in _ADJ for b in _NOUN]
    rng.shuffle(combos)
    return [f"{a}{b}" for a, b in combos[:n]]


def _person_names(rng: random.Random, n: int) -> list[tuple[str, str]]:
    combos = [(f, l) for f in _FIRST for l in _LAST]
    rng.shuffle(combos)
    return combos[:n]


def _unsub_headers(rng: random.Random, domain: str, kind: str) -> tuple[str | None, str | None]:
    if kind == "one_click":
        token = rng.randrange(10**8, 10**9)
        return f"<https://{domain}/unsubscribe?u={token}>", "List-Unsubscribe=One-Click"
    if kind == "mailto":
        return f"<mailto:unsubscribe@{domain}?subject=unsubscribe>", None
    if kind == "http":
        return f"<https://{domain}/preferences>", None
    return None, None


def _pick_kind(rng: random.Random, weights: dict[str, float]) -> str:
    kinds = list(weights.keys())
    return rng.choices(kinds, weights=[weights[k] for k in kinds], k=1)[0]


def seed_demo(store: Store, seed: int = 7) -> None:
    _seed_categories(store)
    if store.counts()["messages"] > 0:
        return

    rng = random.Random(seed)
    now = int(time.time())
    year_ago = now - 365 * 86400

    n_promo, n_update, n_primary, n_social = 99, 45, 27, 9
    brand_pool = _brand_names(rng, n_promo + n_update)
    promo_brands = brand_pool[:n_promo]
    update_brands = brand_pool[n_promo : n_promo + n_update]
    people = _person_names(rng, n_primary)
    forum_names = list(_FORUM_NAMES)
    while len(forum_names) < n_social:
        forum_names.append(f"Circle {len(forum_names)}")
    forum_names = forum_names[:n_social]

    senders: list[dict] = []

    for brand in promo_brands:
        domain = f"{brand.lower()}.{rng.choice(_TLDS)}"
        kind = _pick_kind(rng, {"one_click": 0.5, "mailto": 0.25, "http": 0.15, "none": 0.10})
        heavy = rng.random() < 0.12
        senders.append(
            {
                "email": f"news@{domain}",
                "name": brand,
                "domain": domain,
                "category": "promotions",
                "open_rate": rng.uniform(0.0, 0.05),
                "unsub_kind": kind,
                "count": rng.randint(150, 300) if heavy else rng.randint(10, 55),
                "star_rate": 0.003,
                "important_rate": 0.0,
                "archive_rate": 0.35,
                "subjects": _PROMO_SUBJECTS,
            }
        )

    for brand in update_brands:
        domain = f"{brand.lower()}.{rng.choice(_TLDS)}"
        has_unsub = rng.random() < 0.4
        kind = _pick_kind(rng, {"one_click": 0.5, "mailto": 0.3, "http": 0.2}) if has_unsub else "none"
        senders.append(
            {
                "email": f"noreply@{domain}",
                "name": brand,
                "domain": domain,
                "category": "updates",
                "open_rate": rng.uniform(0.15, 0.75),
                "unsub_kind": kind,
                "count": rng.randint(8, 45),
                "star_rate": 0.01,
                "important_rate": 0.08,
                "archive_rate": 0.15,
                "subjects": _UPDATE_SUBJECTS,
            }
        )

    for first, last in people:
        domain = rng.choice(_PERSON_DOMAINS)
        senders.append(
            {
                "email": f"{first.lower()}.{last.lower()}@{domain}",
                "name": f"{first} {last}",
                "domain": domain,
                "category": "primary",
                "open_rate": rng.uniform(0.6, 0.97),
                "unsub_kind": "none",
                "count": rng.randint(2, 28),
                "star_rate": 0.06,
                "important_rate": 0.2,
                "archive_rate": 0.05,
                "subjects": _PRIMARY_SUBJECTS,
            }
        )

    for i, name in enumerate(forum_names):
        domain = f"{name.lower().replace(' ', '')}.net"
        gmail_cat = "forums" if i % 2 == 0 else "social"
        has_unsub = rng.random() < 0.5
        kind = _pick_kind(rng, {"one_click": 0.6, "mailto": 0.4}) if has_unsub else "none"
        senders.append(
            {
                "email": f"notify@{domain}",
                "name": name,
                "domain": domain,
                "category": gmail_cat,
                "open_rate": rng.uniform(0.1, 0.4),
                "unsub_kind": kind,
                "count": rng.randint(4, 35),
                "star_rate": 0.01,
                "important_rate": 0.0,
                "archive_rate": 0.2,
                "subjects": _SOCIAL_SUBJECTS,
            }
        )

    rows: list[dict] = []
    msg_n = 0
    for s in senders:
        list_id = f"<{s['domain'].replace('.', '-')}.list-id.{s['domain']}>" if s["unsub_kind"] != "none" else None
        list_unsub, list_unsub_post = _unsub_headers(rng, s["domain"], s["unsub_kind"])
        for _ in range(s["count"]):
            msg_n += 1
            internal_ts = rng.randint(year_ago, now)
            is_unread = 0 if rng.random() < s["open_rate"] else 1
            is_starred = 1 if rng.random() < s["star_rate"] else 0
            is_important = 1 if rng.random() < s["important_rate"] else 0
            in_inbox = 0 if rng.random() < s["archive_rate"] else 1
            subject_tpl = rng.choice(s["subjects"])
            subject = subject_tpl.format(b=s["name"], n=rng.randint(1000, 99999))
            labels = ["INBOX"] if in_inbox else []
            if is_unread:
                labels.append("UNREAD")
            if is_starred:
                labels.append("STARRED")
            if is_important:
                labels.append("IMPORTANT")
            cat_label = {
                "promotions": "CATEGORY_PROMOTIONS",
                "updates": "CATEGORY_UPDATES",
                "social": "CATEGORY_SOCIAL",
                "forums": "CATEGORY_FORUMS",
                "primary": "CATEGORY_PERSONAL",
            }.get(s["category"])
            if cat_label:
                labels.append(cat_label)

            rows.append(
                {
                    "id": f"demo-msg-{msg_n}",
                    "thread_id": f"demo-thread-{msg_n}",
                    "sender": s["email"],
                    "sender_name": s["name"],
                    "sender_raw": f"{s['name']} <{s['email']}>",
                    "domain": s["domain"],
                    "subject": subject,
                    "snippet": rng.choice(_SNIPPETS),
                    "internal_ts": internal_ts,
                    "is_unread": is_unread,
                    "is_starred": is_starred,
                    "is_important": is_important,
                    "in_inbox": in_inbox,
                    "in_trash": 0,
                    "labels": labels,
                    "category": s["category"],
                    "list_id": list_id,
                    "list_unsubscribe": list_unsub,
                    "list_unsubscribe_post": list_unsub_post,
                    "size": rng.randint(2000, 60000),
                    "fetched_at": now,
                    "gone": 0,
                }
            )

    for i in range(0, len(rows), 500):
        store.upsert_messages(rows[i : i + 500])
