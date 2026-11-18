"""Two things the user can send the AI to do for one research question: find more passages
(services/find_passages.py) and say what is missing (services/question_gaps.py)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, current_user, get_source, workspace_project_ids
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..models_prototype import NextStepSuggestion, PassageSuggestion, QuestionGap, ResearchQuestion
from ..ownership import owned_rq
from ..schemas import FindPassagesRequest, GapListenNextRequest, HighlightCreate, HighlightSnippetIn
from ..services import activity, questions
from ..services.code_families import group_for
from ..services.find_passages import find_passages, recently_opened
from ..services.highlights import create_highlight_for_user
from ..services.question_gaps import add_to_listen_next, generate_gaps
from ..services.rq_merges import resolve_rq_id
from ..timefmt import iso_utc
from .highlights import serialize_highlight
from .next_steps import _serialize as serialize_next_step

router = APIRouter(tags=["question_levers"])
config = get_config()


def _project_ids(prototype_db: Session, user: CurrentUser) -> list[int]:
    return workspace_project_ids(active_workspace(prototype_db, user))


# --- find more passages -------------------------------------------------------------------


def _owned_passage(prototype_db: Session, suggestion_id: int, user: CurrentUser) -> PassageSuggestion:
    s = prototype_db.get(PassageSuggestion, suggestion_id)
    if s is None or s.user_id != user.id:
        raise HTTPException(404, "passage suggestion not found")
    return s


def _serialize_passages(prototype_db: Session, source, suggestions: list[PassageSuggestion]) -> list[dict]:
    snippets = source.snippets_by_ids({s.snippet_id for s in suggestions})
    conversations = source.conversations_by_ids({s.conversation_id for s in suggestions})

    def one(s: PassageSuggestion) -> dict:
        snippet = snippets.get(s.snippet_id)
        words = snippet.words[s.start_index : s.end_index] if snippet else []
        conv = conversations.get(s.conversation_id)
        first_word_start = words[0].get("start") if words else None
        return {
            "id": s.id,
            "research_question_id": resolve_rq_id(prototype_db, s.research_question_id),
            "conversation_id": s.conversation_id,
            "conversation_title": conv.title if conv else None,
            "snippet_id": s.snippet_id,
            "start_index": s.start_index,
            "end_index": s.end_index,
            "speaker_name": snippet.speaker_name if snippet else None,
            "is_facilitator": snippet.is_facilitator if snippet else None,
            # Seconds into the conversation: the passage's first word when Cortico timed it,
            # else the start of its snippet.
            "time_seconds": first_word_start
            if first_word_start is not None
            else (snippet.start_offset if snippet else None),
            "snippet_start_offset": snippet.start_offset if snippet else None,
            "text": " ".join(w["word"] for w in words),
            "reason": s.reason,
            "status": s.status,
            "highlight_id": s.highlight_id,
            "ai_metadata": json.loads(s.ai_metadata) if s.ai_metadata else None,
            "created_at": iso_utc(s.created_at),
        }

    return [one(s) for s in suggestions]


@router.post("/research_questions/{rq_id}/find_passages")
def run_find_passages(
    rq_id: int,
    body: FindPassagesRequest | None = None,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Paid: one model call per conversation searched. With `conversation_id`, that
    conversation; without, the conversations the user opened most recently, up to
    config.find_passages.max_conversations_per_run."""
    owned_rq(prototype_db, rq_id, user)
    questions.checked_question_ids(prototype_db, user.id, [rq_id])  # 409 on an archived question
    project_ids = _project_ids(prototype_db, user)
    if body is not None and body.conversation_id is not None:
        conversation = source.conversation(body.conversation_id)
        if conversation is None:
            raise HTTPException(404, "conversation not found")
        conversations = [conversation]
    else:
        conversations = recently_opened(prototype_db, source, user.id, project_ids)
    result = find_passages(
        prototype_db=prototype_db, cortico_db=source, config=config, user_id=user.id,
        research_question_id=rq_id, conversations=conversations,
    )
    return {
        "research_question_id": result.research_question_id,
        "searched_conversation_ids": result.searched_conversation_ids,
        "failed": result.failed,
        "suggestions": _serialize_passages(prototype_db, source, result.suggestions),
    }


