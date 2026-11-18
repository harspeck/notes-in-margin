"""Reads the activity log. Rows are written by services/activity.py, which lists the kinds."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user
from ..db import get_prototype_db
from ..models_prototype import Activity
from ..services.activity import CHANGED, SET_ASIDE, kinds_in
from ..timefmt import iso_utc

router = APIRouter(prefix="/activity", tags=["activity"])


def _serialize(a: Activity) -> dict:
    return {
        "id": a.id,
        "created_at": iso_utc(a.created_at),
        "actor": a.actor,
        "kind": a.kind,
        "summary": a.summary,
        "refs": json.loads(a.refs_json) if a.refs_json else {},
    }


@router.get("")
def list_activity(
    filter: Literal["all", "ai", "changed", "set_aside"] = "all",
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, description="Return rows older than this activity id."),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Newest first. Ids rise with time, so `before` (the `next_before` of the previous
    page) pages backwards without skipping rows written in between."""
    query = prototype_db.query(Activity).filter(Activity.user_id == user.id)
    if filter == "ai":
        query = query.filter(Activity.actor == "ai")
    elif filter == "changed":
        query = query.filter(Activity.kind.in_(kinds_in(CHANGED)))
    elif filter == "set_aside":
        query = query.filter(Activity.kind.in_(kinds_in(SET_ASIDE)))
    if before is not None:
        query = query.filter(Activity.id < before)
    rows = query.order_by(Activity.id.desc()).limit(limit + 1).all()
    page = rows[:limit]
    return {
        "items": [_serialize(a) for a in page],
        "next_before": page[-1].id if len(rows) > limit else None,
    }
