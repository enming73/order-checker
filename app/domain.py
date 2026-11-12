import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

LABELS = {"order_number": "Auftragsnummer", "drawing_number": "Zeichnungsnummer",
          "drawing_revision": "Zeichnungsstand", "ip_rating": "IP-Schutzart", "quantity": "Stückzahl"}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    source_type: Literal["email", "pdf"]
    attachment_name: str | None
    page: int | None
    quote: str = Field(min_length=1, max_length=2000)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=120)
    evidence: Evidence


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_number: list[Candidate] = Field(max_length=30)
    drawing_number: list[Candidate] = Field(max_length=30)
    drawing_revision: list[Candidate] = Field(max_length=30)
    ip_rating: list[Candidate] = Field(max_length=30)
    quantity: list[Candidate] = Field(max_length=30)


def normalize(field, value):
    value = value.strip().upper()
    if field == "drawing_revision":
        return re.sub(r"^(?:REVISION|REV\.?|STAND)\s*", "", value).strip()
    if field == "ip_rating":
        return re.sub(r"\s+", "", value)
    if field == "quantity" and value.isdigit():
        return str(int(value))
    return value


def value_in_quote(field, value, quote):
    patterns = {
        "drawing_revision": r"(?:Rev(?:ision)?\.?|Stand)\s*([A-Z0-9]+)\b",
        "ip_rating": r"\bIP\s*\d{2}\b",
        "quantity": r"(?:Stückzahl|Menge|Quantity)\s*[:=]?\s*(\d+)\b",
    }
    if field in patterns:
        matches = re.findall(patterns[field], quote, re.I)
        return normalize(field, value) in [normalize(field, m) for m in matches]
    return bool(re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", quote, re.I))


def validate_evidence(extraction, sources):
    indexed = {s["id"]: s for s in sources}
    fields = {}
    for field, candidates in extraction.model_dump().items():
        fields[field] = []
        for candidate in candidates:
            evidence = candidate["evidence"]
            source = indexed.get(evidence["source_id"])
            valid = bool(source and source["kind"] == evidence["source_type"]
                         and source["filename"] == evidence["attachment_name"]
                         and source["page"] == evidence["page"]
                         and evidence["quote"] in source["text"]
                         and value_in_quote(field, candidate["value"], evidence["quote"]))
            candidate["verified"] = valid
            candidate["normalized"] = normalize(field, candidate["value"])
            fields[field].append(candidate)
    return fields


def issue(code, field, message, question, candidates=None):
    return {"code": code, "field": field, "message": message, "question": question,
            "evidence": [c["evidence"] for c in (candidates or [])]}


def check_rules(fields, erp):
    problems = []
    for field, label in LABELS.items():
        candidates = fields.get(field, [])
        valid = [c for c in candidates if c["verified"]]
        if not valid:
            problems.append(issue("missing", field, f"{label} fehlt oder ist nicht belegt.",
                                  f"Bitte teilen Sie uns die Angabe zu {label} mit."))
        invalid = [c for c in candidates if not c["verified"]]
        if invalid:
            problems.append(issue("evidence", field, f"{label}: Beleg muss manuell geprüft werden.",
                                  f"Bitte bestätigen Sie die Angabe zu {label} mit einem eindeutigen Beleg.", invalid))
        values = {c["normalized"] for c in valid}
        if len(values) > 1:
            question = ("Welcher Zeichnungsstand ist für diesen Auftrag verbindlich?"
                        if field == "drawing_revision" else f"Welche Angabe zu {label} ist verbindlich?")
            problems.append(issue("conflict", field, f"{label}: widersprüchliche Angaben ({', '.join(sorted(values))}).", question, valid))
        if field == "quantity" and any(not v.isdigit() or int(v) <= 0 for v in values):
            problems.append(issue("invalid", field, "Stückzahl muss eine positive ganze Zahl sein.", "Bitte bestätigen Sie die Stückzahl."))
        if field == "ip_rating" and any(not re.fullmatch(r"IP[0-6][0-9]", v) for v in values):
            problems.append(issue("invalid", field, "IP-Schutzart hat ein ungültiges Format.", "Bitte bestätigen Sie die IP-Schutzart."))
    if erp["status"] == "timeout":
        problems.append(issue("erp_timeout", "drawing_revision", "ERP-Zeitüberschreitung. Abgleich erneut ausführen.", None))
    elif erp["status"] == "not_found":
        problems.append(issue("erp_not_found", "drawing_number", "Zeichnung im ERP nicht registriert.", "Bitte bestätigen Sie die Zeichnungsnummer und den zugehörigen Zeichnungsstand."))
    elif erp["status"] == "unavailable":
        problems.append(issue("erp_unavailable", "drawing_revision", "ERP nicht erreichbar. Abgleich erneut ausführen.", None))
    elif erp["status"] == "ok":
        revisions = [c for c in fields.get("drawing_revision", []) if c["verified"]]
        if any(c["normalized"] != normalize("drawing_revision", erp["revision"]) for c in revisions):
            problems.append(issue("erp_conflict", "drawing_revision", f"ERP führt Rev. {erp['revision']}; Unterlagen weichen ab.",
                                  "Welcher Zeichnungsstand ist für diesen Auftrag verbindlich?", revisions))
    return problems


def make_draft(issues):
    questions = list(dict.fromkeys(i["question"] for i in issues if i.get("question")))
    if not questions:
        return ""
    return "Guten Tag,\n\nbei der technischen Vorprüfung benötigen wir folgende Klärung:\n\n" + "\n".join("• " + q for q in questions) + "\n\nVielen Dank.\nMit freundlichen Grüßen\nTechnische Auftragsprüfung"
