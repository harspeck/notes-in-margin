"""Organizations, projects, and workspaces (named bundles of projects).

The project list a user sees is what /v2/projects returns, filtered to the projects their
/v2/me roles cover: a role on the project itself, or an organization-wide role. Nobody has
confirmed that /v2/projects already filters by membership, so the filter stays.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, cortico_client_for, current_user
from ..config import get_config
from ..cortico.client import CorticoClient
from ..db import CorticoSession, get_prototype_db
from ..models_prototype import RLWorkspace

router = APIRouter(tags=["workspaces"])
config = get_config()


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    organization_id: int
    project_ids: list[int] = Field(min_length=1)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    project_ids: list[int] | None = Field(default=None, min_length=1)
    archived: bool | None = None


def accessible_projects(client: CorticoClient, organization_id: int | None = None) -> tuple[dict, list[dict]]:
    me = client.me()
    roles = me.get("roles") or []
    project_roles = {r["project_id"] for r in roles if r.get("project_id")}
    org_roles = {r["organization_id"] for r in roles if r.get("organization_id") and not r.get("project_id")}
    projects = client.projects(organization_id)
    if roles:
        projects = [p for p in projects if p["id"] in project_roles or p.get("organization_id") in org_roles]
    return me, projects


def _snapshot_projects(organization_id: int | None = None) -> tuple[list[dict], list[dict]]:
    from ..models_cortico import Conversation, Organization, Project

    with CorticoSession() as db:
        orgs = [{"id": o.id, "name": o.name} for o in db.query(Organization).order_by(Organization.name)]
        q = db.query(Project)
        if organization_id:
            q = q.filter(Project.organization_id == organization_id)
        counts = dict(
            db.query(Conversation.project_id, func.count(Conversation.id))
            .group_by(Conversation.project_id)
            .all()
        )
        projects = [
            {"id": p.id, "name": p.name, "description": p.description, "organization_id": p.organization_id,
             "conversation_count": counts.get(p.id, 0)}
            for p in q.order_by(Project.name)
        ]
    return orgs, projects


@router.get("/organizations")
def list_organizations(user: CurrentUser = Depends(current_user), db: Session = Depends(get_prototype_db)):
    if config.data_source == "snapshot":
        orgs, projects = _snapshot_projects()
    else:
        with cortico_client_for(db, user) as client:
            me, projects = accessible_projects(client)
        orgs = [{"id": o["id"], "name": o.get("name")} for o in me.get("organizations") or []]
    counts: dict[int, int] = {}
    for p in projects:
        counts[p.get("organization_id")] = counts.get(p.get("organization_id"), 0) + 1
    known = {o["id"] for o in orgs}
    # Projects can sit in an organization the user isn't a member of (project-only roles).
    orgs += [{"id": oid, "name": None} for oid in counts if oid is not None and oid not in known]
    return [{**o, "project_count": counts.get(o["id"], 0)} for o in orgs if counts.get(o["id"])]


@router.get("/organizations/{organization_id}/projects")
def list_projects(organization_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_prototype_db)):
    if config.data_source == "snapshot":
        return _snapshot_projects(organization_id)[1]
    with cortico_client_for(db, user) as client:
        _, projects = accessible_projects(client, organization_id)
    return [
        {"id": p["id"], "name": p.get("name"), "description": p.get("description"), "organization_id": p.get("organization_id")}
        for p in sorted(projects, key=lambda p: (p.get("name") or "").lower())
    ]


def _serialize(ws: RLWorkspace, active_id: int | None) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "organization_id": ws.organization_id,
        "project_ids": json.loads(ws.project_ids_json),
        "archived": ws.archived,
        "active": ws.id == active_id,
        "updated_at": ws.updated_at.isoformat() if ws.updated_at else None,
    }


def _check_projects(db: Session, user: CurrentUser, organization_id: int, project_ids: list[int]) -> None:
    if config.data_source == "snapshot":
        allowed = {p["id"] for p in _snapshot_projects(organization_id)[1]}
    else:
        with cortico_client_for(db, user) as client:
            allowed = {p["id"] for p in accessible_projects(client, organization_id)[1]}
    missing = sorted(set(project_ids) - allowed)
    if missing:
        raise HTTPException(403, f"These projects aren't open to your account in this organization: {missing}")


def _owned(db: Session, user: CurrentUser, workspace_id: int) -> RLWorkspace:
    ws = db.get(RLWorkspace, workspace_id)
    if ws is None or ws.user_id != user.id:
        raise HTTPException(404, "workspace not found")
    return ws


@router.get("/workspaces")
def list_workspaces(user: CurrentUser = Depends(current_user), db: Session = Depends(get_prototype_db)):
    active = active_workspace(db, user)
    rows = (
        db.query(RLWorkspace)
        .filter(RLWorkspace.user_id == user.id, RLWorkspace.data_source == config.data_source)
        .order_by(RLWorkspace.updated_at.desc())
        .all()
    )
    return [_serialize(w, active.id if active else None) for w in rows]


@router.post("/workspaces")
def create_workspace(body: WorkspaceCreate, user: CurrentUser = Depends(current_user), db: Session = Depends(get_prototype_db)):
    project_ids = sorted(set(body.project_ids))
    _check_projects(db, user, body.organization_id, project_ids)
    ws = RLWorkspace(
        user_id=user.id,
        organization_id=body.organization_id,
        name=body.name.strip(),
        project_ids_json=json.dumps(project_ids),
        data_source=config.data_source,
    )
    db.add(ws)
    db.flush()
    if user.session is not None:
        user.session.active_workspace_id = ws.id
    db.commit()
    return _serialize(ws, ws.id)


@router.patch("/workspaces/{workspace_id}")
def update_workspace(workspace_id: int, body: WorkspaceUpdate, user: CurrentUser = Depends(current_user),
                     db: Session = Depends(get_prototype_db)):
    ws = _owned(db, user, workspace_id)
    if body.project_ids is not None:
        project_ids = sorted(set(body.project_ids))
        _check_projects(db, user, ws.organization_id, project_ids)
        ws.project_ids_json = json.dumps(project_ids)
    if body.name is not None:
        ws.name = body.name.strip()
    if body.archived is not None:
        ws.archived = body.archived
    db.commit()
    active = active_workspace(db, user)
    return _serialize(ws, active.id if active else None)


@router.post("/workspaces/{workspace_id}/activate")
def activate_workspace(workspace_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_prototype_db)):
    ws = _owned(db, user, workspace_id)
    if ws.archived:
        raise HTTPException(409, "Unarchive this workspace before opening it.")
    if user.session is not None:
        user.session.active_workspace_id = ws.id
    else:
        # Snapshot mode has no session, so the most recently updated workspace is the active one.
        ws.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _serialize(ws, ws.id)
