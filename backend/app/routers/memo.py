from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource, as_source
from ..db import get_prototype_db
from ..evidence_provenance import assigned_highlight_ids, evidence_row, user_highlight_spans
from ..evidence_text import evidence_conversations, quoted_texts
from ..models_prototype import (
    CodeFamily,
    CodeFamilyMember,
    InsightMerge,
    InsightMove,
    ReflectionConnection,
    ResearchQuestion,
    RLHighlight,
    SetAside,
    VoiceMemo,
)
from ..ownership import owned_connection
from ..schemas import InsightMergeRequest, MemoEntryUpdate, MemoReorderRequest
from ..services import activity, questions
from ..services.code_families import group_for, write_revision as write_family_revision
from ..services.insight_merges import (
    active_merges_by_target,
    merge_insights,
    serialize_merge,
    undo_insight_merge,
    write_connection_revision,
)
from ..timefmt import iso_utc
from ..services.rq_merges import active_merge_edges, resolve_rq_id, rq_group_ids

router = APIRouter(prefix="/memo", tags=["memo"])
insight_merges_router = APIRouter(prefix="/insight_merges", tags=["memo"])
config = get_config()

FILED_STATUSES = ("accepted", "edited")
SET_ASIDE_STATUSES = ("retracted", "rejected")


def filed_connections(prototype_db: Session, user_id: int) -> list[ReflectionConnection]:
    return (
        prototype_db.query(ReflectionConnection)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(ReflectionConnection.status.in_(FILED_STATUSES), VoiceMemo.user_id == user_id)
        .all()
    )


def starred_highlights(prototype_db: Session, user_id: int) -> list[RLHighlight]:
    return (
        prototype_db.query(RLHighlight)
        .filter(RLHighlight.user_id == user_id, RLHighlight.starred.is_(True))
        .all()
    )


def evidence_texts(
    cortico_db: Session, connections: list[ReflectionConnection], highlights: list[RLHighlight]
) -> dict[tuple[int, int, int], str]:
    """Quoted text for every evidence span of `connections` and every snippet span of `highlights`."""
    spans = [(e.snippet_id, e.start_index, e.end_index) for c in connections for e in c.evidence]
    spans += [(s.snippet_id, s.start_index, s.end_index) for h in highlights for s in h.snippets]
    return quoted_texts(cortico_db, spans)


def evidence_serializer(
    prototype_db: Session, connections: list[ReflectionConnection], text_by_span: dict, cortico_db
):
    """Returns a function that serializes one connection's evidence list, with each row's
    highlight star, quoted text and its own conversation. After an insight merge the
    evidence of one insight comes from several conversations, so the conversation is read
    from the snippet; the reflection's conversation is only the fallback for a snippet
    Cortico no longer shows."""
    cited = [e.highlight_id for c in connections for e in c.evidence if e.highlight_id is not None]
    starred_by_highlight = (
        {h.id: h.starred for h in prototype_db.query(RLHighlight).filter(RLHighlight.id.in_(cited))}
        if cited
        else {}
    )
    by_snippet = evidence_conversations(cortico_db, {e.snippet_id for c in connections for e in c.evidence})
    unknown_vm_ids = {c.voice_memo_id for c in connections if any(e.snippet_id not in by_snippet for e in c.evidence)}
    fallback = (
        {vm.id: vm.conversation_id for vm in prototype_db.query(VoiceMemo).filter(VoiceMemo.id.in_(unknown_vm_ids))}
        if unknown_vm_ids
        else {}
    )

    def serialize(c: ReflectionConnection) -> list[dict]:
        out = []
        for e in c.evidence:
            conversation_id, title = by_snippet.get(e.snippet_id, (fallback.get(c.voice_memo_id), None))
            out.append(
                {
                    "highlight_id": e.highlight_id,
                    "snippet_id": e.snippet_id,
                    "start_index": e.start_index,
                    "end_index": e.end_index,
                    "source": e.source,
                    "starred": starred_by_highlight.get(e.highlight_id, False),
                    "text": text_by_span.get((e.snippet_id, e.start_index, e.end_index), ""),
                    "conversation_id": conversation_id,
                    "conversation_title": title,
                }
            )
        return out

    return serialize


