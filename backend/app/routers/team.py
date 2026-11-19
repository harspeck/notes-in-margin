"""The Team Page and team codebook routes. Every one builds its view with `team.load_view`
and reads only through `services/team.py`, which holds the access rule."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..schemas import (
    TeamGroupQuestionAdd,
    TeamQuestionGroupCreate,
    TeamQuestionGroupUpdate,
    TeamThemeCreate,
    TeamThemeMembersUpdate,
    TeamThemeUpdate,
)
from ..services import team
from .research_questions import serialize_rqs

router = APIRouter(tags=["team"])
config = get_config()


def _view(
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
) -> team.TeamView:
    return team.load_view(prototype_db, source, user)


@router.get("/team")
def get_team(view: team.TeamView = Depends(_view)):
    return team.overview(view)


@router.get("/team/questions/{rq_id}")
def get_team_question(rq_id: int, view: team.TeamView = Depends(_view)):
    return team.question_detail(view, rq_id)


@router.get("/team/people/{user_id}")
def get_team_person(user_id: int, view: team.TeamView = Depends(_view)):
    return team.person_detail(view, user_id)


@router.get("/conversations/{conversation_id}/team_highlights")
def list_team_highlights(conversation_id: int, view: team.TeamView = Depends(_view)):
    return team.conversation_highlights(view, conversation_id)


@router.post("/team/questions/{rq_id}/adopt")
def adopt_question(rq_id: int, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    return serialize_rqs(view.db, [team.adopt_question(view, user, rq_id)])[0]


# --- question groups ------------------------------------------------------------------------


@router.post("/team/question_groups")
def create_question_group(
    body: TeamQuestionGroupCreate, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)
):
    return team.create_group(view, user, body.name, body.research_question_ids)


@router.post("/team/question_groups/suggest")
def suggest_question_groups(view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    """Paid: one model call."""
    return team.suggest_groups(view, user, config)


@router.get("/team/question_groups/{group_id}")
def get_question_group(group_id: int, include_dismissed: bool = False, view: team.TeamView = Depends(_view)):
    return team.group_detail(view, group_id, include_dismissed)


@router.patch("/team/question_groups/{group_id}")
def update_question_group(group_id: int, body: TeamQuestionGroupUpdate, view: team.TeamView = Depends(_view)):
    return team.rename_group(view, group_id, body.name)


@router.post("/team/question_groups/{group_id}/questions")
def add_question_to_group(
    group_id: int, body: TeamGroupQuestionAdd, view: team.TeamView = Depends(_view),
    user: CurrentUser = Depends(current_user),
):
    return team.add_group_question(view, user, group_id, body.research_question_id)


@router.delete("/team/question_groups/{group_id}/questions/{rq_id}")
def remove_question_from_group(group_id: int, rq_id: int, view: team.TeamView = Depends(_view)):
    return team.remove_group_question(view, group_id, rq_id)


@router.post("/team/question_groups/{group_id}/accept")
def accept_question_group(group_id: int, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    return team.accept_group(view, user, group_id)


@router.post("/team/question_groups/{group_id}/dismiss")
def dismiss_question_group(group_id: int, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    return team.dismiss_group(view, user, group_id)


# --- team themes ----------------------------------------------------------------------------


@router.post("/team/question_groups/{group_id}/themes/synthesize")
def synthesize_themes(group_id: int, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    """Paid: one model call."""
    return team.synthesize_themes(view, user, group_id, config)


@router.post("/team/question_groups/{group_id}/themes")
def create_theme(
    group_id: int, body: TeamThemeCreate, view: team.TeamView = Depends(_view),
    user: CurrentUser = Depends(current_user),
):
    return team.create_theme(view, user, group_id, body.name, body.definition, body.connection_ids)


@router.patch("/team/themes/{theme_id}")
def update_theme(
    theme_id: int, body: TeamThemeUpdate, view: team.TeamView = Depends(_view),
    user: CurrentUser = Depends(current_user),
):
    return team.update_theme(view, user, theme_id, body.name, body.definition)


@router.put("/team/themes/{theme_id}/members")
def set_theme_members(
    theme_id: int, body: TeamThemeMembersUpdate, view: team.TeamView = Depends(_view),
    user: CurrentUser = Depends(current_user),
):
    return team.set_theme_members(view, user, theme_id, body.connection_ids)


@router.post("/team/themes/{theme_id}/accept")
def accept_theme(theme_id: int, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    return team.decide_theme(view, user, theme_id, "accepted")


@router.post("/team/themes/{theme_id}/dismiss")
def dismiss_theme(theme_id: int, view: team.TeamView = Depends(_view), user: CurrentUser = Depends(current_user)):
    return team.decide_theme(view, user, theme_id, "dismissed")


@router.get("/team/themes/{theme_id}/history")
def get_theme_history(theme_id: int, view: team.TeamView = Depends(_view)):
    return team.theme_history(view, theme_id)
