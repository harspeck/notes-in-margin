"""Creating a highlight. One code path for the reader (POST /conversations/{id}/highlights)
and for an accepted passage suggestion, so the project id and the Cortico write-back behave
the same for both."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import get_config
from ..cortico.source import CorticoSource
from ..cortico.write_back import push_highlight
from ..models_prototype import HighlightSnippet, RLHighlight
from ..schemas import HighlightCreate


def create_highlight_for_user(
    prototype_db: Session, source: CorticoSource, user_id: int, conversation_id: int, body: HighlightCreate
) -> tuple[RLHighlight, dict | None]:
    """Saves the highlight (committing), then in cortico_api mode pushes it to Cortico.
    Returns the highlight and its Cortico sync state (None when write-back is off). 404 for
    a conversation the source doesn't show; 409 for one outside the workspace's projects."""
    config = get_config()
    conversation = source.conversation(conversation_id)
    if conversation is None:
        raise HTTPException(404, "conversation not found")
    if conversation.project_id is None:
        raise HTTPException(409, "This conversation isn't in one of your workspace's projects.")

    # Snippet text is served from a cache shared by every user, so a snippet from another
    # conversation would hand back a transcript this user may not be able to open.
    in_conversation = {s.id for s in source.snippets(conversation_id)}
    foreign = sorted({s.snippet_id for s in body.snippets} - in_conversation)
    if foreign:
        raise HTTPException(400, f"snippets {foreign} aren't in this conversation's transcript")

    highlight = RLHighlight(
        user_id=user_id,
        conversation_id=conversation_id,
        project_id=conversation.project_id,
        title=body.title,
        notes=body.notes,
    )
    prototype_db.add(highlight)
    prototype_db.flush()
    for snip in body.snippets:
        prototype_db.add(
            HighlightSnippet(
                highlight_id=highlight.id,
                snippet_id=snip.snippet_id,
                start_index=snip.start_index,
                end_index=snip.end_index,
            )
        )
    prototype_db.commit()
    prototype_db.refresh(highlight)
    sync = None
    if source.kind == "cortico_api" and config.cortico.write_back.highlights:
        row = push_highlight(prototype_db, source, highlight, conversation, tool_id=config.cortico.tool_id)
        sync = {"status": row.status, "cortico_id": row.cortico_id, "error": row.error}
    return highlight, sync