def starred_unassigned_groups(
    cortico_db: Session,
    connections: list[ReflectionConnection],
    highlights: list[RLHighlight],
    text_by_span: dict,
) -> list[dict]:
    """Starred highlights not cited by any of `connections` (pass every filed connection),
    grouped by conversation."""
    cited = assigned_highlight_ids(connections)
    groups: dict[int, dict] = {}
    for h in highlights:
        if h.id in cited:
            continue
        conv = as_source(cortico_db).conversations_by_ids({h.conversation_id}).get(h.conversation_id)
        group = groups.setdefault(
            h.conversation_id,
            {"conversation_id": h.conversation_id, "conversation_title": conv.title if conv else None, "highlights": []},
        )
        group["highlights"].append(
            {
                "highlight_id": h.id,
                "notes": h.notes,
                "snippets": [
                    {
                        "snippet_id": s.snippet_id,
                        "start_index": s.start_index,
                        "end_index": s.end_index,
                        "text": text_by_span.get((s.snippet_id, s.start_index, s.end_index), ""),
                    }
                    for s in h.snippets
                ],
            }
        )
    return list(groups.values())


def _connection_refs(prototype_db: Session, connection: ReflectionConnection) -> dict:
    vm = prototype_db.get(VoiceMemo, connection.voice_memo_id)
    return {
        "connection_id": connection.id,
        "voice_memo_id": connection.voice_memo_id,
        "conversation_id": vm.conversation_id if vm else None,
        "research_question_id": connection.research_question_id,
    }


_write_revision = write_connection_revision


@router.get("")
def get_memo(
    cortico_db: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    connections = filed_connections(prototype_db, user.id)

    voice_memo_ids = {c.voice_memo_id for c in connections}
    voice_memos = {
        vm.id: vm for vm in prototype_db.query(VoiceMemo).filter(VoiceMemo.id.in_(voice_memo_ids))
    } if voice_memo_ids else {}

    conv_ids = {vm.conversation_id for vm in voice_memos.values()}
    conversations = (
        as_source(cortico_db).conversations_by_ids(conv_ids)
        if conv_ids
        else {}
    )

    research_questions = (
        prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user.id)
        .order_by(ResearchQuestion.display_order)
        .all()
    )

    all_starred_highlights = starred_highlights(prototype_db, user.id)
    text_by_span = evidence_texts(cortico_db, connections, all_starred_highlights)
    serialize_evidence = evidence_serializer(prototype_db, connections, text_by_span, cortico_db)
    merges_by_target = active_merges_by_target(prototype_db, [c.id for c in connections])
    merged_source_ids = {m.source_connection_id for merges in merges_by_target.values() for m in merges}
    merged_text = (
        {
            c.id: c.insight_text
            for c in prototype_db.query(ReflectionConnection).filter(ReflectionConnection.id.in_(merged_source_ids))
        }
        if merged_source_ids
        else {}
    )

    # A merged RQ has no section of its own; its entries read under its final target.
    edges = active_merge_edges(prototype_db)
    research_questions = [rq for rq in research_questions if rq.id not in edges]
    by_rq: dict[int | None, list[ReflectionConnection]] = {rq.id: [] for rq in research_questions}
    unmapped: list[ReflectionConnection] = []
    for c in connections:
        rq_id = (
            resolve_rq_id(prototype_db, c.research_question_id, edges)
            if c.research_question_id is not None
            else None
        )
        if rq_id is not None and rq_id in by_rq:
            by_rq[rq_id].append(c)
        else:
            unmapped.append(c)

    def entry(c: ReflectionConnection) -> dict:
        vm = voice_memos.get(c.voice_memo_id)
        conv = conversations.get(vm.conversation_id) if vm else None
        ai_metadata = json.loads(c.ai_metadata) if c.ai_metadata else None
        evidence = serialize_evidence(c)
        return {
            "connection_id": c.id,
            # As stored: null for "doesn't fit a question yet", and a merged question's own
            # id for an entry that reads under the question it was merged into.
            "research_question_id": c.research_question_id,
            # The reflection's conversation first, then any other its evidence comes from.
            "conversation_ids": list(
                dict.fromkeys(
                    cid
                    for cid in [vm.conversation_id if vm else None] + [e["conversation_id"] for e in evidence]
                    if cid is not None
                )
            ),
            # Insights merged into this one, newest last; each can be undone.
            "merged_from": [
                {
                    "merge_id": m.id,
                    "connection_id": m.source_connection_id,
                    "insight_text": merged_text.get(m.source_connection_id, ""),
                    "merged_at": iso_utc(m.created_at),
                }
                for m in merges_by_target.get(c.id, [])
            ],
            "insight_text": c.insight_text,
            "source": c.source,
            "edited_from_ai": c.edited_from_ai,
            "conversation_id": conv.id if conv else None,
            "conversation_title": conv.title if conv else None,
            "voice_memo_id": c.voice_memo_id,
            "voice_memo_audio_path": (vm.audio_path or None) if vm else None,
            "voice_memo_typed": (not vm.audio_path) if vm else None,
            # Where in the voice memo's own recording this insight was drawn from, if the
            # AI located it — lets the memo page link straight to that moment, not just
            # the start of the file. None for manually-added connections or older ones
            # created before this field existed.
            "reflection_timestamp": ai_metadata.get("reflection_timestamp") if ai_metadata else None,
            "filed_at": c.updated_at.isoformat(),
            "evidence": evidence,
        }

    def ordered(conns: list[ReflectionConnection]) -> list[ReflectionConnection]:
        return sorted(conns, key=lambda c: (c.memo_order if c.memo_order is not None else 1 << 30, c.created_at))

    sections = [
        {
            "research_question_id": rq.id,
            "name": rq.name,
            "archived": rq.archived,
            "merged_from": rq_group_ids(prototype_db, rq.id, edges)[1:],
            "entries": [entry(c) for c in ordered(by_rq[rq.id])],
        }
        for rq in research_questions
    ]

    return {
        "sections": sections,
        "unmapped": [entry(c) for c in ordered(unmapped)],
        "starred_unassigned": starred_unassigned_groups(cortico_db, connections, all_starred_highlights, text_by_span),
    }


