from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user
from ..db import get_prototype_db
from ..schemas import PromptUpdate
from ..services import ai_review

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/review-prompt")
def get_review_prompt(prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)):
    return {
        "text": ai_review.get_review_prompt(prototype_db, user.id),
        "default_text": ai_review.DEFAULT_REVIEW_PROMPT,
    }


@router.put("/review-prompt")
def save_review_prompt(
    body: PromptUpdate, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    ai_review.set_review_prompt(prototype_db, body.text, user.id)
    return {"text": body.text}


@router.post("/review-prompt/reset")
def reset_review_prompt(prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)):
    text = ai_review.reset_review_prompt(prototype_db, user.id)
    return {"text": text}
