from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..models_prototype import CodeFamily, CodeFamilyMember, CodeFamilyMergeSuggestion, ReflectionConnection
from ..ownership import owned_family, owned_merge_suggestion, owned_rq
from ..schemas import (
    CodeFamilyCreate,
    CodeFamilyMembersUpdate,
    CodeFamilyMergeRequest,
    CodeFamilyUpdate,
    StarRequest,
)
from ..services.code_families import (
    LIVE_FAMILY_STATUSES,
    _merge_target,
    _valid_connection_ids,
    accept_merge,
    cap_status,
    group_for,
    merge_families,
    set_star,
    starred_family_ids,
    synthesize_code_families,
    write_revision,
)
from ..services.rq_merges import resolve_rq_id

router = APIRouter(tags=["code_families"])
config = get_config()


def _insight_texts_for(prototype_db: Session, families: list[CodeFamily]) -> dict[int, str]:
    connection_ids = {m.connection_id for f in families for m in f.members}
    if not connection_ids:
        return {}
    return {
        c.id: c.insight_text
        for c in prototype_db.query(ReflectionConnection).filter(ReflectionConnection.id.in_(connection_ids))
    }


def _serialize(family: CodeFamily, insight_by_connection: dict[int, str], starred: bool = False) -> dict:
    return {
        "id": family.id,
        "research_question_id": family.research_question_id,
        "name": family.name,
        "definition": family.definition,
        "status": family.status,
        "starred": starred,
        "ai_metadata": json.loads(family.ai_metadata) if family.ai_metadata else None,
        "created_at": family.created_at.isoformat(),
        "members": [
            {"connection_id": m.connection_id, "insight_text": insight_by_connection.get(m.connection_id, "")}
            for m in family.members
        ],
    }


def _serialize_merge(m: CodeFamilyMergeSuggestion) -> dict:
    return {
        "id": m.id,
        "research_question_id": m.research_question_id,
        "family_ids": json.loads(m.family_ids_json),
        "target_family_id": m.target_family_id,
        "proposed_name": m.proposed_name,
        "proposed_definition": m.proposed_definition,
        "rationale": m.rationale,
        "status": m.status,
        "ai_metadata": json.loads(m.ai_metadata) if m.ai_metadata else None,
        "created_at": m.created_at.isoformat(),
    }


def serialize_families(prototype_db: Session, families: list[CodeFamily]) -> list[dict]:
    insight_by_connection = _insight_texts_for(prototype_db, families)
    starred = starred_family_ids(prototype_db, [f.id for f in families])
    return [_serialize(f, insight_by_connection, f.id in starred) for f in families]


def _serialize_one(prototype_db: Session, family: CodeFamily) -> dict:
    return serialize_families(prototype_db, [family])[0]


def cap_dict(live_count: int) -> dict:
    cap = cap_status(live_count, config.code_families, override_cap=False)
    return {
        "live_count": cap.live_count,
        "max_per_rq": cap.max_per_rq,
        "near_cap": cap.near_cap,
        "at_cap": cap.live_count >= cap.max_per_rq,
    }


def _pending_merges(prototype_db: Session, rq_id: int) -> list[dict]:
    """Pending merge suggestions whose families are all still live — one whose family was
    dismissed or merged elsewhere since is stale and just hidden (still logged)."""
    group = group_for(prototype_db, rq_id)
    live_ids = {
        f.id
        for f in prototype_db.query(CodeFamily.id).filter(
            CodeFamily.research_question_id.in_(group), CodeFamily.status.in_(LIVE_FAMILY_STATUSES)
        )
    }
    merges = (
        prototype_db.query(CodeFamilyMergeSuggestion)
        .filter(
            CodeFamilyMergeSuggestion.research_question_id.in_(group),
            CodeFamilyMergeSuggestion.status == "suggested",
        )
        .order_by(CodeFamilyMergeSuggestion.created_at)
        .all()
    )
    return [_serialize_merge(m) for m in merges if set(json.loads(m.family_ids_json)) <= live_ids]