@router.patch("/connections/{connection_id}")
def edit_memo_entry(
    connection_id: int, body: MemoEntryUpdate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Edits a filed entry's text or evidence, and/or moves it to another research question.

    A body that names `research_question_id` (null included: "doesn't fit a question yet")
    is a move. A move alone changes neither the status nor the source: the insight's words
    are untouched. Every call writes a revision first; a move is also recorded in
    rl_insight_move, because the revision has no column for the question."""
    connection = owned_connection(prototype_db, connection_id, user)
    if connection is None or connection.status not in FILED_STATUSES:
        raise HTTPException(404, "filed memo entry not found")

    moving = "research_question_id" in body.model_fields_set
    new_rq_id: int | None = None
    if moving:
        if body.research_question_id is not None:
            new_rq_id = questions.checked_question_ids(prototype_db, user.id, [body.research_question_id])[0]
        current = (
            resolve_rq_id(prototype_db, connection.research_question_id)
            if connection.research_question_id is not None
            else None
        )
        moving = new_rq_id != current
    editing = body.insight_text is not None or body.evidence is not None or not moving

    _write_revision(prototype_db, connection)

    if editing:
        if connection.source == "ai":
            connection.source = "user"
            connection.edited_from_ai = True
        if body.insight_text is not None:
            connection.insight_text = body.insight_text
        if body.evidence is not None:
            for e in list(connection.evidence):
                prototype_db.delete(e)
            vm = prototype_db.get(VoiceMemo, connection.voice_memo_id)
            # By snippet, not by the reflection's conversation: a merged insight's evidence
            # comes from several conversations.
            spans = user_highlight_spans(prototype_db, vm.user_id, {ev.snippet_id for ev in body.evidence})
            for ev in body.evidence:
                prototype_db.add(evidence_row(connection.id, ev, spans, vm, prototype_db))
        connection.status = "edited"
        activity.log_activity(
            prototype_db, user.id, "user", activity.MEMO_ENTRY_EDITED,
            f"You edited a memo entry: \u201c{activity.excerpt(connection.insight_text)}\u201d",
            **_connection_refs(prototype_db, connection),
        )

    removed_from_families: list[int] = []
    if moving:
        previous_rq_id = connection.research_question_id
        prototype_db.add(
            InsightMove(
                connection_id=connection.id, user_id=user.id,
                from_research_question_id=previous_rq_id, to_research_question_id=new_rq_id,
            )
        )
        connection.research_question_id = new_rq_id
        connection.memo_order = None  # it joins the end of its new section
        # A code family holds entries of one question's group only.
        new_group = set(group_for(prototype_db, new_rq_id)) if new_rq_id is not None else set()
        memberships = (
            prototype_db.query(CodeFamilyMember, CodeFamily)
            .join(CodeFamily, CodeFamily.id == CodeFamilyMember.code_family_id)
            .filter(CodeFamilyMember.connection_id == connection.id)
            .all()
        )
        for member, family in memberships:
            if family.research_question_id not in new_group:
                write_family_revision(prototype_db, family)
                prototype_db.delete(member)
                removed_from_families.append(family.id)
        new_name = prototype_db.get(ResearchQuestion, new_rq_id).name if new_rq_id is not None else None
        activity.log_activity(
            prototype_db, user.id, "user", activity.MEMO_ENTRY_MOVED,
            f"You moved an insight to \u201c{activity.excerpt(new_name, 60)}\u201d: "
            f"\u201c{activity.excerpt(connection.insight_text, 60)}\u201d"
            if new_name is not None
            else f"You moved an insight out of its question: \u201c{activity.excerpt(connection.insight_text, 60)}\u201d",
            previous_research_question_id=previous_rq_id, **_connection_refs(prototype_db, connection),
        )
    prototype_db.commit()
    return {
        "id": connection.id,
        "status": connection.status,
        "research_question_id": connection.research_question_id,
        "moved": moving,
        "removed_from_family_ids": removed_from_families,
    }


@router.post("/connections/{connection_id}/merge_into")
def merge_memo_entry_into(
    connection_id: int, body: InsightMergeRequest, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Merges this filed insight into another of the user's. The target gains its evidence
    (identical spans once) and this one's status becomes "merged". 409 unless both are filed."""
    source = owned_connection(prototype_db, connection_id, user)
    target = owned_connection(prototype_db, body.target_connection_id, user)
    if source is None or target is None:
        raise HTTPException(404, "memo entry not found")
    try:
        merge = merge_insights(prototype_db, user_id=user.id, source=source, target=target)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _merge_response(prototype_db, merge)


def _merge_response(prototype_db: Session, merge: InsightMerge) -> dict:
    source = prototype_db.get(ReflectionConnection, merge.source_connection_id)
    target = prototype_db.get(ReflectionConnection, merge.target_connection_id)
    return {
        "merge": serialize_merge(merge),
        "source": {"id": source.id, "status": source.status},
        "target": {"id": target.id, "status": target.status, "evidence_count": len(target.evidence)},
    }


@insight_merges_router.post("/{merge_id}/undo")
def undo_merge_of_insights(
    merge_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    merge = prototype_db.get(InsightMerge, merge_id)
    if merge is None or merge.user_id != user.id:
        raise HTTPException(404, "insight merge not found")
    try:
        undo_insight_merge(prototype_db, merge)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _merge_response(prototype_db, merge)


@router.post("/connections/{connection_id}/retract")
def retract_memo_entry(
    connection_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    connection = owned_connection(prototype_db, connection_id, user)
    if connection is None or connection.status not in FILED_STATUSES:
        raise HTTPException(404, "filed memo entry not found")
    prototype_db.add(
        SetAside(
            connection_id=connection.id, user_id=user.id, kind="retracted", previous_status=connection.status
        )
    )
    activity.log_activity(
        prototype_db, user.id, "user", activity.MEMO_ENTRY_RETRACTED,
        f"You took an entry out of the memo: \u201c{activity.excerpt(connection.insight_text)}\u201d",
        **_connection_refs(prototype_db, connection),
    )
    connection.status = "retracted"
    prototype_db.commit()
    return {"id": connection.id, "status": connection.status}


def _open_set_aside(prototype_db: Session, connection_id: int, kind: str) -> SetAside | None:
    """The latest set-aside row of this kind that hasn't been restored. None for a
    connection set aside before rl_set_aside existed."""
    return (
        prototype_db.query(SetAside)
        .filter(SetAside.connection_id == connection_id, SetAside.kind == kind, SetAside.restored_at.is_(None))
        .order_by(SetAside.id.desc())
        .first()
    )


def _filed_status_before_retraction(connection: ReflectionConnection, record: SetAside | None) -> str:
    if record is not None and record.previous_status in FILED_STATUSES:
        return record.previous_status
    # Retracted before rl_set_aside existed. Every edit writes a revision first, so an entry
    # with a revision (or one edited from an AI proposal) was "edited".
    return "edited" if connection.edited_from_ai or connection.revisions else "accepted"


@router.get("/set_aside")
def list_set_aside(
    cortico_db: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """The user's connections that were retracted from the memo or rejected at review,
    newest first."""
    rows = (
        prototype_db.query(ReflectionConnection, VoiceMemo)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(ReflectionConnection.status.in_(SET_ASIDE_STATUSES), VoiceMemo.user_id == user.id)
        .all()
    )
    connection_ids = [c.id for c, _ in rows]
    records: dict[tuple[int, str], SetAside] = {}
    if connection_ids:
        query = (
            prototype_db.query(SetAside)
            .filter(SetAside.connection_id.in_(connection_ids), SetAside.restored_at.is_(None))
            .order_by(SetAside.id)
        )
        for record in query:
            records[(record.connection_id, record.kind)] = record  # the latest wins
    conversations = as_source(cortico_db).conversations_by_ids({vm.conversation_id for _, vm in rows})
    edges = active_merge_edges(prototype_db)
    rq_names = {
        rq.id: rq.name for rq in prototype_db.query(ResearchQuestion).filter(ResearchQuestion.user_id == user.id)
    }

    def item(c: ReflectionConnection, vm: VoiceMemo) -> dict:
        record = records.get((c.id, c.status))
        conv = conversations.get(vm.conversation_id)
        rq_id = resolve_rq_id(prototype_db, c.research_question_id, edges) if c.research_question_id is not None else None
        return {
            "connection_id": c.id,
            "kind": c.status,
            "insight_text": c.insight_text,
            "source": c.source,
            "research_question_id": rq_id,
            "research_question_name": rq_names.get(rq_id),
            "conversation_id": vm.conversation_id,
            "conversation_title": conv.title if conv else None,
            "voice_memo_id": vm.id,
            # Recorded when it happened, or, for rows set aside before that was recorded,
            # the connection's last change (setting the status was that change).
            "set_aside_at": iso_utc(record.set_aside_at if record else c.updated_at),
            "set_aside_at_source": "recorded" if record else "last_changed",
            "restores_to": _filed_status_before_retraction(c, record) if c.status == "retracted" else "suggested",
        }

    items = [item(c, vm) for c, vm in rows]
    items.sort(key=lambda i: (i["set_aside_at"] or "", i["connection_id"]), reverse=True)
    return {"items": items}


@router.post("/connections/{connection_id}/restore")
def restore_connection(
    connection_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    """Brings back a set-aside connection.

    A retracted entry had been filed, so it goes back into the memo with the filed status it
    had. This is the one deliberate exception to "only file_to_memo sets accepted or edited".

    A connection rejected at review never reached the memo, so it does not go there: it
    returns to `suggested`, shows as undecided in its reflection's review again, and is filed
    through file_to_memo like any other."""
    connection = owned_connection(prototype_db, connection_id, user)
    if connection is None:
        raise HTTPException(404, "connection not found")
    if connection.status not in SET_ASIDE_STATUSES:
        raise HTTPException(409, f"this connection is {connection.status}, not set aside")

    restored_from = connection.status
    record = _open_set_aside(prototype_db, connection.id, restored_from)
    refs = _connection_refs(prototype_db, connection)
    if restored_from == "retracted":
        filed_status = _filed_status_before_retraction(connection, record)
        _write_revision(prototype_db, connection)
        connection.status = filed_status
        activity.log_activity(
            prototype_db, user.id, "user", activity.MEMO_ENTRY_RESTORED,
            f"You put an entry back in the memo: \u201c{activity.excerpt(connection.insight_text)}\u201d",
            **refs,
        )
    else:
        connection.status = "suggested"
        activity.log_activity(
            prototype_db, user.id, "user", activity.INSIGHT_RETURNED_TO_REVIEW,
            f"You sent an insight back to review: \u201c{activity.excerpt(connection.insight_text)}\u201d",
            **refs,
        )
    if record is not None:
        record.restored_at = datetime.now(timezone.utc)
    prototype_db.commit()
    return {
        "id": connection.id,
        "status": connection.status,
        "restored_from": restored_from,
        "voice_memo_id": connection.voice_memo_id,
        "in_memo": connection.status in FILED_STATUSES,
    }


@router.put("/reorder")
def reorder_memo_entries(
    body: MemoReorderRequest, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    group = set(rq_group_ids(prototype_db, body.research_question_id))
    for order, connection_id in enumerate(body.ordered_connection_ids):
        c = owned_connection(prototype_db, connection_id, user)
        if c is not None and c.research_question_id in group:
            c.memo_order = order
    prototype_db.commit()
    return {"reordered": True}
