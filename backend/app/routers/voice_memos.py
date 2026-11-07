from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user
from ..config import get_config
from ..db import PrototypeSession, get_prototype_db
from ..models_prototype import VoiceMemo
from ..ownership import owned_voice_memo
from ..schemas import VoiceMemoEdit
from ..services.transcription import ENGINE_LABEL, transcribe

router = APIRouter(tags=["voice_memos"])
config = get_config()


def _serialize(vm: VoiceMemo) -> dict:
    return {
        "id": vm.id,
        "conversation_id": vm.conversation_id,
        "audio_path": vm.audio_path,
        "duration": vm.duration,
        "recorded_at": vm.recorded_at.isoformat(),
        "transcription_raw": vm.transcription_raw,
        "transcription_edited": vm.transcription_edited,
        "transcription_engine": vm.transcription_engine,
        "transcription_progress": vm.transcription_progress,
        "transcription_error": vm.transcription_error,
        "extracted_interests": vm.extracted_interests,
        "status": vm.status,
    }


def _run_transcription_in_background(voice_memo_id: int, audio_path: str) -> None:
    """Runs off the request thread so the upload response returns immediately — the
    frontend polls GET /voice_memos/{id} for transcription_progress. Opens its own DB
    session since the request-scoped one is closed by the time this runs."""
    db = PrototypeSession()
    try:
        def on_progress(fraction: float) -> None:
            vm = db.get(VoiceMemo, voice_memo_id)
            vm.transcription_progress = fraction
            db.commit()

        text, words_json = transcribe(audio_path, config, on_progress=on_progress)

        vm = db.get(VoiceMemo, voice_memo_id)
        vm.transcription_raw = words_json
        vm.transcription_edited = text
        vm.transcription_engine = ENGINE_LABEL
        vm.transcription_progress = 1.0
        vm.status = "ready_for_review"
        db.commit()
    except Exception as e:  # noqa: BLE001 — surface any transcription failure to the UI
        vm = db.get(VoiceMemo, voice_memo_id)
        vm.status = "failed"
        vm.transcription_error = str(e)
        db.commit()
    finally:
        db.close()


@router.post("/conversations/{conversation_id}/voice_memos")
async def create_voice_memo(
    conversation_id: int,
    audio: UploadFile,
    duration: float = Form(...),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    if config.data_source == "cortico_api":
        # Audio lives on local disk and transcription runs faster-whisper locally; the hosted
        # backend has neither until voice memos move to Blob storage and hosted transcription.
        raise HTTPException(501, "Voice memos aren't available on the hosted backend yet.")
    if duration > config.voice_memo.max_duration_seconds:
        raise HTTPException(400, f"recording exceeds the {config.voice_memo.max_duration_seconds}s cap")

    suffix = ".webm"
    filename = f"voice_memo_{uuid.uuid4().hex}{suffix}"
    dest = config.audio_dir_abspath / filename
    dest.write_bytes(await audio.read())

    vm = VoiceMemo(
        user_id=user.id,
        conversation_id=conversation_id,
        audio_path=str(dest.relative_to(config.project_root)),
        duration=duration,
        recorded_at=datetime.now(timezone.utc),
        status="transcribing",
        transcription_progress=0.0,
    )
    prototype_db.add(vm)
    prototype_db.commit()
    prototype_db.refresh(vm)

    threading.Thread(
        target=_run_transcription_in_background, args=(vm.id, str(dest)), daemon=True
    ).start()

    return _serialize(vm)


@router.get("/voice_memos/{voice_memo_id}")
def get_voice_memo(
    voice_memo_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    return _serialize(vm)


@router.patch("/voice_memos/{voice_memo_id}")
def edit_voice_memo(
    voice_memo_id: int, body: VoiceMemoEdit, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    vm.transcription_edited = body.transcription_edited
    prototype_db.commit()
    prototype_db.refresh(vm)
    return _serialize(vm)


@router.get("/conversations/{conversation_id}/voice_memos")
def list_voice_memos_for_conversation(
    conversation_id: int, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    vms = (
        prototype_db.query(VoiceMemo)
        .filter(VoiceMemo.conversation_id == conversation_id, VoiceMemo.user_id == user.id)
        .order_by(VoiceMemo.recorded_at)
        .all()
    )
    return [_serialize(vm) for vm in vms]
