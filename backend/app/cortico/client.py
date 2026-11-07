"""Thin synchronous client for Cortico's v2 API (https://api2.cortico.ai).

All Cortico calls stay server-side: api2's CORS only allows Cortico's own domains, and the
user's tokens never reach the browser.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from .errors import (
    CorticoAuthError,
    CorticoError,
    CorticoForbidden,
    CorticoLoginFailed,
    CorticoNotFound,
    CorticoRejected,
    CorticoUnavailable,
)

# Pagination params differ by route (page_info.cursor is read the same way for both).
AFTER_CURSOR_ROUTES = ("/v2/organizations", "/v2/projects", "/v2/highlights", "/v2/highlight-shares")
MAX_RETRY_AFTER_SECONDS = 10.0
MAX_PAGES = 200


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:300]
    return str(body)[:300]


def raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    detail = _detail(response)
    if status == 401:
        raise CorticoAuthError(upstream_status=status)
    if status == 403:
        raise CorticoForbidden(upstream_status=status)
    if status == 404:
        raise CorticoNotFound(upstream_status=status)
    if status in (400, 409, 422):
        raise CorticoRejected(f"Cortico rejected the request: {detail}", upstream_status=status)
    if status == 429 or status >= 500:
        retry_after = response.headers.get("Retry-After")
        raise CorticoUnavailable(
            upstream_status=status,
            retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    raise CorticoError(f"Cortico returned {status}: {detail}", upstream_status=status)


class CorticoClient:
    """One client per request. `access_token` is the user's bearer token; `refresh` is called
    at most once per request when Cortico answers 401, and must return a fresh access token
    (or raise CorticoAuthError)."""

    def __init__(
        self,
        base_url: str,
        access_token: str | None = None,
        *,
        refresh: Callable[[], str] | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self._refresh = refresh
        self._refreshed = False
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CorticoClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- core request loop -------------------------------------------------------------

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None, auth: bool = True) -> Any:
        attempt = 0
        while True:
            headers = {"Accept": "application/json"}
            if auth:
                if not self.access_token:
                    raise CorticoAuthError()
                headers["Authorization"] = f"Bearer {self.access_token}"
            try:
                response = self._http.request(method, path, params=_clean(params), json=json, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self._sleep(min(2**attempt * 0.5, MAX_RETRY_AFTER_SECONDS))
                    continue
                raise CorticoUnavailable(f"Couldn't reach Cortico ({type(exc).__name__}).") from exc

            if response.status_code == 401 and auth and self._refresh and not self._refreshed:
                self._refreshed = True
                self.access_token = self._refresh()
                continue
            if (response.status_code == 429 or response.status_code >= 500) and attempt < self.max_retries:
                attempt += 1
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt * 0.5
                self._sleep(min(delay, MAX_RETRY_AFTER_SECONDS))
                continue
            raise_for_status(response)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def paginate(self, path: str, list_key: str, *, page_size: int = 100, **params: Any) -> Iterator[dict]:
        """Follows page_info.cursor until page_info.total rows have arrived. Cortico sets a
        cursor on the last page too (the last row's id), and /v2/conversations rejects that
        cursor ("Invalid GUID"), so `total`, not a null cursor, is what ends the loop."""
        cursor_param = "after" if path.startswith(AFTER_CURSOR_ROUTES) else "cursor"
        cursor = None
        seen = 0
        for _ in range(MAX_PAGES):
            page = self.get(path, count=page_size, **{cursor_param: cursor}, **params)
            rows = (page or {}).get(list_key) or []
            yield from rows
            seen += len(rows)
            info = (page or {}).get("page_info") or {}
            cursor = info.get("cursor")
            total = info.get("total")
            if cursor is None or not rows or (total is not None and seen >= total):
                return
        raise CorticoUnavailable(f"Stopped paging {path} after {MAX_PAGES} pages.")

    # --- auth (routes sit at the root, not under /v2) -----------------------------------

    def login(self, email: str, password: str) -> dict:
        try:
            tokens = self.request("POST", "/auth/login", json={"email": email, "password": password}, auth=False)
        except (CorticoAuthError, CorticoRejected, CorticoForbidden) as exc:
            raise CorticoLoginFailed() from exc
        return _tokens(tokens)

    def refresh_tokens(self, refresh_token: str) -> dict:
        try:
            tokens = self.request("POST", "/auth/refresh-tokens", json={"refresh_token": refresh_token}, auth=False)
        except (CorticoRejected, CorticoForbidden, CorticoNotFound) as exc:
            raise CorticoAuthError() from exc
        return _tokens(tokens)

    def logout(self) -> None:
        self.request("POST", "/auth/logout")

    # --- reads ----------------------------------------------------------------------------

    def me(self) -> dict:
        return self.get("/v2/me")

    def projects(self, organization_id: int | None = None) -> list[dict]:
        params = {"organization_ids": organization_id} if organization_id else {}
        return list(self.paginate("/v2/projects", "projects", **params))

    def conversations(self, project_id: int) -> list[dict]:
        # Large pages ordered by id, as llm-auto-highlighter does: one request for almost
        # every project, so the cursor is rarely needed.
        return list(self.paginate("/v2/conversations", "conversations", page_size=1000, project_id=project_id, order_by="id"))

    def conversation(self, conversation_id: int) -> dict:
        return self.get(f"/v2/conversations/{conversation_id}")

    def transcript(self, conversation_id: int) -> list[dict]:
        return (self.get(f"/v2/conversations/{conversation_id}/transcript") or {}).get("snippets") or []

    def conversation_guides(self, project_id: int) -> list[dict]:
        return list(self.paginate("/v2/conversation-guides", "items", project_id=project_id))

    # --- writes ---------------------------------------------------------------------------

    def create_highlight(self, payload: dict) -> dict:
        return self.request("POST", "/v2/highlights", json=payload)

    def delete_highlight(self, highlight_id: int) -> None:
        self.request("DELETE", f"/v2/highlights/{highlight_id}")


def _clean(params: dict | None) -> dict | None:
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _tokens(body: Any) -> dict:
    if not isinstance(body, dict) or not body.get("access_token") or not body.get("refresh_token"):
        raise CorticoError("Cortico's sign-in response had no tokens.")
    return {"access_token": body["access_token"], "refresh_token": body["refresh_token"]}
