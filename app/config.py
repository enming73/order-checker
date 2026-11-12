import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

@dataclass
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    llm_mode: str = os.getenv("LLM_MODE", "demo")
    llm_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    workflow_url: str = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/auftragspruefung")
    internal_token: str = os.getenv("INTERNAL_TOKEN", "local-demo-token-change-me")
    erp_url: str = os.getenv("ERP_BASE_URL", "http://localhost:8000/mock-erp")
    erp_timeout: float = float(os.getenv("ERP_TIMEOUT_SECONDS", "1"))
    max_upload: int = 12 * 1024 * 1024
    max_attachment: int = 5 * 1024 * 1024
    max_text: int = 150_000
