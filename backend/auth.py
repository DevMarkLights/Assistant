# Username/password authentication: register, login, logout, and the current_user dependency.
# Sessions are random tokens kept in an httpOnly cookie; only their SHA-256 hash is stored in MongoDB.
import hashlib
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pwdlib import PasswordHash

import db2
from config import ALLOW_SIGNUP, COOKIE_SECURE, SESSION_DAYS
from models import Credentials

COOKIE_NAME = "session"
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")

password_hash = PasswordHash.recommended()
# Verified against when the username doesn't exist, so login takes the same time either way
DUMMY_HASH = password_hash.hash("not-a-real-password")

router = APIRouter(prefix="/Assistant/api/auth", tags=["auth"])


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _start_session(response: Response, user_id: str):
    token = secrets.token_urlsafe(32)
    max_age = timedelta(days=SESSION_DAYS)
    await db2.create_session(_hash_token(token), user_id, db2.now() + max_age)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=int(max_age.total_seconds()),
        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/Assistant",
    )


async def current_user(session: str | None = Cookie(default=None)) -> dict:
    user = await db2.get_session_user(_hash_token(session)) if session else None
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


@router.post("/register")
async def register(creds: Credentials, response: Response):
    if not ALLOW_SIGNUP:
        raise HTTPException(status_code=403, detail="Sign-up is disabled")
    username = creds.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400,
                            detail="Username must be 3-32 characters: letters, numbers, . _ -")
    user = await db2.create_user(username, password_hash.hash(creds.password))
    if not user:
        raise HTTPException(status_code=409, detail="Username is taken")
    await _start_session(response, user["id"])
    return user


@router.post("/login")
async def login(creds: Credentials, response: Response):
    user = await db2.get_user_with_hash(creds.username.strip().lower())
    valid = password_hash.verify(creds.password, user["password_hash"] if user else DUMMY_HASH)
    if not user or not valid:
        raise HTTPException(status_code=401, detail="Wrong username or password")
    await _start_session(response, user["id"])
    return {"id": user["id"], "username": user["username"]}


@router.post("/logout", status_code=204)
async def logout(response: Response, session: str | None = Cookie(default=None)):
    if session:
        await db2.delete_session(_hash_token(session))
    response.delete_cookie(COOKIE_NAME, path="/Assistant")


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user
