import hashlib
import json
import time
from uuid import uuid4
from urllib.parse import quote
import httpx
from pydantic import ValidationError
from .db import dumps, now
from .domain import Extraction, validate_evidence, check_rules, make_draft, issue
from .model import extract
from .parsing import ManualCheck, parse_email

STAGES = {"parse": ("", "parsed"), "extract": ("parsed", "extracted"), "validate": ("extracted", "validated")}


class Conflict(Exception):
    pass


class Service:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def upload(self, raw, filename):
        sha = hashlib.sha256(raw).hexdigest()
        case_id = str(uuid4())
        with self.db.connect(write=True) as db:
            existing = db.execute("SELECT id FROM cases WHERE sha=?", (sha,)).fetchone()
            if existing:
                return existing["id"], False
            db.execute("INSERT INTO cases(id, sha, filename, raw, created) VALUES (?,?,?,?,?)",
                       (case_id, sha, filename, raw, now()))
        return case_id, True

    def new_run(self, case_id):
        run_id = str(uuid4())
        with self.db.connect(write=True) as db:
            case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not case:
                raise KeyError(case_id)
            if db.execute("SELECT 1 FROM reviews WHERE case_id=?", (case_id,)).fetchone():
                raise Conflict("Bereits geprüft; nur ERP-Synchronisierung ist noch möglich.")
            # Explicit retry revokes the previous run. Its callbacks can no longer commit.
            db.execute("UPDATE cases SET run_id=?, stage='', busy=NULL, status='queued', failure_step=NULL, failure_reason=NULL WHERE id=?", (run_id, case_id))
        return run_id

    def trigger(self, case_id):
        run_id = self.new_run(case_id)
        start = time.monotonic()
        try:
            response = httpx.post(self.settings.workflow_url, json={"case_id": case_id, "run_id": run_id}, timeout=5)
            response.raise_for_status()
        except httpx.HTTPError:
            self.fail(case_id, run_id, "trigger", "Workflow konnte nicht gestartet werden. Bitte erneut auslösen.")
            self.record_trigger(case_id, run_id, start, "failed")
        else:
            self.record_trigger(case_id, run_id, start, "ok")
        return run_id

    def record_trigger(self, case_id, run_id, start, result):
        with self.db.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO steps(case_id,run_id,step,started,duration_ms,result) VALUES (?,?,?,?,?,?)",
                       (case_id, run_id, "trigger", now(), int((time.monotonic()-start)*1000), result))

    def fail(self, case_id, run_id, step, reason):
        with self.db.connect(write=True) as db:
            db.execute("UPDATE cases SET status='manual', busy=NULL, failure_step=?, failure_reason=? WHERE id=? AND run_id=? AND status NOT IN ('reviewed','ready','manual') AND NOT EXISTS (SELECT 1 FROM reviews WHERE case_id=?)",
                       (step, reason, case_id, run_id, case_id))

    def step(self, case_id, run_id, step):
        if step not in STAGES:
            raise Conflict("Unbekannter Verarbeitungsschritt.")
        before, after = STAGES[step]
        with self.db.connect(write=True) as db:
            case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not case:
                raise KeyError(case_id)
            base = {"case_id": case_id, "run_id": run_id, "step": step}
            if case["run_id"] != run_id or case["status"] in ("manual", "reviewed", "ready"):
                return {**base, "ok": False, "result": "ignored"}
            done = db.execute("SELECT result FROM steps WHERE case_id=? AND run_id=? AND step=?", (case_id, run_id, step)).fetchone()
            if done and done["result"] == "ok":
                return {**base, "ok": True, "result": "already_done"}
            if case["busy"] or case["stage"] != before:
                return {**base, "ok": False, "result": "busy_or_out_of_order"}
            db.execute("UPDATE cases SET busy=?, status='processing' WHERE id=?", (step, case_id))
            db.execute("INSERT OR IGNORE INTO steps(case_id,run_id,step,started) VALUES (?,?,?,?)", (case_id, run_id, step, now()))
        start = time.monotonic()
        updates, sources, attachments = {}, None, None
        result, reason = "ok", None
        try:
            current = self.db.case(case_id)
            if step == "parse":
                subject, sources, attachments, warnings = parse_email(current["raw"], self.settings)
                updates = {"subject": subject, "fields": "{}", "draft": "", "model_response": None,
                           "issues": dumps([issue("document", None, warning, "Bitte senden Sie die technischen Unterlagen als lesbares Text-PDF.") for warning in warnings])}
                if warnings:
                    result, reason = "manual", "Unterlagen benötigen eine manuelle Prüfung."
            elif step == "extract":
                raw = extract(self.settings, current["sha"], self.db.sources(case_id))
                updates["model_response"] = raw
                extraction = Extraction.model_validate_json(raw)
                updates["fields"] = dumps(validate_evidence(extraction, self.db.sources(case_id)))
            elif step == "validate":
                drawings = {c["normalized"] for c in current["fields"].get("drawing_number", []) if c["verified"]}
                erp = self.lookup(next(iter(drawings))) if len(drawings) == 1 else {"status": "skipped"}
                issues = check_rules(current["fields"], erp)
                updates = {"issues": dumps(issues), "draft": make_draft(issues)}
        except ManualCheck as exc:
            result, reason = "manual", str(exc)
        except ValidationError as exc:
            # Keep raw output in the application, but never log input values or model text.
            details = ", ".join(sorted({e["type"] for e in exc.errors(include_input=False)}))
            result, reason = "manual", f"KI-Antwort entspricht nicht dem Schema ({details}). Erneut versuchen oder manuell prüfen."
        except httpx.HTTPError:
            result, reason = "manual", "Modelldienst nicht erreichbar oder Anfrage abgewiesen. Bitte Konfiguration prüfen und erneut versuchen."
        except Exception as exc:
            result, reason = "manual", f"Verarbeitung fehlgeschlagen ({type(exc).__name__}). Bitte manuell prüfen oder erneut versuchen."
        elapsed = int((time.monotonic()-start)*1000)
        with self.db.connect(write=True) as db:
            fresh = db.execute("SELECT run_id, status, busy FROM cases WHERE id=?", (case_id,)).fetchone()
            if fresh["run_id"] != run_id or fresh["status"] != "processing" or fresh["busy"] != step:
                db.execute("UPDATE steps SET duration_ms=?, result='superseded' WHERE case_id=? AND run_id=? AND step=?", (elapsed, case_id, run_id, step))
                return {**base, "ok": False, "result": "ignored"}
            if sources is not None:
                db.execute("DELETE FROM sources WHERE case_id=?", (case_id,))
                db.execute("DELETE FROM attachments WHERE case_id=?", (case_id,))
                for source in sources:
                    db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)", (case_id + ":" + source["id"], case_id, source["kind"], source["filename"], source["page"], source["text"]))
                for attachment in attachments:
                    db.execute("INSERT INTO attachments VALUES (?,?,?,?)", (case_id + ":" + attachment["id"], case_id, attachment["filename"], attachment["raw"]))
            if result != "ok":
                updates.update(status="manual", failure_step=step, failure_reason=reason)
                # An unreadable input must never appear as a clean order.
                if not updates.get("issues"):
                    updates["issues"] = dumps([issue("processing", None, reason, None)])
            else:
                updates.update(status="ready" if step == "validate" else "processing", failure_step=None, failure_reason=None)
            updates.update(stage=after if result == "ok" else before, busy=None)
            db.execute("UPDATE cases SET " + ",".join(f"{key}=?" for key in updates) + " WHERE id=?", (*updates.values(), case_id))
            db.execute("UPDATE steps SET duration_ms=?, result=?, reason=? WHERE case_id=? AND run_id=? AND step=?", (elapsed, result, reason, case_id, run_id, step))
        return {**base, "ok": result == "ok", "result": result, "duration_ms": elapsed}

    def lookup(self, drawing):
        try:
            response = httpx.get(self.settings.erp_url + "/drawings/" + quote(drawing, safe=""), timeout=self.settings.erp_timeout,
                                 headers={"X-Internal-Token": self.settings.internal_token})
            if response.status_code == 404:
                return {"status": "not_found"}
            response.raise_for_status()
            return {"status": "ok", "revision": response.json()["revision"]}
        except httpx.TimeoutException:
            return {"status": "timeout"}
        except (httpx.HTTPError, ValueError, KeyError):
            return {"status": "unavailable"}

    def review(self, case_id, decision, draft):
        with self.db.connect(write=True) as db:
            existing = db.execute("SELECT id FROM reviews WHERE case_id=?", (case_id,)).fetchone()
            if existing:
                return existing["id"]
            case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not case:
                raise KeyError(case_id)
            if case["status"] not in ("ready", "manual"):
                raise Conflict("Verarbeitung läuft noch. Bitte zuerst das Ergebnis abwarten.")
            issues = json.loads(case["issues"])
            if decision == "complete" and (issues or case["status"] != "ready"):
                raise Conflict("Offene Prüfpunkte verhindern den Abschluss.")
            if decision == "clarify" and not draft.strip():
                raise Conflict("Bitte einen Rückfrageentwurf eintragen.")
            if decision not in ("complete", "clarify"):
                raise Conflict("Ungültige Entscheidung.")
            event_id = str(uuid4())
            db.execute("INSERT INTO reviews(id,case_id,decision,draft,reviewer,created,snapshot) VALUES (?,?,?,?,?,?,?)",
                       (event_id, case_id, decision, draft, "Demo-Ingenieur/in", now(), dumps({"fields": json.loads(case["fields"]), "issues": issues})))
            db.execute("UPDATE cases SET status='reviewed', draft=?, busy=NULL WHERE id=?", (draft, case_id))
        return event_id

    def sync(self, case_id):
        with self.db.connect() as db:
            review = db.execute("SELECT * FROM reviews WHERE case_id=?", (case_id,)).fetchone()
        if not review:
            raise Conflict("Zuerst die technische Prüfung bestätigen.")
        if review["synced"]:
            return True
        payload = {"event_id": review["id"], "case_id": case_id,
                   "status": "Rückfrage erforderlich" if review["decision"] == "clarify" else "Vorprüfung abgeschlossen",
                   "reviewed_at": review["created"], "reviewer": review["reviewer"],
                   "draft": review["draft"], "result": json.loads(review["snapshot"])}
        try:
            response = httpx.post(self.settings.erp_url + "/results", json=payload,
                                  headers={"X-Internal-Token": self.settings.internal_token}, timeout=self.settings.erp_timeout)
            response.raise_for_status()
            success, error = True, None
        except httpx.TimeoutException:
            success, error = False, "ERP-Zeitüberschreitung beim Schreiben. Sicher erneut synchronisieren."
        except httpx.HTTPError:
            success, error = False, "ERP-Schreibzugriff fehlgeschlagen. Sicher erneut synchronisieren."
        with self.db.connect(write=True) as db:
            # A late failed concurrent attempt cannot revoke a successful synchronization.
            db.execute("UPDATE reviews SET synced=?, sync_error=? WHERE id=? AND synced=0", (int(success), error, review["id"]))
        return success
