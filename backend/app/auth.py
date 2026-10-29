"""Accounts, password hashing, sessions and request authentication.

The design choices here are deliberately conservative and dependency-free:

* Passwords are hashed with `hashlib.scrypt` and a per-user random salt. The stored string
  records the parameters, so they can be raised later without invalidating old passwords.
* Session cookies are opaque random tokens. Only a SHA-256 digest is stored, so a database
  leak does not expose usable sessions.
* The session cookie is `HttpOnly` and `SameSite=Lax`. A separate CSRF cookie is readable
  by the dashboard, and state-changing cookie-authenticated requests must echo it in
  `X-CSRF-Token`.
* Login attempts are limited both per account (a temporary lock) and per client address
  (an in-process sliding window). That is enough for a single-node home deployment; a
  multi-node deployment should move the window to shared storage.
* Device tokens are a separate credential type. A device can write only to its own record,
  which in turn belongs to exactly one user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.database import get_db
from backend.app.models import AuthSession, Device, User
from backend.app.timestamps import utc_now

SESSION_COOKIE = "wattxpense_session"
CSRF_COOKIE = "wattxpense_csrf"
CSRF_HEADER = "x-csrf-token"
DEVICE_TOKEN_PREFIX = "wxdev_"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# scrypt parameters. 2**14 is a reasonable interactive-login cost on a laptop; the stored
# hash records these values so they can be increased without breaking existing accounts.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

# In-process brute-force window, keyed by client address and by normalised email.
_ATTEMPTS: dict[str, list[float]] = {}
_WINDOW_SECONDS = 300


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def validate_email(value: str) -> str:
    email = normalize_email(value)
    if len(email) > 320 or not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address")
    return email


def validate_password(value: str) -> str:
    password = value or ""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if len(password) > 128:
        raise ValueError("Password must be at most 128 characters")
    if not password.strip():
        raise ValueError("Password must not be only whitespace")
    return password


def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash. Never store the password itself."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = (encoded or "").split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A fixed hash used to keep the "unknown email" path roughly as expensive as a real one.
_DUMMY_HASH = hash_password("not-a-real-password")


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_device_token() -> tuple[str, str]:
    raw = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, token_hash(raw)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(key: str, now: float) -> list[float]:
    stamps = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < _WINDOW_SECONDS]
    if stamps:
        _ATTEMPTS[key] = stamps
    else:
        _ATTEMPTS.pop(key, None)
    return stamps


def check_attempt_window(key: str, *, limit: int) -> None:
    """Refuse a burst of attempts from one key. Raises 429, never reveals account state."""
    now = time.monotonic()
    stamps = _prune(key, now)
    if len(stamps) >= limit:
        retry = max(1, int(_WINDOW_SECONDS - (now - stamps[0])))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {retry} seconds.",
            headers={"Retry-After": str(retry)},
        )


def record_attempt(key: str) -> None:
    _ATTEMPTS.setdefault(key, []).append(time.monotonic())


def clear_attempts(key: str) -> None:
    _ATTEMPTS.pop(key, None)


def reset_rate_limits() -> None:
    """Test helper. Production code has no reason to call this."""
    _ATTEMPTS.clear()


def _set_cookies(response: Response, *, session_token: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = max(int(settings.session_ttl_hours * 3600), 60)
    common = {
        "max_age": max_age,
        "secure": bool(settings.cookie_secure),
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True, **common)
    # The CSRF cookie is intentionally readable by the dashboard; it is not the credential.
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **common)


def clear_cookies(response: Response) -> None:
    settings = get_settings()
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=bool(settings.cookie_secure), samesite="lax")


def start_session(db: Session, user: User, request: Request, response: Response) -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = utc_now()
    row = AuthSession(
        user_id=user.id,
        token_hash=token_hash(raw),
        csrf_token=csrf,
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(hours=max(get_settings().session_ttl_hours, 1)),
        user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        ip_address=_client_key(request)[:64],
    )
    db.add(row)
    db.flush()
    _set_cookies(response, session_token=raw, csrf_token=csrf)
    return raw, csrf


def revoke_session(db: Session, row: AuthSession) -> None:
    row.revoked_at = utc_now()


def _session_token_from_request(request: Request) -> tuple[str | None, bool]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie, True
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip() or None, False
    return None, False


@dataclass
class AuthContext:
    user: User
    session: AuthSession | None
    via_cookie: bool


def require_user(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Resolve the signed-in user and enforce CSRF for cookie-authenticated writes."""
    raw, via_cookie = _session_token_from_request(request)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue",
            headers={"WWW-Authenticate": "Session"},
        )

    row = db.execute(
        select(AuthSession).where(AuthSession.token_hash == token_hash(raw))
    ).scalars().first()
    now = utc_now()
    if row is None or row.revoked_at is not None or (_aware(row.expires_at) or now) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is unavailable")

    if via_cookie and request.method.upper() in _UNSAFE_METHODS:
        supplied = request.headers.get(CSRF_HEADER) or ""
        if not supplied or not hmac.compare_digest(supplied, row.csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")

    row.last_used_at = now
    return AuthContext(user=user, session=row, via_cookie=via_cookie)


def optional_user(request: Request, db: Session = Depends(get_db)) -> AuthContext | None:
    if not (request.cookies.get(SESSION_COOKIE) or request.headers.get("authorization")):
        return None
    try:
        return require_user(request, db)
    except HTTPException:
        return None


@dataclass
class DevicePrincipal:
    device: Device


def require_device(request: Request, db: Session = Depends(get_db)) -> DevicePrincipal:
    """Authenticate a hardware device by its one-time-issued bearer token."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token required")
    raw = header.split(" ", 1)[1].strip()
    if not raw.startswith(DEVICE_TOKEN_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token required")
    device = db.execute(
        select(Device).where(Device.api_token_hash == token_hash(raw))
    ).scalars().first()
    if device is None or device.owner_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token is not valid")
    return DevicePrincipal(device=device)


def user_payload(user: User) -> dict:
    payload = user.as_dict()
    payload["created_at"] = payload["created_at"].isoformat() if payload["created_at"] else None
    payload["last_login_at"] = payload["last_login_at"].isoformat() if payload["last_login_at"] else None
    return payload
