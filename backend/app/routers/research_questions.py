from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, current_user, workspace_project_ids
from ..config import get_config
from ..db import get_prototype_db
from ..models_prototype import ResearchQuestion, RQFocus, RQMerge
from ..ownership import owned_rq, owned_rq_merge, owns_rq
from ..schemas import (
    FocusRequest,
    ReorderRequest,
    ResearchQuestionCreate,
    ResearchQuestionUpdate,
    RQMergeRequest,
)
from ..services.rq_merges import active_merge_edges, merge_rq, resolve_rq_id, rq_group_ids, undo_merge

router = APIRouter(prefix="/research_questions", tags=["research_questions"])
rq_merges_router = APIRouter(prefix="/rq_merges", tags=["research_questions"])
config = get_config()


def serialize_rqs(prototype_db: Session, rqs: list[ResearchQuestion]) -> list[dict]:
    edges = active_merge_edges(prototype_db)
    merge_by_source = {
        m.source_rq_id: m for m in prototype_db.query(RQMerge).filter(RQMerge.status == "active")
    }
    focused = {
        f.research_question_id for f in prototype_db.query(RQFocus).filter(RQFocus.focused.is_(True))
    }
    source_ids = {sid for rq in rqs for sid in rq_group_ids(prototype_db, rq.id, edges)}
    names = (
        {r.id: r.name for r in prototype_db.query(ResearchQuestion).filter(ResearchQuestion.id.in_(source_ids))}
        if source_ids
        else {}
    )

    def one(rq: ResearchQuestion) -> dict:
        merged = rq.id in edges
        return {
            "id": rq.id,
            "name": rq.name,
            "description": rq.description,
            "display_order": rq.display_order,
            "archived": rq.archived,
            "focused": rq.id in focused and not merged,
            "merged_into": resolve_rq_id(prototype_db, rq.id, edges) if merged else None,
            "merged_sources": [
                {
                    "merge_id": merge_by_source[sid].id,
                    "id": sid,
                    "name": names.get(sid),
                    "into_id": merge_by_source[sid].target_rq_id,
                }
                for sid in rq_group_ids(prototype_db, rq.id, edges)[1:]
            ],
        }

    return [one(rq) for rq in rqs]


def _serialize(prototype_db: Session, rq: ResearchQuestion) -> dict:
    return serialize_rqs(prototype_db, [rq])[0]


def _serialize_merge(m: RQMerge) -> dict:
    return {
        "id": m.id,
        "source_rq_id": m.source_rq_id,
        "target_rq_id": m.target_rq_id,
        "status": m.status,
        "created_at": m.created_at.isoformat(),
        "undone_at": m.undone_at.isoformat() if m.undone_at else None,
    }


@router.get("")
def list_research_questions(
    include_archived: bool = False, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    query = prototype_db.query(ResearchQuestion).filter(
        ResearchQuestion.user_id == user.id
    )
    if not include_archived:
        query = query.filter(ResearchQuestion.archived.is_(False))
    rqs = query.order_by(ResearchQuestion.display_order).all()
    return serialize_rqs(prototype_db, rqs)


@router.post("")
def create_research_question(
    body: ResearchQuestionCreate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    max_order = (
        prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user.id)
        .count()
    )
    workspace_projects = workspace_project_ids(active_workspace(prototype_db, user))
    rq = ResearchQuestion(
        user_id=user.id,
        # A research question spans the workspace; project_id records where it started.
        project_id=workspace_projects[0] if workspace_projects else config.default_project_id,
        name=body.name,
        description=body.description,
        display_order=max_order,
    )
    prototype_db.add(rq)
    prototype_db.commit()
    prototype_db.refresh(rq)
    return _serialize(prototype_db, rq)


@router.patch("/{rq_id}")
def update_research_question(
    rq_id: int, body: ResearchQuestionUpdate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    rq = owned_rq(prototype_db, rq_id, user)
    if body.name is not None:
        rq.name = body.name
    if body.description is not None:
        rq.description = body.description
    if body.archived is not None:
        if not body.archived and rq.id in active_merge_edges(prototype_db):
            raise HTTPException(409, "this research question is merged into another; undo the merge instead")
        rq.archived = body.archived
    prototype_db.commit()
    prototype_db.refresh(rq)
    return _serialize(prototype_db, rq)


@router.put("/reorder")
def reorder_research_questions(
    body: ReorderRequest, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    for order, rq_id in enumerate(body.ordered_ids):
        if owns_rq(prototype_db, rq_id, user):
            prototype_db.get(ResearchQuestion, rq_id).display_order = order
    prototype_db.commit()
    return {"reordered": True}


@router.post("/{rq_id}/focus")
def set_focus(rq_id: int, body: FocusRequest, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)):
    rq = owned_rq(prototype_db, rq_id, user)
    if rq.id in active_merge_edges(prototype_db):
        raise HTTPException(409, "this research question is merged into another; focus its target instead")
    focus = prototype_db.get(RQFocus, rq.id)
    if focus is None:
        focus = RQFocus(research_question_id=rq.id, user_id=user.id)
        prototype_db.add(focus)
    focus.focused = body.focused
    prototype_db.commit()
    return _serialize(prototype_db, rq)


@router.post("/{rq_id}/merge_into")
def merge_into(rq_id: int, body: RQMergeRequest, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)):
    source = owned_rq(prototype_db, rq_id, user)
    target = owned_rq(prototype_db, body.target_id, user)
    try:
        merge = merge_rq(prototype_db, user_id=user.id, source=source, target=target)
    except ValueError as e:
        raise HTTPException(409, str(e))
    prototype_db.refresh(source)
    prototype_db.refresh(target)
    return {
        "merge": _serialize_merge(merge),
        "source": _serialize(prototype_db, source),
        "target": _serialize(prototype_db, target),
    }


@rq_merges_router.post("/{merge_id}/undo")
def undo(
    merge_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    merge = owned_rq_merge(prototype_db, merge_id, user)
    try:
        kept = undo_merge(prototype_db, merge)
    except ValueError as e:
        raise HTTPException(409, str(e))
    source = prototype_db.get(ResearchQuestion, merge.source_rq_id)
    target = prototype_db.get(ResearchQuestion, merge.target_rq_id)
    return {
        "merge": _serialize_merge(merge),
        "source": _serialize(prototype_db, source),
        "target": _serialize(prototype_db, target),
        "families_kept_with_target": [{"id": f.id, "name": f.name} for f in kept],
    }
