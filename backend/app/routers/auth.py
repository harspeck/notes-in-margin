from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import (
    CurrentUser,
    active_workspace,
    cortico_client_for,
    create_session,
    current_user,
    hash_token,
    new_client,
    session_token_from,
    workspace_project_ids,
)
from ..config import get_config
from ..cortico.errors import CorticoError
from ..db import get_prototype_db
from ..models_prototype import RLSession

router = APIRouter(prefix="/auth", tags=["auth"])
config = get_config()


class LoginBody(BaseModel):
    email: str
    password: str


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        config.session.cookie_name,
        token,
        max_age=config.session.max_age_days * 86400,
        httponly=True,
        secure=(
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"
            or config.session.cookie_samesite == "none"
        ),
        samesite=config.session.cookie_samesite,
        path="/",
    )


@router.post("/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    include_token: bool = False,
    db: Session = Depends(get_prototype_db),
):
    """Trades the user's Cortico email and password for Cortico tokens. Cortico offers no
    OAuth redirect for third-party apps, so this app sees the password once, in transit, and
    never stores it."""
    if config.data_source != "cortico_api":
        raise HTTPException(400, "Sign-in is only used when DATA_SOURCE is cortico_api.")
    if not config.session_secret:
        raise HTTPException(503, "The server is missing SESSION_SECRET, so it can't keep sessions.")
    with new_client() as client:
        tokens = client.login(body.email.strip(), body.password)
        client.access_token = tokens["access_token"]
        me = client.me()
    token = create_session(db, tokens, me)
    _set_cookie(request, response, token)
    payload = {"user": _user_payload(me)}
    if include_token:  # for scripts, which send it back as a Bearer token; browsers use the cookie
        payload["session_token"] = token
    return payload


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_prototype_db)):
    token = session_token_from(request)
    row = db.get(RLSession, hash_token(token)) if token else None
    if row is not None:
        try:
            user = CurrentUser(id=row.user_id, session=row)
            with cortico_client_for(db, user) as client:
                client.logout()
        except (CorticoError, HTTPException):
            pass  # best effort: the local session ends either way
        db.delete(row)
        db.commit()
    response.delete_cookie(config.session.cookie_name, path="/")
    return {"signed_out": True}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user), db: Session = Depends(get_prototype_db)):
    ws = active_workspace(db, user)
    return {
        "data_source": config.data_source,
        "user": {"id": user.id, "email": user.email, "name": user.display_name},
        "active_workspace": {"id": ws.id, "name": ws.name, "organization_id": ws.organization_id,
                             "project_ids": workspace_project_ids(ws)} if ws else None,
    }


def _user_payload(me: dict) -> dict:
    name = " ".join(p for p in (me.get("first_name"), me.get("last_name")) if p) or None
    return {"id": me["id"], "email": me.get("email"), "name": name}
