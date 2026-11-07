"""Who is calling, and which Cortico data they can see.

In `snapshot` mode there is no sign-in: every request is `config.default_user_id`, as the
prototype always was. In `cortico_api` mode a request must carry a session token (the
`nim_session` cookie, or `Authorization: Bearer <session token>` for scripts), created by
POST /auth/login after Cortico accepts the user's email and password.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import get_config
from .cortico.client import CorticoClient
from .cortico.errors import CorticoAuthError
from .cortico.source import ApiSource, CorticoSource, SnapshotSource
from .db import CorticoSession, get_prototype_db
from .models_prototype import RLSession, RLWorkspace

config = get_config()
ACCESS_TOKEN_FALLBACK_TTL = timedelta(minutes=15)
REFRESH_MARGIN = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- token encryption -----------------------------------------------------------------


def _fernet() -> Fernet:
    if not config.session_secret:
        raise RuntimeError("SESSION_SECRET must be set in cortico_api mode.")
    key = base64.urlsafe_b64encode(hashlib.sha256(config.session_secret.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise CorticoAuthError("Your session can't be read. Sign in again.") from exc


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def access_token_expiry(access_token: str) -> datetime:
    """Reads `exp` from the JWT payload without verifying it (Cortico verifies). Falls back
    to Cortico's documented 15-minute lifetime."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (IndexError, KeyError, ValueError, TypeError):
        return _now() + ACCESS_TOKEN_FALLBACK_TTL


# Tests swap in an httpx.MockTransport standing in for Cortico.
TRANSPORT = None


def new_client(access_token: str | None = None, refresh=None) -> CorticoClient:
    return CorticoClient(
        config.cortico.base_url,
        access_token,
        refresh=refresh,
        timeout=config.cortico.timeout_seconds,
        max_retries=config.cortico.max_retries,
        transport=TRANSPORT,
        sleep=(lambda _s: None) if TRANSPORT is not None else time.sleep,
    )


# --- sessions ---------------------------------------------------------------------------


def create_session(db: Session, tokens: dict, me: dict) -> str:
    token = secrets.token_urlsafe(32)
    name = " ".join(p for p in (me.get("first_name"), me.get("last_name")) if p) or None
    db.add(
        RLSession(
            token_hash=hash_token(token),
            user_id=me["id"],
            email=me.get("email"),
            display_name=name,
            access_token_enc=encrypt(tokens["access_token"]),
            refresh_token_enc=encrypt(tokens["refresh_token"]),
            access_expires_at=access_token_expiry(tokens["access_token"]),
            expires_at=_now() + timedelta(days=config.session.max_age_days),
        )
    )
    db.commit()
    return token


def session_token_from(request: Request) -> str | None:
    token = request.cookies.get(config.session.cookie_name)
    if token:
        return token
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def refresh_session(db: Session, token_hash: str) -> str:
    """Single-flight refresh. Cortico rotates refresh tokens and revokes the old one, so two
    requests refreshing at once would log the user out. The row lock (FOR UPDATE on Postgres;
    SQLite serializes writers anyway) makes the second request reuse the first one's result."""
    row = db.query(RLSession).filter(RLSession.token_hash == token_hash).with_for_update().one_or_none()
    if row is None:
        raise CorticoAuthError()
    if _aware(row.access_expires_at) > _now() + REFRESH_MARGIN:
        access = decrypt(row.access_token_enc)
        db.commit()
        return access
    with new_client() as client:
        try:
            tokens = client.refresh_tokens(decrypt(row.refresh_token_enc))
        except CorticoAuthError:
            db.delete(row)
            db.commit()
            raise
    row.access_token_enc = encrypt(tokens["access_token"])
    row.refresh_token_enc = encrypt(tokens["refresh_token"])
    row.access_expires_at = access_token_expiry(tokens["access_token"])
    db.commit()
    return tokens["access_token"]


@dataclass
class CurrentUser:
    id: int
    email: str | None = None
    display_name: str | None = None
    session: RLSession | None = None

    @property
    def signed_in(self) -> bool:
        return self.session is not None


def current_user(request: Request, db: Session = Depends(get_prototype_db)) -> CurrentUser:
    if config.data_source == "snapshot":
        return CurrentUser(id=config.default_user_id)
    token = session_token_from(request)
    if not token:
        raise HTTPException(401, "Sign in with your Cortico account.")
    row = db.get(RLSession, hash_token(token))
    if row is None or _aware(row.expires_at) < _now():
        raise HTTPException(401, "Your session has ended. Sign in again.")
    row.last_seen_at = _now()
    db.commit()
    return CurrentUser(id=row.user_id, email=row.email, display_name=row.display_name, session=row)


def active_workspace(db: Session, user: CurrentUser) -> RLWorkspace | None:
    if user.session is not None and user.session.active_workspace_id:
        ws = db.get(RLWorkspace, user.session.active_workspace_id)
        if ws and ws.user_id == user.id and not ws.archived:
            return ws
    return (
        db.query(RLWorkspace)
        .filter(RLWorkspace.user_id == user.id, RLWorkspace.archived.is_(False),
                RLWorkspace.data_source == config.data_source)
        .order_by(RLWorkspace.updated_at.desc())
        .first()
    )


def workspace_project_ids(ws: RLWorkspace | None) -> list[int]:
    return list(json.loads(ws.project_ids_json)) if ws else []


def cortico_client_for(db: Session, user: CurrentUser) -> CorticoClient:
    """A client carrying this user's access token, refreshing it first when it is about to
    expire, and once more if Cortico still answers 401."""
    if user.session is None:
        raise HTTPException(401, "Sign in with your Cortico account.")
    token_hash = user.session.token_hash
    if _aware(user.session.access_expires_at) <= _now() + REFRESH_MARGIN:
        access = refresh_session(db, token_hash)
    else:
        access = decrypt(user.session.access_token_enc)
    return new_client(access, refresh=lambda: _force_refresh(db, token_hash))


def _force_refresh(db: Session, token_hash: str) -> str:
    row = db.get(RLSession, token_hash)
    if row is not None:
        row.access_expires_at = _now()  # make refresh_session treat it as expired
        db.commit()
    return refresh_session(db, token_hash)


def get_source(
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_prototype_db),
):
    """FastAPI dependency: the Cortico data source for this request."""
    if config.data_source == "snapshot":
        cortico_db = CorticoSession()
        try:
            yield SnapshotSource(cortico_db)
        finally:
            cortico_db.close()
        return
    client = cortico_client_for(db, user)
    try:
        yield ApiSource(
            client,
            db,
            snippet_ttl_seconds=config.cortico.snippet_cache_ttl_seconds,
            workspace_project_ids=workspace_project_ids(active_workspace(db, user)),
        )
    finally:
        client.close()

