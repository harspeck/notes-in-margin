from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import models_prototype  # noqa: F401 — registers tables on PrototypeBase
from .auth import current_user
from .config import get_config
from .cortico.errors import CorticoError
from .db import PrototypeBase, PrototypeSession, prototype_engine
from .routers import (
    auth,
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
    workspaces,
)
from .seed import seed_research_questions

config = get_config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    PrototypeBase.metadata.create_all(bind=prototype_engine)
    if config.data_source == "snapshot":
        # The seed RQs belong to the single implicit user; signed-in users start with none.
        with PrototypeSession() as db:
            seed_research_questions(db)
    yield


app = FastAPI(title="Notes in the Margin API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,  # the session cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(CorticoError)
def _cortico_error(_request: Request, exc: CorticoError):
    headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
    return JSONResponse(
        {"detail": exc.detail, "source": "cortico", "upstream_status": exc.upstream_status},
        status_code=exc.status,
        headers=headers,
    )


try:
    app.mount("/audio", StaticFiles(directory=str(config.audio_dir_abspath)), name="audio")
except OSError:
    pass  # read-only filesystem (Vercel): voice memo audio needs Blob storage there

# Every app route needs a signed-in user in cortico_api mode (a no-op in snapshot mode).
signed_in = [Depends(current_user)]
app.include_router(auth.router)
app.include_router(workspaces.router, dependencies=signed_in)
app.include_router(conversations.router, dependencies=signed_in)
app.include_router(highlights.router, dependencies=signed_in)
app.include_router(checkpoints.router, dependencies=signed_in)
app.include_router(research_questions.router, dependencies=signed_in)
app.include_router(research_questions.rq_merges_router, dependencies=signed_in)
app.include_router(voice_memos.router, dependencies=signed_in)
app.include_router(review.router, dependencies=signed_in)
app.include_router(memo.router, dependencies=signed_in)
app.include_router(next_steps.router, dependencies=signed_in)
app.include_router(code_families.router, dependencies=signed_in)
app.include_router(organize.router, dependencies=signed_in)
app.include_router(settings.router, dependencies=signed_in)


@app.get("/health")
def health():
    return {"ok": True}
