"""Error model: one stable, machine-readable failure shape for every tool.

Tools never raise raw exceptions at the agent. They raise `ToolFailure` (an MCP `ToolError`)
whose text is `CODE: human message [retry_after_seconds=N] [request_id=…]`, so an AI agent can
branch on the code and a human can quote the request id at support. Petabyte's own error
envelope (`{"error": {"code", "message", "next", "request_id", "retry_after_seconds"}}`) is
parsed here and mapped 1:1 — the API's code is preserved whenever it sends one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from mcp.server.mcpserver.exceptions import ToolError


class Codes:
    """Error codes the MCP layer itself produces (API-originated codes pass through)."""

    NO_API_KEY = "NO_API_KEY"
    INVALID_API_KEY = "INVALID_API_KEY"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    ACCOUNT_ACCESS_REQUIRED = "ACCOUNT_ACCESS_REQUIRED"
    READ_ONLY_MODE = "READ_ONLY_MODE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    ALREADY_STOPPED = "ALREADY_STOPPED"
    RESTART_RELAUNCH_FAILED = "RESTART_RELAUNCH_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    RATE_LIMITED = "RATE_LIMIT_EXCEEDED"
    API_UNAVAILABLE = "API_UNAVAILABLE"
    API_TIMEOUT = "API_TIMEOUT"
    API_ERROR = "API_ERROR"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    FORBIDDEN = "NOT_PERMITTED"
    CONFLICT = "CONFLICT"


class ToolFailure(ToolError):
    """A deliberate, agent-readable tool failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        retry_after_s: int | None = None,
        next_hint: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after_s = retry_after_s
        self.next_hint = next_hint
        self.details = dict(details or {})
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [f"{self.code}: {self.message}"]
        if self.retry_after_s is not None:
            parts.append(f"[retry_after_seconds={self.retry_after_s}]")
        if self.request_id:
            parts.append(f"[request_id={self.request_id}]")
        if self.next_hint:
            parts.append(f"| next: {self.next_hint}")
        if self.details:
            parts.append("| " + json.dumps(self.details, sort_keys=True, default=str))
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.request_id:
            out["request_id"] = self.request_id
        if self.retry_after_s is not None:
            out["retry_after_seconds"] = self.retry_after_s
        if self.next_hint:
            out["next"] = self.next_hint
        if self.details:
            out["details"] = self.details
        return out


def fail(code: str, message: str, **kwargs: Any) -> NoReturn:
    raise ToolFailure(code, message, **kwargs)


class PetabyteAPIError(Exception):
    """A non-2xx response from the Petabyte API, already parsed into its envelope fields."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        retry_after_s: int | None = None,
        next_hint: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after_s = retry_after_s
        self.next_hint = next_hint
        super().__init__(f"{status} {code}: {message}")


_STATUS_CODES = {
    400: "INVALID_REQUEST",
    401: "NOT_AUTHENTICATED",
    402: "INSUFFICIENT_BALANCE",
    403: "NOT_PERMITTED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMIT_EXCEEDED",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def _flatten_validation(detail: list[Any]) -> str:
    """FastAPI 422 body -> 'field: message; field2: message' (mirrors the CLI's humaniser)."""
    parts = []
    for item in detail:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        loc = ".".join(str(p) for p in (item.get("loc") or [])[1:]) or "input"
        parts.append(f"{loc}: {item.get('msg')}")
    return "; ".join(parts)


def parse_error_response(status: int, headers: Mapping[str, str], body: str) -> PetabyteAPIError:
    """Turn an error response into a `PetabyteAPIError`, understanding Petabyte's envelope,
    FastAPI's 422 list, a bare `{"detail": "..."}` and non-JSON bodies."""
    code = _STATUS_CODES.get(status, "REQUEST_FAILED")
    message = f"HTTP {status}"
    request_id = None
    next_hint = None
    retry_after: int | None = None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra and str(ra).strip().isdigit():
        retry_after = int(str(ra).strip())
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        detail = parsed.get("detail")
        if isinstance(err, dict):
            code = str(err.get("code") or code)
            message = str(err.get("message") or message)
            request_id = err.get("request_id") or None
            next_hint = err.get("next") or None
            if err.get("retry_after_seconds") is not None:
                try:
                    retry_after = int(err["retry_after_seconds"])
                except (TypeError, ValueError):
                    pass
        elif isinstance(detail, list):
            code = "VALIDATION_FAILED"
            message = _flatten_validation(detail)
        elif isinstance(detail, dict):
            code = str(detail.get("code") or code)
            message = str(detail.get("message") or message)
            next_hint = detail.get("next") or None
        elif isinstance(detail, str):
            message = detail
    elif body:
        message = body.strip()[:200]
    return PetabyteAPIError(
        status, code, message, request_id=request_id, retry_after_s=retry_after, next_hint=next_hint
    )


# API codes that carry a meaning the MCP layer wants to name explicitly for agents.
_ACCOUNT_ACCESS_MARKER = "not scoped for account access"


def to_tool_failure(err: PetabyteAPIError) -> ToolFailure:
    """Map an API error onto the tool failure vocabulary without losing the API's own code."""
    code = err.code
    message = err.message
    if err.status == 401:
        code = Codes.INVALID_API_KEY
        message = f"the Petabyte API rejected the API key: {err.message}"
    elif err.status == 403:
        if _ACCOUNT_ACCESS_MARKER in err.message:
            code = Codes.ACCOUNT_ACCESS_REQUIRED
        elif err.code in ("NOT_PERMITTED", "REQUEST_FAILED"):
            code = Codes.FORBIDDEN
    elif err.status == 402:
        code = Codes.INSUFFICIENT_FUNDS
    elif err.status == 404 and err.code in ("NOT_FOUND", "REQUEST_FAILED"):
        code = Codes.NOT_FOUND
    elif err.status == 422:
        code = Codes.VALIDATION_FAILED
    elif err.status == 429:
        code = Codes.RATE_LIMITED
    elif err.status >= 500 and err.code in (
        "INTERNAL_ERROR",
        "SERVICE_UNAVAILABLE",
        "BAD_GATEWAY",
        "GATEWAY_TIMEOUT",
        "REQUEST_FAILED",
    ):
        code = Codes.API_ERROR
        message = f"the Petabyte API returned HTTP {err.status}: {err.message}"
    return ToolFailure(
        code,
        message,
        request_id=err.request_id,
        retry_after_s=err.retry_after_s,
        next_hint=err.next_hint,
    )
