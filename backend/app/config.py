from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")  # ANTHROPIC_API_KEY (and OPENAI_API_KEY) live here, never committed


class AIReviewConfig(BaseModel):
    provider: str = "anthropic"  # "anthropic" or "openai"; see app/llm.py
    model: str
    reasoning_effort: str
    max_output_tokens: int = 32000


class TranscriptionConfig(BaseModel):
    engine: str
    model_size: str
    compute_type: str


class VoiceMemoConfig(BaseModel):
    max_duration_seconds: int


class CodeFamiliesConfig(BaseModel):
    max_per_rq: int  # cap on non-dismissed, non-merged families per RQ (user can override)
    near_cap_at: int  # at or above this many, synthesis also proposes merges
    max_new_per_run: int


class CorticoWriteBackConfig(BaseModel):
    highlights: bool = True


class CorticoConfig(BaseModel):
    base_url: str = "https://api2.cortico.ai"
    timeout_seconds: float = 20.0
    max_retries: int = 2  # extra attempts on 429 / 5xx / network errors
    snippet_cache_ttl_seconds: int = 3600
    tool_id: str = "notes-in-the-margin"
    write_back: CorticoWriteBackConfig = CorticoWriteBackConfig()


class SessionConfig(BaseModel):
    cookie_name: str = "nim_session"
    max_age_days: int = 7
    cookie_samesite: str = "lax"


class Config(BaseModel):
    # "snapshot" reads the local read-only Cortico copy as one implicit user (local dev and
    # tests). "cortico_api" signs users in with Cortico and reads the v2 API with their token.
    data_source: str = "snapshot"
    database_url: str | None = None  # sidecar database; None means the SQLite file below
    session_secret: str | None = None
    cors_origins: list[str] = ["http://localhost:5173"]
    cortico: CorticoConfig = CorticoConfig()
    session: SessionConfig = SessionConfig()
    default_user_id: int
    default_project_id: int
    cortico_db_path: str
    prototype_db_path: str
    audio_dir: str
    ai_review: AIReviewConfig
    transcription: TranscriptionConfig
    voice_memo: VoiceMemoConfig
    code_families: CodeFamiliesConfig

    @property
    def project_root(self) -> Path:
        return BACKEND_DIR.parent

    @property
    def cortico_db_abspath(self) -> Path:
        return (BACKEND_DIR / self.cortico_db_path).resolve()

    @property
    def prototype_db_abspath(self) -> Path:
        path = (BACKEND_DIR.parent / self.prototype_db_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def audio_dir_abspath(self) -> Path:
        path = (BACKEND_DIR.parent / self.audio_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_config() -> Config:
    with open(BACKEND_DIR / "config.yaml") as f:
        raw = yaml.safe_load(f)
    # Lets tests (see tests/conftest.py) point the prototype-owned tables at a scratch
    # file instead of the real dev database, without touching config.yaml.
    override = os.environ.get("PROTOTYPE_DB_PATH")
    if override:
        raw["prototype_db_path"] = override
    snapshot_override = os.environ.get("CORTICO_DB_PATH")
    if snapshot_override:
        raw["cortico_db_path"] = snapshot_override
    if os.environ.get("DATA_SOURCE"):
        raw["data_source"] = os.environ["DATA_SOURCE"]
    # Vercel's Neon integration exposes DATABASE_URL (pooled) and POSTGRES_URL.
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if database_url:
        raw["database_url"] = database_url
    if os.environ.get("SESSION_SECRET"):
        raw["session_secret"] = os.environ["SESSION_SECRET"]
    if os.environ.get("CORS_ORIGINS"):
        raw["cors_origins"] = [o.strip() for o in os.environ["CORS_ORIGINS"].split(",") if o.strip()]
    if os.environ.get("CORTICO_BASE_URL"):
        raw.setdefault("cortico", {})["base_url"] = os.environ["CORTICO_BASE_URL"]
    if os.environ.get("SESSION_COOKIE_SAMESITE"):
        raw.setdefault("session", {})["cookie_samesite"] = os.environ["SESSION_COOKIE_SAMESITE"]
    for env, key in (("AI_PROVIDER", "provider"), ("AI_MODEL", "model"), ("AI_REASONING_EFFORT", "reasoning_effort")):
        if os.environ.get(env):
            raw.setdefault("ai_review", {})[key] = os.environ[env]
    if raw.get("data_source") not in ("snapshot", "cortico_api"):
        raise ValueError(f"data_source must be 'snapshot' or 'cortico_api', got {raw.get('data_source')!r}")
    return Config.model_validate(raw)
