"""Deterministic fictitious email/PDF fixtures and prepared, explicit model outputs."""
import hashlib
import json
from email.message import EmailMessage
from email import policy
from io import BytesIO
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

ROOT = Path(__file__).resolve().parents[1]
manifest = {}
for letter, suffix, drawing, rev, pdf_rev, ip in [
    ("A", "0141", "SZ-4101", "A", "A", "IP54"),
    ("B", "0142", "SZ-4102", "C", "C", None),
    ("C", "0143", "SZ-4103", "C", "B", "IP54"),
]:
    filename = f"Technische-Unterlagen-{letter}.pdf"
    common = [("order_number", "Auftragsnummer", f"AB-2026-{suffix}"),
              ("drawing_number", "Zeichnungsnummer", drawing),
              ("drawing_revision", "Zeichnungsstand", f"Rev. {rev}"),
              ("quantity", "Stückzahl", "2")]
    if ip:
        common.append(("ip_rating", "IP-Schutzart", ip))
    email_lines = [f"{label}: {value}" for _, label, value in common]
    pdf_fields = [(key, label, f"Rev. {pdf_rev}" if key == "drawing_revision" else value) for key, label, value in common]
    output = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica"), NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    lines = [(22, 770, "TECHNISCHE UNTERLAGEN"), (11, 742, f"Muster {letter} / Fiktive Nordwerk Anlagenbau GmbH")]
    lines += [(14, 660 - idx*40, f"{label}: {value}") for idx, (_, label, value) in enumerate(pdf_fields)]
    lines += [(10, 90, "Nur Demonstration. Keine Fertigungsfreigabe. Keine realen Kundendaten."), (10, 70, "Seite 1 / 1")]
    content = b"0.08 0.2 0.22 rg\n"
    for size, y, line in lines:
        encoded = line.encode("cp1252").hex()
        content += f"BT /F1 {size} Tf 45 {y} Td <{encoded}> Tj ET\n".encode()
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    (ROOT / "samples" / filename).write_bytes(output.getvalue())
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "Lena Beispiel <einkauf@nordwerk.example>"
    msg["To"] = "technik@schaltschrank.example"
    msg["Subject"] = f"Bestellung AB-2026-{suffix} – Muster {letter}"
    msg["Date"] = "Thu, 12 Nov 2026 09:00:00 +0100"
    msg["Message-ID"] = f"<demo-{letter}@nordwerk.example>"
    msg.set_content("Guten Tag,\n\nbitte fertigen Sie die Schaltschränke gemäß folgenden Angaben:\n\n" + "\n".join(email_lines) + "\n\nDie technischen Unterlagen finden Sie im Anhang.\n\nMit freundlichen Grüßen\nLena Beispiel\n")
    msg.add_attachment(output.getvalue(), maintype="application", subtype="pdf", filename=filename)
    msg.set_boundary(f"demo-order-{letter}-stable-boundary")
    raw = msg.as_bytes()
    (ROOT / "samples" / f"Auftrag-{letter}.eml").write_bytes(raw)
    response = {k: [] for k in ("order_number", "drawing_number", "drawing_revision", "ip_rating", "quantity")}
    for kind, fields in [("email", common), ("pdf", pdf_fields)]:
        for key, label, value in fields:
            response[key].append({"value": value, "evidence": {
                "source_id": "email" if kind == "email" else "pdf-0-1", "source_type": kind,
                "attachment_name": filename if kind == "pdf" else None, "page": 1 if kind == "pdf" else None,
                "quote": f"{label}: {value}"}})
    response_name = f"{letter}.json"
    (ROOT / "samples/responses" / response_name).write_text(json.dumps(response, indent=2, ensure_ascii=False))
    manifest[hashlib.sha256(raw).hexdigest()] = response_name
(ROOT / "samples/manifest.json").write_text(json.dumps(manifest, indent=2))
