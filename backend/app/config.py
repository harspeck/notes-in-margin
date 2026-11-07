from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")  # OPENAI_API_KEY lives here, never committed


class AIReviewConfig(BaseModel):
    model: str
    reasoning_effort: str


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


class Config(BaseModel):
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
    return Config.model_validate(raw)