@router.get("/research_questions/{rq_id}/passage_suggestions")
def list_passage_suggestions(
    rq_id: int,
    conversation_id: int | None = None,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """The undecided passage suggestions for this question's merged group, newest first.
    `conversation_id` narrows the list to one conversation (the notebook)."""
    owned_rq(prototype_db, rq_id, user)
    query = prototype_db.query(PassageSuggestion).filter(
        PassageSuggestion.user_id == user.id,
        PassageSuggestion.status == "suggested",
        PassageSuggestion.research_question_id.in_(group_for(prototype_db, rq_id)),
    )
    if conversation_id is not None:
        query = query.filter(PassageSuggestion.conversation_id == conversation_id)
    suggestions = query.order_by(PassageSuggestion.id.desc()).all()
    return {
        "research_question_id": resolve_rq_id(prototype_db, rq_id),
        "suggestions": _serialize_passages(prototype_db, source, suggestions),
    }


@router.post("/passage_suggestions/{suggestion_id}/accept")
def accept_passage(
    suggestion_id: int,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Makes the passage a real highlight of the user's, through the same function as
    POST /conversations/{id}/highlights (so the project id and the Cortico write-back are the
    same), and files it under the question. 409 once it has been accepted or dismissed."""
    suggestion = _owned_passage(prototype_db, suggestion_id, user)
    if suggestion.status != "suggested":
        raise HTTPException(409, f"this passage was already {suggestion.status}")
    (rq_id,) = questions.checked_question_ids(prototype_db, user.id, [suggestion.research_question_id])

    highlight, sync = create_highlight_for_user(
        prototype_db, source, user.id, suggestion.conversation_id,
        HighlightCreate(
            snippets=[
                HighlightSnippetIn(
                    snippet_id=suggestion.snippet_id,
                    start_index=suggestion.start_index,
                    end_index=suggestion.end_index,
                )
            ]
        ),
    )
    questions.add_highlight_question(prototype_db, highlight, rq_id)
    suggestion.status = "accepted"
    suggestion.highlight_id = highlight.id
    activity.log_activity(
        prototype_db, user.id, "user", activity.PASSAGE_ACCEPTED,
        "You kept a passage the AI found and filed it under a question.",
        passage_suggestion_id=suggestion.id, highlight_id=highlight.id, research_question_id=rq_id,
        conversation_id=suggestion.conversation_id,
    )
    prototype_db.commit()
    prototype_db.refresh(highlight)
    return {
        "suggestion": _serialize_passages(prototype_db, source, [suggestion])[0],
        "highlight": serialize_highlight(prototype_db, highlight, sync),
    }


@router.post("/passage_suggestions/{suggestion_id}/dismiss")
def dismiss_passage(
    suggestion_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    suggestion = _owned_passage(prototype_db, suggestion_id, user)
    if suggestion.status == "accepted":
        raise HTTPException(409, "this passage was already accepted; delete the highlight instead")
    if suggestion.status != "dismissed":
        activity.log_activity(
            prototype_db, user.id, "user", activity.PASSAGE_DISMISSED, "You set aside a passage the AI found.",
            passage_suggestion_id=suggestion.id, research_question_id=suggestion.research_question_id,
            conversation_id=suggestion.conversation_id,
        )
    suggestion.status = "dismissed"
    prototype_db.commit()
    return {"id": suggestion.id, "status": suggestion.status}


# --- what am I missing ----------------------------------------------------------------------


def _owned_gap(prototype_db: Session, gap_id: int, user: CurrentUser) -> QuestionGap:
    gap = prototype_db.get(QuestionGap, gap_id)
    if gap is None or gap.user_id != user.id:
        raise HTTPException(404, "observation not found")
    return gap


def _serialize_gaps(prototype_db: Session, source, user: CurrentUser, gaps: list[QuestionGap]) -> list[dict]:
    named = {cid for g in gaps for cid in json.loads(g.conversation_ids_json)}
    conversations = source.conversations_by_ids(named)
    listed: dict[int, int] = {}
    if named:
        rows = (
            prototype_db.query(NextStepSuggestion)
            .filter(
                NextStepSuggestion.user_id == user.id,
                NextStepSuggestion.status != "dismissed",
                NextStepSuggestion.recommended_conversation_id.in_(named),
            )
            .order_by(NextStepSuggestion.id)
        )
        for row in rows:
            listed.setdefault(row.recommended_conversation_id, row.id)

    def one(g: QuestionGap) -> dict:
        return {
            "id": g.id,
            "research_question_id": resolve_rq_id(prototype_db, g.research_question_id),
            "kind": g.gap_kind,
            "observation": g.observation,
            "conversations": [
                {
                    "conversation_id": cid,
                    "title": conversations[cid].title if cid in conversations else None,
                    "in_listen_next": cid in listed,
                    "next_step_id": listed.get(cid),
                }
                for cid in json.loads(g.conversation_ids_json)
            ],
            "status": g.status,
            "ai_metadata": json.loads(g.ai_metadata) if g.ai_metadata else None,
            "created_at": iso_utc(g.created_at),
        }

    return [one(g) for g in gaps]


@router.post("/research_questions/{rq_id}/whats_missing")
def run_whats_missing(
    rq_id: int,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Paid: one model call. Returns the observations this run created (possibly none)."""
    owned_rq(prototype_db, rq_id, user)
    questions.checked_question_ids(prototype_db, user.id, [rq_id])  # 409 on an archived question
    created = generate_gaps(
        prototype_db=prototype_db, cortico_db=source, config=config, user_id=user.id,
        research_question_id=rq_id, project_ids=_project_ids(prototype_db, user),
    )
    return {
        "research_question_id": resolve_rq_id(prototype_db, rq_id),
        "gaps": _serialize_gaps(prototype_db, source, user, created),
    }


@router.get("/research_questions/{rq_id}/gaps")
def list_gaps(
    rq_id: int,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """The observations the user has not dismissed, for this question's merged group, newest
    first."""
    owned_rq(prototype_db, rq_id, user)
    gaps = (
        prototype_db.query(QuestionGap)
        .filter(
            QuestionGap.user_id == user.id,
            QuestionGap.status == "suggested",
            QuestionGap.research_question_id.in_(group_for(prototype_db, rq_id)),
        )
        .order_by(QuestionGap.id.desc())
        .all()
    )
    return {
        "research_question_id": resolve_rq_id(prototype_db, rq_id),
        "gaps": _serialize_gaps(prototype_db, source, user, gaps),
    }


@router.post("/question_gaps/{gap_id}/dismiss")
def dismiss_gap(
    gap_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    gap = _owned_gap(prototype_db, gap_id, user)
    if gap.status != "dismissed":
        activity.log_activity(
            prototype_db, user.id, "user", activity.QUESTION_GAP_DISMISSED,
            f"You set aside an observation: “{activity.excerpt(gap.observation)}”",
            question_gap_id=gap.id, research_question_id=gap.research_question_id,
        )
    gap.status = "dismissed"
    prototype_db.commit()
    return {"id": gap.id, "status": gap.status}


@router.post("/question_gaps/{gap_id}/listen_next")
def gap_listen_next(
    gap_id: int,
    body: GapListenNextRequest | None = None,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Adds the conversations this observation names (or the subset in the body) to the
    user's Listen next list, as next-step rows. A conversation already on the list is not
    added twice. 409 for a dismissed observation or one that names no conversation."""
    gap = _owned_gap(prototype_db, gap_id, user)
    if gap.status == "dismissed":
        raise HTTPException(409, "this observation was dismissed")
    named = json.loads(gap.conversation_ids_json)
    chosen = named
    if body is not None and body.conversation_ids is not None:
        outside = [cid for cid in body.conversation_ids if cid not in named]
        if outside:
            raise HTTPException(422, f"this observation doesn't name conversations {outside}")
        chosen = list(dict.fromkeys(body.conversation_ids))
    conversations = source.conversations_by_ids(set(chosen))
    chosen = [cid for cid in chosen if cid in conversations]  # still visible to the user
    if not chosen:
        raise HTTPException(409, "this observation names no conversation to listen to")

    rq = prototype_db.get(ResearchQuestion, resolve_rq_id(prototype_db, gap.research_question_id))
    rows = add_to_listen_next(
        prototype_db, user_id=user.id, gap=gap, conversation_ids=chosen, question_name=rq.name if rq else None
    )
    added = [row for row, created in rows if created]
    if added:
        activity.log_activity(
            prototype_db, user.id, "user", activity.QUESTION_GAP_LISTEN_NEXT,
            f"You added {activity.plural(len(added), 'conversation')} to Listen next.",
            question_gap_id=gap.id, research_question_id=gap.research_question_id,
            conversation_ids=[row.recommended_conversation_id for row in added],
            next_step_ids=[row.id for row in added],
        )
    prototype_db.commit()
    titles = {cid: c.title for cid, c in conversations.items()}
    return {
        "gap": _serialize_gaps(prototype_db, source, user, [gap])[0],
        "next_steps": [{**serialize_next_step(row, titles), "created": created} for row, created in rows],
    }
