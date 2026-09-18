"""Browser smoke test: drives the real dashboard end to end in DEMO mode.

Not collected by pytest (the file name does not start with test_). Run it
against a demo server you started yourself:

    DEMO=1 DATA_DIR=/tmp/inbox-cleanup-smoke PORT=8792 .venv/bin/python -m backend.api &
    OUT=/tmp/inbox-cleanup-smoke BASE=http://127.0.0.1:8792 .venv/bin/python tests/browser_smoke.py

Needs `pip install -r requirements-dev.txt` and a Playwright Chromium
(`.venv/bin/playwright install chromium`, or set PW_CHROMIUM to an existing
Chromium binary). Screenshots land in $OUT.
"""
import json, os, sys, time, urllib.request
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8792")
OUT = os.environ.get("OUT", ".")
errors = []

def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method, headers={"content-type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def wait_job(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = api("/api/status")["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.3)
    raise RuntimeError("job did not finish")

def shot(page, name):
    page.screenshot(path=f"{OUT}/{name}.png")

with sync_playwright() as p:
    browser = p.chromium.launch(**({"executable_path": os.environ["PW_CHROMIUM"]} if os.environ.get("PW_CHROMIUM") else {}))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error",) else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    # 1. Overview + scan
    page.goto(BASE + "/")
    page.wait_for_selector("#panel-overview:not([hidden])")
    before = api("/api/overview")
    shot(page, "01-overview")
    page.click('[data-action="scan-start"]')
    page.wait_for_selector("#jobPill:not(.hidden)", timeout=10000)
    shot(page, "02-scanning")
    job = wait_job()
    assert job["state"] == "done", job
    page.wait_for_function("document.getElementById('jobPill').classList.contains('hidden')", timeout=15000)
    after = api("/api/overview")
    assert after["messages_total"] == before["messages_total"], (before, after)
    assert after["unread_total"] == before["unread_total"]
    shot(page, "03-overview-after-scan")
    print("scan ok:", after["messages_total"], "messages,", after["senders_total"], "senders")

    # 2. Subscriptions: stop two senders
    page.click("#tabbtn-subscriptions")
    page.wait_for_selector("#panel-subscriptions tbody tr")
    boxes = page.locator("#panel-subscriptions tbody input[type=checkbox]")
    n = boxes.count(); assert n >= 3, n
    first_two = []
    for i in range(2):
        boxes.nth(i).check()
    rows = page.locator("#panel-subscriptions tbody tr")
    for i in range(2):
        first_two.append(rows.nth(i).inner_text().split("\n"))
    page.click('[data-action="bulk-stop"]')
    page.wait_for_selector("#modalRoot >> text=Confirm action")
    shot(page, "04-stop-preview")
    page.get_by_role("button", name="Confirm").click()
    job = wait_job(); assert job["state"] == "done", job
    blocked = api("/api/senders?status=blocked&subscription_only=false&min_total=1")
    assert blocked["total"] == 2, blocked["total"]
    print("stop ok:", [s["sender"] for s in blocked["items"]])
    page.wait_for_timeout(1500)
    shot(page, "05-after-stop")

    # 3. Trash older than 30 days for one sender
    page.wait_for_selector("#panel-subscriptions tbody tr")
    page.locator("#panel-subscriptions tbody input[type=checkbox]").nth(0).check()
    page.click('[data-action="bulk-trash"]')
    page.wait_for_selector("#modalRoot >> text=Confirm action")
    shot(page, "06-trash-preview")
    modal_text = page.locator("#modalRoot").inner_text()
    page.get_by_role("button", name="Confirm").click()
    job = wait_job(); assert job["state"] == "done", job
    ov = api("/api/overview")
    assert ov["actions"]["trashed_messages"] > 0, ov["actions"]
    purge_rows = [x for x in api("/api/actions/log?limit=50")["items"] if x["action"] == "purge"]
    assert len(purge_rows) == 1, f"purge touched {len(purge_rows)} senders, expected 1 (stale selection?)"
    assert "1 sender" in modal_text or "1 sender(s)" in modal_text, modal_text[:200]
    print("purge ok: trashed", ov["actions"]["trashed_messages"], "from one sender")

    # 4. Activity: undo the purge (first undoable row)
    page.click("#tabbtn-activity")
    page.wait_for_selector("#panel-activity tbody tr")
    shot(page, "07-activity")
    log = api("/api/actions/log?limit=50")
    purge = next(e for e in log["items"] if e["action"] == "purge" and e["undoable"] and not e["undone"])
    page.locator(f'[data-action="undo-action"][data-log-id="{purge["id"]}"], [data-action="undo-action"][data-id="{purge["id"]}"]').first.click()
    page.wait_for_timeout(1500)
    log2 = api("/api/actions/log?limit=50")
    assert next(e for e in log2["items"] if e["id"] == purge["id"])["undone"] is True
    ov2 = api("/api/overview")
    assert ov2["actions"]["trashed_messages"] == ov["actions"]["trashed_messages"] - purge["message_count"], (ov2["actions"], purge["message_count"])
    assert ov2["actions"]["trashed_messages"] == 0, ov2["actions"]
    print("undo ok")
    shot(page, "08-activity-undone")

    # 5. Organize: estimate -> classify -> override -> apply
    page.click("#tabbtn-organize")
    page.wait_for_selector("#panel-organize:not([hidden])")
    page.wait_for_timeout(1000)
    shot(page, "09-organize")
    est = api("/api/organize/estimate?scope=inbox&days=365")
    assert est["candidates"] > 0, est
    page.click('[data-action="organize-classify"]')
    job = wait_job(120); assert job["state"] == "done", job
    page.wait_for_timeout(1500)
    res = api("/api/organize/results?limit=5")
    assert res["total"] > 0 and res["items"], res["total"]
    shot(page, "10-organize-results")
    sel = page.locator("#panel-organize tbody select").first
    sel.wait_for()
    target = "travel" if res["items"][0]["category"] != "travel" else "finance"
    sel.select_option(target)
    page.wait_for_timeout(800)
    mid = res["items"][0]["message_id"]
    got = api(f"/api/organize/results?limit=200")
    row = next((i for i in got["items"] if i["message_id"] == mid), None)
    assert row and row["category"] == target and row["overridden"], row
    print("override ok:", mid, "->", target)
    page.click('[data-action="organize-apply-open"]')
    page.wait_for_selector("#modalRoot >> text=Apply labels")
    shot(page, "11-apply-modal")
    page.locator("#modalRoot").get_by_role("button", name="Apply", exact=True).click()
    job = wait_job(120); assert job["state"] == "done", job
    log3 = api("/api/actions/log?limit=50")
    applied = [e for e in log3["items"] if e["action"] == "apply_labels"]
    assert applied, "no apply_labels log rows"
    print("apply ok:", sum(e["message_count"] for e in applied), "labeled across", len(applied), "categories")
    page.wait_for_timeout(1000)
    shot(page, "12-organize-applied")

    # 6. Settings
    page.click("#tabbtn-settings")
    page.wait_for_selector("#panel-settings:not([hidden])")
    shot(page, "13-settings")

    # 7. Mobile viewport check
    m = browser.new_context(viewport={"width": 375, "height": 740}).new_page()
    m.goto(BASE + "/")
    m.wait_for_selector("#panel-overview:not([hidden])")
    m.screenshot(path=f"{OUT}/m-01-overview.png")
    m.click("#tabbtn-subscriptions")
    m.wait_for_selector("#panel-subscriptions tbody tr")
    m.locator("#panel-subscriptions tbody input[type=checkbox]").nth(0).check()
    m.wait_for_timeout(500)
    m.screenshot(path=f"{OUT}/m-02-subscriptions.png")
    sw, iw = m.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert sw <= iw, f"horizontal page scroll at 375px: {sw} > {iw}"
    print("mobile ok: no horizontal scroll")

    # 8. Dark mode
    d = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark").new_page()
    d.goto(BASE + "/")
    d.wait_for_selector("#panel-overview:not([hidden])")
    d.wait_for_timeout(800)
    d.screenshot(path=f"{OUT}/d-01-overview.png")
    d.click("#tabbtn-subscriptions")
    d.wait_for_selector("#panel-subscriptions tbody tr")
    d.screenshot(path=f"{OUT}/d-02-subscriptions.png")

    browser.close()

print("console/page errors:", [e for e in errors if "favicon" not in e] or "none")