def _state_for_rq(prototype_db: Session, rq_id: int, withheld_new_families: int = 0) -> dict:
    families = _list_for_rq(prototype_db, rq_id, include_dismissed=False)
    live_count = sum(1 for f in families if f["status"] in LIVE_FAMILY_STATUSES)
    return {
        "families": families,
        "merge_suggestions": _pending_merges(prototype_db, rq_id),
        "cap": cap_dict(live_count),
        "withheld_new_families": withheld_new_families,
    }


def live_families_for_group(prototype_db: Session, group: list[int]) -> list[CodeFamily]:
    return (
        prototype_db.query(CodeFamily)
        .filter(CodeFamily.research_question_id.in_(group), CodeFamily.status.in_(LIVE_FAMILY_STATUSES))
        .order_by(CodeFamily.created_at, CodeFamily.id)
        .all()
    )


def _list_for_rq(prototype_db: Session, rq_id: int, include_dismissed: bool) -> list[dict]:
    query = prototype_db.query(CodeFamily).filter(CodeFamily.research_question_id.in_(group_for(prototype_db, rq_id)))
    if not include_dismissed:
        query = query.filter(CodeFamily.status.in_(LIVE_FAMILY_STATUSES))
    return serialize_families(prototype_db, query.order_by(CodeFamily.created_at, CodeFamily.id).all())


def _checked_connection_ids(prototype_db: Session, rq_id: int, connection_ids: list[int]) -> list[int]:
    """De-duplicated ids, each a filed entry in rq_id's merged group, else 422."""
    ids = list(dict.fromkeys(connection_ids))
    valid = _valid_connection_ids(prototype_db, rq_id)
    invalid = [cid for cid in ids if cid not in valid]
    if invalid:
        raise HTTPException(422, f"not filed memo entries of this research question: {invalid}")
    return ids


def _require_text(value: str, field: str) -> str:
    if not value.strip():
        raise HTTPException(422, f"{field} must not be empty")
    return value.strip()


