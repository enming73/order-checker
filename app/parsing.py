import logging
from email import policy
from email.parser import BytesParser
from io import BytesIO
from bs4 import BeautifulSoup
from pypdf import PdfReader


# Library diagnostics may include bytes from malformed PDF headers.
_pdf_logger = logging.getLogger("pypdf")
_pdf_logger.addHandler(logging.NullHandler())
_pdf_logger.propagate = False


class ManualCheck(Exception):
    pass


def parse_email(raw, settings):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if msg.defects or not (msg.get("From") and msg.get("Subject")):
        raise ManualCheck("E-Mail ist beschädigt oder enthält keine gültigen Kopfzeilen.")
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if not isinstance(text, str):
        raise ManualCheck("E-Mail-Text konnte nicht gelesen werden.")
    if body and body.get_content_type() == "text/html":
        soup = BeautifulSoup(text, "html.parser")
        for el in soup(["script", "style"]):
            el.decompose()
        text = soup.get_text("\n", strip=True)
    sources = [{"id": "email", "kind": "email", "filename": None, "page": None, "text": text}]
    attachments, warnings = [], []
    for index, part in enumerate(msg.iter_attachments()):
        filename = part.get_filename() or f"Anhang-{index + 1}"
        payload = part.get_payload(decode=True) or b""
        attachments.append({"id": f"attachment-{index}", "filename": filename, "raw": payload})
        if len(payload) > settings.max_attachment:
            warnings.append(f"{filename}: Anhang überschreitet 5 MiB. Bitte manuell prüfen.")
            continue
        if not filename.lower().endswith(".pdf"):
            warnings.append(f"{filename}: Nur Text-PDF wird unterstützt. Bitte manuell prüfen.")
            continue
        try:
            reader = PdfReader(BytesIO(payload), strict=True)
            if reader.is_encrypted or len(reader.pages) > 30:
                raise ValueError("PDF limit")
            for page_number, page in enumerate(reader.pages, 1):
                page_text = page.extract_text() or ""
                if not page_text.strip():
                    warnings.append(f"{filename}, Seite {page_number}: Kein Textlayer (Scan). Bitte manuell prüfen.")
                sources.append({"id": f"pdf-{index}-{page_number}", "kind": "pdf", "filename": filename,
                                "page": page_number, "text": page_text})
        except Exception:
            warnings.append(f"{filename}: PDF beschädigt, verschlüsselt oder über 30 Seiten. Bitte manuell prüfen.")
    if sum(len(s["text"]) for s in sources) > settings.max_text:
        raise ManualCheck("Textumfang überschreitet das Verarbeitungslimit. Bitte manuell prüfen.")
    return str(msg["Subject"]), sources, attachments, warnings
