"""
Authentication helpers and public auth routes.

This is intentionally small for the demo: database-backed users, Argon2
password hashes, and short-lived HS256 bearer tokens.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from database import get_db
from models import TokenResponse, User, UserLogin, UserRegister


ACCESS_TOKEN_EXPIRE_MINUTES = 60
JWT_ALGORITHM = "HS256"
JWT_SECRET_KEY_ENV = "JWT_SECRET_KEY"

password_hasher = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
router = APIRouter(prefix="/auth", tags=["auth"])


def _get_jwt_secret_key() -> str:
    secret_key = os.getenv(JWT_SECRET_KEY_ENV)
    if not secret_key or len(secret_key) < 32:
        raise RuntimeError(f"{JWT_SECRET_KEY_ENV} must be set to at least 32 characters")
    return secret_key


def validate_auth_config() -> None:
    _get_jwt_secret_key()


def get_password_hash(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(user: User) -> tuple[str, int]:
    expires_delta = dt.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    expires_at = dt.datetime.now(dt.UTC) + expires_delta
    token = jwt.encode(
        {"sub": str(user.id), "exp": expires_at},
        _get_jwt_secret_key(),
        algorithm=JWT_ALGORITHM,
    )
    return token, int(expires_delta.total_seconds())


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(token, _get_jwt_secret_key(), algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub", ""))
    except (InvalidTokenError, TypeError, ValueError):
        raise _auth_error()

    user = db.get(User, user_id)
    if not user:
        raise _auth_error()
    return user


@router.post("/register", response_model=TokenResponse)
def register_user(body: UserRegister, db: Session = Depends(get_db)):
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username and password are required",
        )

    existing_user = db.query(User).filter(User.username == username).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    user = User(username=username, password_hash=get_password_hash(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token, expires_in = create_access_token(user)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/login", response_model=TokenResponse)
def login_user(body: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username.strip()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise _auth_error()

    token, expires_in = create_access_token(user)
    return TokenResponse(access_token=token, expires_in=expires_in)