@router.get("/research_questions/{rq_id}/code_families")
def list_code_families(
    rq_id: int, include_dismissed: bool = False, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    owned_rq(prototype_db, rq_id, user)
    return _list_for_rq(prototype_db, rq_id, include_dismissed)


@router.get("/research_questions/{rq_id}/code_families/state")
def code_family_state(
    rq_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    """Families plus pending merge suggestions and where the RQ stands against its cap."""
    owned_rq(prototype_db, rq_id, user)
    return _state_for_rq(prototype_db, rq_id)


@router.post("/research_questions/{rq_id}/code_families/synthesize")
def synthesize(
    rq_id: int,
    override_cap: bool = False,
    cortico_db: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    owned_rq(prototype_db, rq_id, user)
    result = synthesize_code_families(
        prototype_db=prototype_db,
        cortico_db=cortico_db,
        config=config,
        user_id=user.id,
        research_question_id=rq_id,
        override_cap=override_cap,
    )
    return _state_for_rq(prototype_db, rq_id, withheld_new_families=result.withheld_new_families)


@router.patch("/code_families/{family_id}")
def update_family(
    family_id: int, body: CodeFamilyUpdate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    family = owned_family(prototype_db, family_id, user)
    if body.name is not None or body.definition is not None:
        write_revision(prototype_db, family)
    if body.name is not None:
        family.name = body.name
    if body.definition is not None:
        family.definition = body.definition
    prototype_db.commit()
    prototype_db.refresh(family)
    return _serialize_one(prototype_db, family)


@router.post("/research_questions/{rq_id}/code_families")
def create_family(rq_id: int, body: CodeFamilyCreate, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)):
    """A user-made family: accepted, no ai_metadata, and not blocked by the cap (the
    returned cap lets the UI warn)."""
    owned_rq(prototype_db, rq_id, user)
    name = _require_text(body.name, "name")
    definition = _require_text(body.definition, "definition")
    connection_ids = _checked_connection_ids(prototype_db, rq_id, body.connection_ids)
    target_rq_id = resolve_rq_id(prototype_db, rq_id)

    family = CodeFamily(
        user_id=user.id,
        research_question_id=target_rq_id,
        name=name,
        definition=definition,
        status="accepted",
        ai_metadata=None,
    )
    prototype_db.add(family)
    prototype_db.flush()
    for cid in connection_ids:
        prototype_db.add(CodeFamilyMember(code_family_id=family.id, connection_id=cid))
    prototype_db.commit()
    prototype_db.refresh(family)
    live = live_families_for_group(prototype_db, group_for(prototype_db, target_rq_id))
    return {"family": _serialize_one(prototype_db, family), "cap": cap_dict(len(live))}


@router.put("/code_families/{family_id}/members")
def replace_members(
    family_id: int, body: CodeFamilyMembersUpdate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    family = owned_family(prototype_db, family_id, user)
    if family.status not in LIVE_FAMILY_STATUSES:
        raise HTTPException(409, f"code family is {family.status}")
    connection_ids = _checked_connection_ids(prototype_db, family.research_question_id, body.connection_ids)

    write_revision(prototype_db, family)
    keep = set(connection_ids)
    for m in list(family.members):
        if m.connection_id not in keep:
            prototype_db.delete(m)  # audited by the revision just written
    existing = {m.connection_id for m in family.members}
    for cid in connection_ids:
        if cid not in existing:
            prototype_db.add(CodeFamilyMember(code_family_id=family.id, connection_id=cid))
    prototype_db.commit()
    prototype_db.refresh(family)
    return _serialize_one(prototype_db, family)


@router.post("/code_families/{family_id}/star")
def star_family(
    family_id: int, body: StarRequest, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    family = owned_family(prototype_db, family_id, user)
    if body.starred and family.status not in LIVE_FAMILY_STATUSES:
        raise HTTPException(409, f"code family is {family.status}")
    set_star(prototype_db, family, body.starred)
    prototype_db.commit()
    prototype_db.refresh(family)
    return _serialize_one(prototype_db, family)


@router.post("/code_families/merge")
def merge_user_families(
    body: CodeFamilyMergeRequest, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    family_ids = list(dict.fromkeys(body.family_ids))
    if len(family_ids) < 2:
        raise HTTPException(422, "pick at least two code families to merge")
    name = _require_text(body.name, "name")
    definition = _require_text(body.definition, "definition")
    families = [owned_family(prototype_db, fid, user) for fid in family_ids]
    stale = [f.id for f in families if f.status not in LIVE_FAMILY_STATUSES]
    if stale:
        raise HTTPException(409, f"code families no longer active: {stale}")
    if len({resolve_rq_id(prototype_db, f.research_question_id) for f in families}) > 1:
        raise HTTPException(409, "code families belong to different research questions")

    target_id = _merge_target(prototype_db, family_ids)
    target = next(f for f in families if f.id == target_id)
    merge_families(
        prototype_db,
        target=target,
        sources=[f for f in families if f.id != target_id],
        name=name,
        definition=definition,
    )
    prototype_db.commit()
    prototype_db.refresh(target)
    state = _state_for_rq(prototype_db, target.research_question_id)
    return {**state, "family": _serialize_one(prototype_db, target)}


@router.post("/code_families/{family_id}/accept")
def accept_family(
    family_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    family = owned_family(prototype_db, family_id, user)
    family.status = "accepted"
    prototype_db.commit()
    return {"id": family.id, "status": family.status}


@router.post("/code_families/{family_id}/dismiss")
def dismiss_family(
    family_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    family = owned_family(prototype_db, family_id, user)
    family.status = "dismissed"
    prototype_db.commit()
    return {"id": family.id, "status": family.status}


@router.post("/code_family_merges/{merge_id}/accept")
def accept_merge_suggestion(
    merge_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    suggestion = owned_merge_suggestion(prototype_db, merge_id, user)
    if suggestion.status != "suggested":
        raise HTTPException(409, f"merge suggestion is already {suggestion.status}")
    try:
        accept_merge(prototype_db, suggestion)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _state_for_rq(prototype_db, suggestion.research_question_id)


@router.post("/code_family_merges/{merge_id}/dismiss")
def dismiss_merge_suggestion(
    merge_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    suggestion = owned_merge_suggestion(prototype_db, merge_id, user)
    suggestion.status = "dismissed"
    prototype_db.commit()
    return {"id": suggestion.id, "status": suggestion.status}
