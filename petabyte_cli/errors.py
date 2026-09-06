"""errors — one human-first error shape for the whole CLI, and the translation from the
technical failures (httpx, HTTP status codes) into it.

    ✗ Unable to connect to Petabyte.
      Check your internet connection and try again.

      petabyte doctor
      (ConnectError: [Errno -2] Name or service not known)

The technical detail is kept (dimmed, last) so a developer is never left guessing; with
--verbose the full traceback is printed as well.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

DOCTOR = "petabyte doctor"
LOGIN = "petabyte login"


class CliError(Exception):
    """A failure the CLI knows how to explain. exit_code 1 = handled error."""

    def __init__(self, title: str, *, reason: str | None = None, fix: str | Sequence[str] | None = None,
                 run: str | Sequence[str] | None = None, detail: str | None = None, exit_code: int = 1):
        super().__init__(title)
        self.title = title
        self.reason = reason
        self.fix = fix
        self.run = run
        self.detail = detail
        self.exit_code = exit_code

    def render(self, ui) -> None:
        ui.error(self.title, reason=self.reason, fix=self.fix, run=self.run, detail=self.detail)

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.title, "reason": self.reason, "fix": self.fix, "run": self.run,
                "detail": self.detail}


class NotSignedIn(CliError):
    def __init__(self, detail: str | None = None):
        super().__init__("You're not signed in to Petabyte.",
                         reason="This command needs your account.",
                         fix="Sign in with your browser (no password on the CLI), or export an account API key "
                             "as PETABYTE_API_KEY.",
                         run=LOGIN, detail=detail)


class Unreachable(CliError):
    def __init__(self, api_url: str, detail: str | None = None):
        super().__init__("Unable to connect to Petabyte.",
                         reason=f"No response from {api_url}.",
                         fix=["Check your internet connection and try again.",
                              "If the problem continues, run the diagnostics:"],
                         run=DOCTOR, detail=detail)


def humanize_response(r) -> str:
    """The server's own message, if it sent one (handles our {error:{message}} envelope,
    FastAPI's {detail} and 422 lists)."""
    try:
        j = r.json()
    except Exception:
        return (getattr(r, "text", "") or "").strip()[:200]
    if not isinstance(j, dict):
        return str(j)[:200]
    err = j.get("error") if isinstance(j.get("error"), dict) else {}
    msg = err.get("message")
    detail = j.get("detail")
    if isinstance(detail, list):
        parts = []
        for e in detail:
            # FastAPI validation errors are mappings, but a plain {"detail": ["invalid input"]}
            # is equally valid and used elsewhere. Calling .get() on a string raised
            # AttributeError here, so from_response() could not build a CliError at all and the
            # user saw a traceback instead of the message the server actually sent.
            if not isinstance(e, dict):
                parts.append(str(e))
                continue
            loc = ".".join(str(p) for p in (e.get("loc") or [])[1:]) or "input"
            parts.append(f"{loc}: {e.get('msg')}")
        detail = "; ".join(parts)
    out = msg or (detail if isinstance(detail, str) else None) or (getattr(r, "text", "") or "").strip()[:200]
    rid = err.get("request_id")
    return f"{out}  [req {rid}]" if rid else out


def from_response(r, what: str, api_url: str = "") -> CliError:
    """Turn a non-2xx response into a CliError with the right next step."""
    code = r.status_code
    msg = humanize_response(r)
    if code == 401:
        return NotSignedIn(detail=f"HTTP 401: {msg}")
    if code == 403:
        low = (msg or "").lower()
        if "scope" in low or "not scoped" in low:
            return CliError(f"Your API key can't {what}.",
                            reason="The key you exported is limited to specific scopes.",
                            fix="Sign in with your browser instead, or create an 'account'-scoped key on the web.",
                            run=LOGIN, detail=f"HTTP 403: {msg}")
        if "email" in low and "verif" in low:
            return CliError(f"Verify your email before you {what}.",
                            fix="Open Settings on the web and confirm the verification email.",
                            detail=f"HTTP 403: {msg}")
        return CliError(f"Petabyte refused to {what}.", reason=msg, detail=f"HTTP 403")
    if code == 404:
        return CliError(f"Could not {what}: not found.", reason=msg, detail="HTTP 404")
    if code == 402:
        return CliError(f"Not enough balance to {what}.", reason=msg,
                        fix="Add funds first:", run="petabyte deposit 20", detail="HTTP 402")
    if code == 429:
        return CliError("Slow down — too many requests.", reason=msg, fix="Wait a minute and try again.",
                        detail="HTTP 429")
    if code >= 500:
        return CliError("Petabyte had a problem handling that request.",
                        reason="This is on our side, not yours.",
                        fix="Try again in a moment. If it keeps happening, quote the request id to support.",
                        detail=f"HTTP {code}: {msg}")
    return CliError(f"Could not {what}.", reason=msg, detail=f"HTTP {code}")


def from_exception(exc: BaseException, api_url: str = "") -> CliError:
    """httpx / socket failures -> a connectivity error people can act on."""
    name = type(exc).__name__
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return CliError("Petabyte took too long to answer.",
                            reason=f"{api_url or 'The API'} did not respond in time.",
                            fix="Check your connection and try again; the service may be busy.",
                            run=DOCTOR, detail=f"{name}: {exc}")
        if isinstance(exc, httpx.ConnectError):
            low = str(exc).lower()
            if "certificate" in low or "ssl" in low or "tls" in low:
                return CliError("Secure connection to Petabyte failed.",
                                reason="The TLS certificate could not be verified (TLS verification is never turned off).",
                                fix=["Check the system clock and any corporate proxy / VPN.",
                                     "Make sure PETABYTE_API_URL points at the real Petabyte host."],
                                run=DOCTOR, detail=f"{name}: {exc}")
            return Unreachable(api_url or "the Petabyte API", detail=f"{name}: {exc}")
        if isinstance(exc, httpx.RequestError):
            return Unreachable(api_url or "the Petabyte API", detail=f"{name}: {exc}")
    except ImportError:
        pass
    if isinstance(exc, (ConnectionError, OSError)):
        return Unreachable(api_url or "the Petabyte API", detail=f"{name}: {exc}")
    return CliError("Something went wrong.", reason=str(exc)[:200] or name,
                    fix="Run again with --verbose for the full details, or:", run=DOCTOR, detail=name)
