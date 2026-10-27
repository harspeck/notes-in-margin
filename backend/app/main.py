from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models_prototype  # noqa: F401 — registers tables on PrototypeBase
from .config import get_config
from .db import PrototypeBase, PrototypeSession, prototype_engine
from .routers import (
    checkpoints,
    code_families,
    conversations,
    highlights,
    memo,
    next_steps,
    organize,
    research_questions,
    review,
    settings,
    voice_memos,
)
from .seed import seed_research_questions

config = get_config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    PrototypeBase.metadata.create_all(bind=prototype_engine)
    with PrototypeSession() as db:
        seed_research_questions(db)
    yield


app = FastAPI(title="Reflective Listening Prototype", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/audio", StaticFiles(directory=str(config.audio_dir_abspath)), name="audio")

app.include_router(conversations.router)
app.include_router(highlights.router)
app.include_router(checkpoints.router)
app.include_router(research_questions.router)
app.include_router(research_questions.rq_merges_router)
app.include_router(voice_memos.router)
app.include_router(review.router)
app.include_router(memo.router)
app.include_router(next_steps.router)
app.include_router(code_families.router)
app.include_router(organize.router)
app.include_router(settings.router)


@app.get("/health")
def health():
    return {"ok": True}
