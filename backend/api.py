"""FastAPI app + routes. `python -m backend.api` starts it.

See docs/ARCHITECTURE.md "HTTP API" for the full route table.
"""
from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import actions as actions_mod
from backend import scanner
from backend.config import settings as config
from backend.demo import seed_demo
from backend.gmail_client import DemoGmailClient, GmailClient
from backend.jobs import JobBusy, JobRunner
from backend.models import (
    ActionPreview,
    ActionRequest,
    ApplyRequest,
    CategoryDef,
    ClassifyRequest,
    LastScan,
    OverviewResponse,
    ScanRequest,
    SenderFilter,
    SenderListResponse,
)
from backend.models import Settings as SettingsModel
from backend.models import StatusResponse
from backend.store import Store

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

store = Store(config.db_path)
gmail = DemoGmailClient(store) if config.demo else GmailClient(config)
job_runner = JobRunner()

if config.demo:
    seed_demo(store)

app = FastAPI(title="Gmail Cleanup Dashboard")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


def _start_job(kind: str, fn) -> str:
    try:
        return job_runner.start(kind, fn)
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ------------------------------------------------------------------ organize

_organizer = None


def make_classifier(categories):
    if config.demo or not config.anthropic_api_key:
        from backend.classifier import FakeClassifier

        return FakeClassifier(categories)
    import anthropic

    from backend.classifier import Classifier

    return Classifier(
        client=anthropic.Anthropic(),
        categories=categories,
        cheap_model=config.classify_model,
        strong_model=config.escalate_model,
        escalate_below=config.escalate_below,
    )


def get_organizer():
    global _organizer
    if _organizer is not None:
        return _organizer
    try:
        from backend.organizer import Organizer
    except ImportError:
        return None
    _organizer = Organizer(store=store, gmail=gmail, config=config, classifier_factory=make_classifier)
    return _organizer


def _organizer_or_503():
    org = get_organizer()
    if org is None:
        raise HTTPException(status_code=503, detail="organizer not available")
    return org


# --------------------------------------------------------------------- health

@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------- auth

@app.post("/api/auth/login")
def auth_login():
    try:
        email = gmail.login()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "email": email}


@app.post("/api/auth/logout")
def auth_logout():
    gmail.logout()
    return {"ok": True}


# -------------------------------------------------------------------- status

def _current_settings() -> SettingsModel:
    data = store.get_setting("settings")
    if data:
        return SettingsModel(**data)
    return SettingsModel(
        classify_model=config.classify_model,
        escalate_model=config.escalate_model,
        escalate_below=config.escalate_below,
    )


@app.get("/api/status", response_model=StatusResponse)
def get_status():
    authenticated = gmail.is_authenticated()
    return StatusResponse(
        mode=config.mode,
        authenticated=authenticated,
        email=gmail.email() if authenticated else None,
        job=job_runner.current(),
        counts=store.counts(),
        last_scan=LastScan(**(store.get_scan_state("last_scan") or {})),
        settings=_current_settings(),
        anthropic_configured=bool(config.anthropic_api_key),
    )


@app.get("/api/overview", response_model=OverviewResponse)
def get_overview():
    return store.overview()


# -------------------------------------------------------------------- scan

@app.post("/api/scan")
def post_scan(req: ScanRequest):
    def fn(job):
        return scanner.run_scan(job, store, gmail, req.window_months, req.full)

    job_id = _start_job("scan", fn)
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/cancel")
def post_jobs_cancel():
    job_runner.cancel()
    return {"ok": True}


# ------------------------------------------------------------------ senders

@app.get("/api/senders", response_model=SenderListResponse)
def get_senders(request: Request):
    qp: dict = dict(request.query_params)
    if "categories" in qp:
        qp["categories"] = [c for c in qp["categories"].split(",") if c]
    if "status" in qp:
        qp["status"] = [c for c in qp["status"].split(",") if c]
    try:
        flt = SenderFilter(**qp)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    total, items = store.sender_stats(flt)
    return SenderListResponse(total=total, items=items)


@app.get("/api/senders/{sender}/messages")
def get_sender_messages(sender: str, limit: int = Query(50, ge=1, le=1000)):
    return {"items": store.sender_messages(sender, limit=limit)}


# ------------------------------------------------------------------ actions

@app.post("/api/actions/preview", response_model=ActionPreview)
def post_actions_preview(req: ActionRequest):
    return actions_mod.preview(store, req)


@app.post("/api/actions/execute")
def post_actions_execute(req: ActionRequest):
    def fn(job):
        return actions_mod.run_execute(job, store, gmail, req, demo=config.demo)

    job_id = _start_job("execute_actions", fn)
    return {"ok": True, "job_id": job_id}


@app.get("/api/actions/log")
def get_actions_log(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    total, items = store.action_log(limit, offset)
    return {"total": total, "items": items}


@app.post("/api/actions/undo/{log_id}")
def post_actions_undo(log_id: int):
    try:
        actions_mod.undo(store, gmail, log_id, demo=config.demo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


# ----------------------------------------------------------------- organize

@app.get("/api/organize/categories")
def get_organize_categories():
    org = _organizer_or_503()
    return {"items": org.get_categories()}


@app.put("/api/organize/categories")
def put_organize_categories(payload: dict):
    org = _organizer_or_503()
    try:
        items = [CategoryDef(**item) for item in payload.get("items", [])]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    org.set_categories(items)
    return {"items": org.get_categories()}


@app.get("/api/organize/estimate")
def get_organize_estimate(scope: str = "inbox", days: int = 365):
    org = _organizer_or_503()
    return org.estimate(scope, days)


@app.post("/api/organize/classify")
def post_organize_classify(req: ClassifyRequest):
    org = _organizer_or_503()

    def fn(job):
        return org.run_classify(job, req)

    job_id = _start_job("classify", fn)
    return {"ok": True, "job_id": job_id}


@app.get("/api/organize/results")
def get_organize_results(
    category: str | None = None,
    min_confidence: float | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    org = _organizer_or_503()
    return org.results(category, min_confidence, limit, offset)


@app.put("/api/organize/results/{message_id}")
def put_organize_result(message_id: str, payload: dict):
    org = _organizer_or_503()
    org.override(message_id, payload.get("category"))
    return {"ok": True}


@app.post("/api/organize/apply")
def post_organize_apply(req: ApplyRequest):
    org = _organizer_or_503()

    def fn(job):
        return org.run_apply(job, req)

    job_id = _start_job("apply_labels", fn)
    return {"ok": True, "job_id": job_id}


# -------------------------------------------------------------------- labels

@app.get("/api/labels")
def get_labels():
    return {"items": gmail.list_labels()}


# ------------------------------------------------------------------ settings

@app.get("/api/settings", response_model=SettingsModel)
def get_settings():
    return _current_settings()


@app.put("/api/settings", response_model=SettingsModel)
def put_settings(payload: dict):
    current = _current_settings().model_dump()
    current.update(payload)
    try:
        merged = SettingsModel(**current)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    store.set_setting("settings", merged.model_dump())
    return merged


# ------------------------------------------------------------------ frontend

@app.get("/")
def index():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(
            status_code=404,
            detail="frontend/index.html not found - the frontend has not been built yet",
        )
    return FileResponse(str(index_path))


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=config.port)
