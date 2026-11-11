"""
Xác thực & phân quyền vai trò.
  - Mặc định: tài khoản demo trong config/policies.yml, phát JWT HS256 (ký bằng APP_SECRET).
  - Supabase Auth (tùy chọn): đặt SUPABASE_JWT_SECRET -> chấp nhận access token của Supabase; vai trò lấy từ
    app_metadata.role (analyst/admin), RLS lấy từ app_metadata.rls (vd {"vung": ["B"]}).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Depends, Header, HTTPException

from . import config
from .governance import UserCtx

TTL = 12 * 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(payload: dict, secret: str) -> str:
    h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64(json.dumps(payload, ensure_ascii=False).encode())
    sig = _b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def jwt_decode(token: str, secret: str) -> dict | None:
    try:
        h, p, sig = token.split(".")
        exp_sig = _b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, exp_sig):
            return None
        if json.loads(_unb64(h)).get("alg") != "HS256":
            return None
        payload = json.loads(_unb64(p))
        if payload.get("exp") and payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


def login(username: str, password: str) -> dict | None:
    users = config.policies().get("users", {})
    u = users.get(username)
    if not u or not hmac.compare_digest(str(u.get("password")), str(password)):
        return None
    payload = {"sub": username, "role": u["role"], "rls": u.get("rls") or {}, "name": u.get("full_name", username),
               "iat": int(time.time()), "exp": int(time.time()) + TTL}
    return {"token": jwt_encode(payload, config.SECRET), "user": public_user(payload)}


def public_user(p: dict) -> dict:
    role_cfg = config.policies()["roles"][p["role"]]
    return {"username": p["sub"], "role": p["role"], "role_label": role_cfg.get("label"), "full_name": p.get("name"),
            "rls": p.get("rls") or {}, "can_manage_schema": role_cfg.get("can_manage_schema", False)}


def current_user(authorization: str | None = Header(default=None)) -> UserCtx:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Chưa đăng nhập")
    token = authorization.split(" ", 1)[1]
    p = jwt_decode(token, config.SECRET)
    if p is None and os.environ.get("SUPABASE_JWT_SECRET"):
        sp = jwt_decode(token, os.environ["SUPABASE_JWT_SECRET"])
        if sp:
            meta = sp.get("app_metadata") or {}
            p = {"sub": sp.get("email") or sp.get("sub"), "role": meta.get("role", "analyst"), "rls": meta.get("rls") or {},
                 "name": (sp.get("user_metadata") or {}).get("full_name") or sp.get("email")}
    if p is None or p.get("role") not in config.policies()["roles"]:
        raise HTTPException(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
    return UserCtx(username=p["sub"], role=p["role"], rls=p.get("rls") or {}, full_name=p.get("name", ""))


def require_admin(user: UserCtx = Depends(current_user)) -> UserCtx:
    if not user.role_cfg.get("can_manage_schema"):
        raise HTTPException(403, "Chức năng chỉ dành cho Admin")
    return user
