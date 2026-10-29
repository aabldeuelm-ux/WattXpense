"""Registration, login, logout and account endpoints."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth import (
    AuthContext,
    _aware,
    _client_key,
    check_attempt_window,
    clear_attempts,
    clear_cookies,
    hash_password,
    normalize_email,
    record_attempt,
    require_user,
    revoke_session,
    start_session,
    user_payload,
    validate_email,
    validate_password,
    verify_password,
    _DUMMY_HASH,
)
from backend.app.config import get_settings
from backend.app.database import get_db
from backend.app.models import AuthSession, User
from backend.app.schemas import ChangePasswordIn, LoginIn, RegisterIn
from backend.app.timestamps import utc_now

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _generic_login_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Email or password is incorrect",
        headers={"WWW-Authenticate": "Session"},
    )


@router.get("/status")
def auth_status(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Public. The dashboard uses this to choose the login screen or the dashboard."""
    settings = get_settings()
    from backend.app.auth import optional_user

    context = optional_user(request, db)
    return {
        "authenticated": context is not None,
        "user": user_payload(context.user) if context else None,
        "registration_enabled": settings.registration_enabled,
    }


@router.post("/register")
def register(
    payload: RegisterIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.registration_enabled:
        raise HTTPException(status_code=403, detail="Registration is disabled on this install")

    client = _client_key(request)
    check_attempt_window(f"register:{client}", limit=10)
    record_attempt(f"register:{client}")

    try:
        email = validate_email(payload.email)
        password = validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = db.execute(select(User).where(User.email == email)).scalars().first()
    if existing is not None:
        # Deliberately the same shape as a successful validation failure, so registration
        # cannot be used as an account-enumeration oracle.
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=payload.display_name,
        is_active=True,
        last_login_at=utc_now(),
    )
    db.add(user)
    db.flush()
    _, csrf = start_session(db, user, request, response)
    db.commit()
    return {"user": user_payload(user), "csrf_token": csrf}


@router.post("/login")
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    email = normalize_email(payload.email)
    client = _client_key(request)
    ip_key = f"login-ip:{client}"
    email_key = f"login-email:{email}"

    check_attempt_window(ip_key, limit=max(settings.login_max_attempts * 2, 10))
    check_attempt_window(email_key, limit=max(settings.login_max_attempts * 2, 10))

    user = db.execute(select(User).where(User.email == email)).scalars().first()
    now = utc_now()

    if user is not None:
        locked_until = _aware(user.locked_until)
        if locked_until is not None and locked_until > now:
            retry = max(1, int((locked_until - now).total_seconds()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed attempts. Try again in {retry} seconds.",
                headers={"Retry-After": str(retry)},
            )
        if locked_until is not None and locked_until <= now:
            user.locked_until = None
            user.failed_login_count = 0

    encoded = user.password_hash if user is not None else _DUMMY_HASH
    if not verify_password(payload.password, encoded) or user is None or not user.is_active:
        record_attempt(ip_key)
        record_attempt(email_key)
        if user is not None:
            user.failed_login_count = int(user.failed_login_count or 0) + 1
            if user.failed_login_count >= settings.login_max_attempts:
                user.locked_until = now + timedelta(minutes=max(settings.login_lockout_minutes, 1))
            db.commit()
        raise _generic_login_error()

    clear_attempts(ip_key)
    clear_attempts(email_key)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    _, csrf = start_session(db, user, request, response)
    db.commit()
    return {"user": user_payload(user), "csrf_token": csrf}


@router.post("/logout")
def logout(
    response: Response,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    if auth.session is not None:
        revoke_session(db, auth.session)
        db.commit()
    clear_cookies(response)
    return {"status": "signed_out"}


@router.get("/me")
def me(auth: AuthContext = Depends(require_user)) -> dict:
    return {"user": user_payload(auth.user)}


@router.post("/password")
def change_password(
    payload: ChangePasswordIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    if not verify_password(payload.current_password, auth.user.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    try:
        new_password = validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    auth.user.password_hash = hash_password(new_password)
    # Other sessions are revoked; the one making the change stays valid.
    current_id = auth.session.id if auth.session is not None else None
    for row in db.execute(
        select(AuthSession).where(
            AuthSession.user_id == auth.user.id,
            AuthSession.id != current_id,
            AuthSession.revoked_at.is_(None),
        )
    ).scalars():
        revoke_session(db, row)
    db.commit()
    return {"status": "password_changed"}
