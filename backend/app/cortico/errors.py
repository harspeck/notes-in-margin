from __future__ import annotations


class CorticoError(Exception):
    """Base for every failure talking to Cortico. `status` is the HTTP status this app
    should answer with, not necessarily Cortico's."""

    status = 502
    message = "Cortico returned an unexpected error."

    def __init__(self, detail: str | None = None, *, upstream_status: int | None = None, retry_after: float | None = None):
        super().__init__(detail or self.message)
        self.detail = detail or self.message
        self.upstream_status = upstream_status
        self.retry_after = retry_after


class CorticoAuthError(CorticoError):
    """The Cortico session is missing, expired, or was revoked. The user must sign in again."""

    status = 401
    message = "Your Cortico session has expired. Sign in again."


class CorticoLoginFailed(CorticoError):
    status = 401
    message = "Cortico didn't accept that email and password."


class CorticoForbidden(CorticoError):
    """The signed-in account lacks a role for this object. Never retried."""

    status = 403
    message = "Your Cortico account doesn't have access to this."


class CorticoNotFound(CorticoError):
    status = 404
    message = "Cortico couldn't find that."


class CorticoRejected(CorticoError):
    """Cortico refused the request body (400/409/422)."""

    status = 422
    message = "Cortico rejected the request."


class CorticoUnavailable(CorticoError):
    """Rate limited, timed out, or a 5xx after retries."""

    status = 503
    message = "Cortico is unavailable right now. Try again in a moment."
