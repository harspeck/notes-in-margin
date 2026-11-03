"""Research-question merges. Merging archives the source RQ, and every RQ-scoped read
(memo sections, code family lists, the cap, synthesis input, the Organize page) then
treats the target and everything merged into it as one group. Merges are logged in
rl_rq_merge and undone by status, never deleted.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models_prototype import CodeFamily, ReflectionConnection, ResearchQuestion, RQMerge


def active_merge_edges(session: Session) -> dict[int, int]:
    """source RQ id -> the RQ it is directly merged into, for every active merge."""
    return {
        m.source_rq_id: m.target_rq_id
        for m in session.query(RQMerge).filter(RQMerge.status == "active").order_by(RQMerge.id)
    }


def resolve_rq_id(session: Session, rq_id: int, edges: dict[int, int] | None = None) -> int:
    """Follows active merges to the RQ that finally holds this one's entries."""
    edges = active_merge_edges(session) if edges is None else edges
    seen = {rq_id}
    current = rq_id
    while current in edges:
        nxt = edges[current]
        if nxt in seen:  # refused on write, but a cycle in the data must not hang a read
            break
        seen.add(nxt)
        current = nxt
    return current


def rq_group_ids(session: Session, target_id: int, edges: dict[int, int] | None = None) -> list[int]:
    """The target plus every RQ merged into it, transitively. Target first."""
    edges = active_merge_edges(session) if edges is None else edges
    sources_of: dict[int, list[int]] = {}
    for source, target in edges.items():
        sources_of.setdefault(target, []).append(source)
    group = [target_id]
    seen = {target_id}
    frontier = [target_id]
    while frontier:
        nxt = []
        for rq_id in frontier:
            for source in sources_of.get(rq_id, []):
                if source not in seen:
                    seen.add(source)
                    group.append(source)
                    nxt.append(source)
        frontier = nxt
    return group


def active_merges_into(session: Session, target_id: int) -> list[RQMerge]:
    """Active merges whose source lies anywhere in target_id's group, in merge order."""
    edges = active_merge_edges(session)
    sources = set(rq_group_ids(session, target_id, edges)) - {target_id}
    if not sources:
        return []
    return (
        session.query(RQMerge)
        .filter(RQMerge.status == "active", RQMerge.source_rq_id.in_(sources))
        .order_by(RQMerge.id)
        .all()
    )


def merge_rq(session: Session, *, user_id: int, source: ResearchQuestion, target: ResearchQuestion) -> RQMerge:
    """Raises ValueError (the router's 409) for a self-merge, an archived or merged source
    or target, or a merge that would close a cycle."""
    edges = active_merge_edges(session)
    if source.id == target.id:
        raise ValueError("a research question can't be merged into itself")
    if source.id in edges:
        raise ValueError("this research question is already merged into another")
    if target.id in edges:
        raise ValueError("the target research question is itself merged into another")
    if target.archived:
        raise ValueError("the target research question is archived")
    if source.archived:
        # undo un-archives the source, so merging an archived one would lose that state
        raise ValueError("unarchive this research question before merging it")
    if source.id == resolve_rq_id(session, target.id, edges) or target.id in rq_group_ids(session, source.id, edges):
        raise ValueError("this merge would create a cycle")

    merge = RQMerge(user_id=user_id, source_rq_id=source.id, target_rq_id=target.id, status="active")
    session.add(merge)
    source.archived = True
    session.commit()
    session.refresh(merge)
    return merge


def undo_merge(session: Session, merge: RQMerge) -> list[CodeFamily]:
    """Un-archives the source and returns the live families that stay with the target's
    group although they hold entries from the source's group (families the user merged or
    built across the two while they were merged). Raises ValueError if already undone."""
    from .code_families import LIVE_FAMILY_STATUSES  # code_families imports this module

    if merge.status != "active":
        raise ValueError(f"this merge is already {merge.status}")
    source_group = set(rq_group_ids(session, merge.source_rq_id))

    merge.status = "undone"
    merge.undone_at = datetime.now(timezone.utc)
    source = session.get(ResearchQuestion, merge.source_rq_id)
    source.archived = False
    session.flush()

    target_group = rq_group_ids(session, resolve_rq_id(session, merge.target_rq_id))
    families = (
        session.query(CodeFamily)
        .filter(CodeFamily.research_question_id.in_(target_group), CodeFamily.status.in_(LIVE_FAMILY_STATUSES))
        .order_by(CodeFamily.created_at)
        .all()
    )
    member_ids = {m.connection_id for f in families for m in f.members}
    rq_by_connection = (
        {
            c.id: c.research_question_id
            for c in session.query(ReflectionConnection).filter(ReflectionConnection.id.in_(member_ids))
        }
        if member_ids
        else {}
    )
    kept = [
        f for f in families if any(rq_by_connection.get(m.connection_id) in source_group for m in f.members)
    ]
    session.commit()
    return kept
