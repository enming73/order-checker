import asyncio
import json
import secrets
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, UploadFile, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from .config import Settings, ROOT
from .db import Database, dumps, now
from .domain import LABELS
from .service import Service, Conflict

STATUS = {"uploaded": "Eingegangen", "queued": "Workflow angefordert", "processing": "Vorprüfung läuft",
          "ready": "Bereit zur Prüfung", "manual": "Manuelle Prüfung erforderlich", "reviewed": "Menschlich geprüft"}


class Callback(BaseModel):
    case_id: str
    run_id: str


class Failure(Callback):
    step: Literal["parse", "extract", "validate", "transport"] = "transport"


class ERPResult(BaseModel):
    event_id: str
    case_id: str
    status: Literal["Rückfrage erforderlich", "Vorprüfung abgeschlossen"]
    reviewed_at: str
    reviewer: str
    draft: str
    result: dict


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings.data_dir)
    service = Service(db, settings)
    app = FastAPI(title="Technische Auftragsprüfung")
    app.state.db, app.state.service, app.state.settings = db, service, settings
    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "app/templates")
    templates.env.globals.update(labels=LABELS, statuses=STATUS,
        step_labels={"trigger": "Workflow starten", "parse": "Unterlagen lesen", "extract": "Felder extrahieren", "validate": "Regeln und ERP abgleichen", "transport": "Workflow-Verbindung"},
        result_labels={"ok": "Erfolgreich", "manual": "Manuelle Prüfung", "failed": "Fehlgeschlagen", "superseded": "Durch neuen Lauf ersetzt"})

    def internal(request: Request):
        if not secrets.compare_digest(request.headers.get("X-Internal-Token", ""), settings.internal_token):
            raise HTTPException(401, "Interner Zugriff nicht autorisiert.")

    @app.middleware("http")
    async def origin_guard(request, call_next):
        if request.method == "POST" and not request.url.path.startswith(("/internal/", "/mock-erp/")):
            origin = request.headers.get("origin")
            if (origin and urlsplit(origin).netloc != request.headers.get("host")) or request.headers.get("sec-fetch-site") == "cross-site":
                return Response("Fremder Ursprung nicht erlaubt.", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def render(request, template, **context):
        return templates.TemplateResponse(request=request, name=template,
            context={"mode": settings.llm_mode, **context})

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        response = render(request, "error.html", message="Auftrag nicht gefunden.", code=404)
        response.status_code = 404
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        response = render(request, "error.html", message=str(exc), code=409)
        response.status_code = 409
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        with db.connect() as conn:
            cases = [dict(r) for r in conn.execute("SELECT id,created,status,fields,issues FROM cases ORDER BY created DESC, rowid DESC")]
            faults = dict(conn.execute("SELECT name, mode FROM faults").fetchall())
        for case in cases:
            fields = json.loads(case["fields"])
            case["number"] = next((c["value"] for c in fields.get("order_number", []) if c["verified"]), "Noch nicht ermittelt")
            case["count"] = len(json.loads(case["issues"]))
        return render(request, "index.html", cases=cases, faults=faults)

    @app.get("/samples/{letter}")
    def sample(letter: Literal["A", "B", "C"]):
        return FileResponse(ROOT / "samples" / f"Auftrag-{letter}.eml", filename=f"Auftrag-{letter}.eml", media_type="message/rfc822")

    @app.post("/upload")
    async def upload(file: UploadFile):
        if not file.filename or not file.filename.lower().endswith(".eml"):
            raise Conflict("Bitte eine .eml-Datei auswählen.")
        raw = await file.read(settings.max_upload + 1)
        await file.close()
        if len(raw) > settings.max_upload:
            raise Conflict("E-Mail überschreitet 12 MiB. Bitte Unterlagen manuell prüfen.")
        if not raw:
            raise Conflict("Die E-Mail-Datei ist leer.")
        case_id, created = await run_in_threadpool(service.upload, raw, file.filename)
        if created:
            await run_in_threadpool(service.trigger, case_id)
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    def detail(request: Request, case_id: str):
        case = db.case(case_id)
        with db.connect() as conn:
            review = conn.execute("SELECT * FROM reviews WHERE case_id=?", (case_id,)).fetchone()
            steps = [dict(r) for r in conn.execute("SELECT * FROM steps WHERE case_id=? ORDER BY id", (case_id,))]
            attachments = [dict(r) for r in conn.execute("SELECT id,filename FROM attachments WHERE case_id=?", (case_id,))]
            erp = conn.execute("SELECT event_id FROM erp_results WHERE case_id=?", (case_id,)).fetchone()
        return render(request, "detail.html", case=case, sources=db.sources(case_id), review=review,
                      steps=steps, attachments=attachments, erp=erp,
                      conflict_fields={i["field"] for i in case["issues"] if i["code"] == "conflict"},
                      field_order=sorted(LABELS, key=lambda key: not any(i["code"] == "conflict" and i["field"] == key for i in case["issues"])))

    @app.get("/api/cases/{case_id}")
    def case_status(case_id: str):
        case = db.case(case_id)
        with db.connect() as conn:
            review = conn.execute("SELECT id,decision,synced,sync_error FROM reviews WHERE case_id=?", (case_id,)).fetchone()
        return {key: case[key] for key in ("id", "status", "stage", "failure_step", "failure_reason", "fields", "issues", "draft")} | {"review": dict(review) if review else None}

    @app.get("/cases/{case_id}/original")
    def original(case_id: str):
        return Response(db.case(case_id)["raw"], media_type="message/rfc822",
                        headers={"Content-Disposition": 'attachment; filename="Original.eml"'})

    @app.get("/attachments/{attachment_id}")
    def attachment(attachment_id: str):
        with db.connect() as conn:
            row = conn.execute("SELECT raw FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return Response(row["raw"], media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="Anhang.pdf"'})

    @app.post("/cases/{case_id}/retry")
    def retry(case_id: str):
        service.trigger(case_id)
        return RedirectResponse(f"/cases/{case_id}", 303)

    @app.post("/cases/{case_id}/review")
    def review(case_id: str, decision: str = Form(), draft: str = Form("")):
        if len(draft) > 12000:
            raise Conflict("Der Entwurf ist zu lang (maximal 12.000 Zeichen).")
        service.review(case_id, decision, draft)
        service.sync(case_id)
        return RedirectResponse(f"/cases/{case_id}", 303)

    @app.post("/cases/{case_id}/sync")
    def sync(case_id: str):
        service.sync(case_id)
        return RedirectResponse(f"/cases/{case_id}", 303)

    @app.post("/internal/steps/{step}", dependencies=[Depends(internal)])
    def step(step: str, callback: Callback):
        return service.step(callback.case_id, callback.run_id, step)

    @app.post("/internal/failure", dependencies=[Depends(internal)])
    def failure(callback: Failure):
        service.fail(callback.case_id, callback.run_id, callback.step, "Workflow-Verbindung unterbrochen. Bitte erneut auslösen.")
        return {"case_id": callback.case_id, "result": "failure_recorded"}

    @app.get("/mock-erp/drawings/{number}", dependencies=[Depends(internal)])
    async def drawing(number: str):
        with db.connect() as conn:
            fault = conn.execute("SELECT mode FROM faults WHERE name='read'").fetchone()[0]
            row = conn.execute("SELECT revision FROM erp_drawings WHERE number=?", (number,)).fetchone()
        if fault == "timeout":
            await asyncio.sleep(settings.erp_timeout + 1)
        if not row:
            raise HTTPException(404, "Zeichnung nicht registriert.")
        return {"number": number, "revision": row["revision"]}

    @app.post("/mock-erp/results", dependencies=[Depends(internal)])
    async def erp_write(payload: ERPResult):
        with db.connect() as conn:
            fault = conn.execute("SELECT mode FROM faults WHERE name='write'").fetchone()[0]
        if fault == "timeout":
            await asyncio.sleep(settings.erp_timeout + 1)
        with db.connect(write=True) as conn:
            existing = conn.execute("SELECT payload FROM erp_results WHERE event_id=?", (payload.event_id,)).fetchone()
            serialized = dumps(payload.model_dump())
            if existing and existing["payload"] != serialized:
                raise HTTPException(409, "Ereignis-ID bereits mit anderem Inhalt verwendet.")
            if not existing:
                other = conn.execute("SELECT 1 FROM erp_results WHERE case_id=?", (payload.case_id,)).fetchone()
                if other:
                    raise HTTPException(409, "Auftrag bereits mit anderem Ereignis synchronisiert.")
                conn.execute("INSERT INTO erp_results VALUES (?,?,?,?)", (payload.event_id, payload.case_id, serialized, now()))
        if fault == "after_commit_timeout":
            await asyncio.sleep(settings.erp_timeout + 1)
        return {"event_id": payload.event_id, "saved": True}

    @app.get("/mock-erp/results", dependencies=[Depends(internal)])
    def results():
        with db.connect() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM erp_results")]

    @app.post("/demo/faults")
    def fault(name: Literal["read", "write"] = Form(), mode: Literal["normal", "timeout", "after_commit_timeout"] = Form()):
        if settings.llm_mode != "demo":
            raise Conflict("Fehlersimulation ist nur im Demo-Modus verfügbar.")
        with db.connect(write=True) as conn:
            conn.execute("UPDATE faults SET mode=? WHERE name=?", (mode, name))
        return RedirectResponse("/", 303)

    return app

app = create_app()
