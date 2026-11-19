"""Which Cortico projects the signed-in account can reach right now.

A workspace's projects are checked against the user's Cortico roles when it is saved. Roles
change afterwards, so anything that shows one user another user's work (services/team.py)
asks again, through `accessible_project_ids`: one lookup per session per window, held in
process memory, never one per conversation.
"""
from __future__ import annotations

import time

from ..cortico.client import CorticoClient

# session token hash -> (monotonic expiry, project ids)
_CACHE: dict[str, tuple[float, frozenset[int]]] = {}


def accessible_projects(client: CorticoClient, organization_id: int | None = None) -> tuple[dict, list[dict]]:
    me = client.me()
    roles = me.get("roles") or []
    project_roles = {r["project_id"] for r in roles if r.get("project_id")}
    org_roles = {r["organization_id"] for r in roles if r.get("organization_id") and not r.get("project_id")}
    projects = client.projects(organization_id)
    if roles:
        projects = [p for p in projects if p["id"] in project_roles or p.get("organization_id") in org_roles]
    return me, projects


def accessible_project_ids(client: CorticoClient, session_key: str, ttl_minutes: float) -> frozenset[int]:
    """A Cortico failure raises (the request fails): it never falls back to an unchecked list,
    and a failed lookup is not cached."""
    now = time.monotonic()
    hit = _CACHE.get(session_key)
    if hit is not None and hit[0] > now:
        return hit[1]
    _, projects = accessible_projects(client)
    ids = frozenset(p["id"] for p in projects)
    for key in [k for k, (expires, _) in _CACHE.items() if expires <= now]:
        del _CACHE[key]
    _CACHE[session_key] = (now + ttl_minutes * 60, ids)
    return ids


def clear_access_cache() -> None:
    _CACHE.clear()
