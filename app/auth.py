"""Cognito managed login with PKCE, ported from aws_gallery's auth Lambda.

Unlike the gallery there are no CloudFront signed cookies: every page is
rendered by this app, so the HMAC-signed session cookie is checked here.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
import jwt
from starlette.responses import HTMLResponse, RedirectResponse

COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
REGION = os.environ.get("REGION", "us-east-1")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
HMAC_PARAM = os.environ.get("HMAC_PARAM", "")
SESSION_TTL = int(os.environ.get("SESSION_TTL", "604800"))

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
REDIRECT_URI = f"{SITE_URL}/auth/callback"
SESSION_COOKIE = "vaad_user"

_jwks_client = None
_hmac_key = None


def _secret() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        _hmac_key = boto3.client("ssm").get_parameter(
            Name=HMAC_PARAM, WithDecryption=True
        )["Parameter"]["Value"].encode()
    return _hmac_key


def sign_session(email: str, expires: int, key: bytes | None = None) -> str:
    payload = f"{email}|{expires}"
    digest = hmac.new(key or _secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{digest}"


def session_user(cookie: str | None, key: bytes | None = None) -> str | None:
    """The signed-in email, or None for a missing, forged or expired cookie."""
    if not cookie:
        return None
    payload, _, digest = cookie.rpartition("|")
    email, _, expires = payload.rpartition("|")
    expected = hmac.new(key or _secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not email or not hmac.compare_digest(digest, expected):
        return None
    if not expires.isdigit() or int(expires) < time.time():
        return None
    return email


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json")
    return _jwks_client


def _set(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def _error(code: int, message: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>בעיה בכניסה</title>"
        f"<body dir=rtl style='font-family:system-ui;padding:2rem'>"
        f"<h1>בעיה בכניסה</h1><p>{message}</p><p><a href='/auth/login'>לנסות שוב</a></p>",
        status_code=code,
        headers={"cache-control": "no-store"},
    )


def login(next_path: str) -> RedirectResponse:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    response = RedirectResponse(f"https://{COGNITO_DOMAIN}/oauth2/authorize?{params}", 302,
                                headers={"cache-control": "no-store"})
    _set(response, "pkce", verifier, 600)
    _set(response, "state", state, 600)
    _set(response, "next", urllib.parse.quote(next_path), 600)
    return response


def callback(query: dict, cookies: dict):
    if "error" in query:
        return _error(400, f"Cognito: {query.get('error_description', query['error'])}")
    if not query.get("code"):
        return _error(400, "לא התקבל קוד כניסה.")
    if not cookies.get("state") or not secrets.compare_digest(cookies["state"], query.get("state", "")):
        return _error(400, "הכניסה לא הושלמה בדפדפן הזה.")
    if not cookies.get("pkce"):
        return _error(400, "הכניסה לקחה יותר מדי זמן. התחל מחדש.")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": query["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": cookies["pkce"],
    }).encode()
    request = urllib.request.Request(
        f"https://{COGNITO_DOMAIN}/oauth2/token", data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            tokens = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _error(502, f"Token exchange failed: {exc.read().decode()[:200]}")

    # Back-channel TLS already authenticates the issuer; verifying anyway so a
    # misrouted or replayed token cannot mint a session.
    try:
        claims = jwt.decode(
            tokens["id_token"],
            _jwks().get_signing_key_from_jwt(tokens["id_token"]).key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
        )
    except jwt.PyJWTError as exc:
        return _error(401, f"Token rejected: {exc}")

    expires = int(time.time()) + SESSION_TTL
    email = claims.get("email") or claims.get("cognito:username") or "unknown"
    next_path = urllib.parse.unquote(cookies.get("next", "/"))
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    response = RedirectResponse(next_path, 302, headers={"cache-control": "no-store"})
    _set(response, SESSION_COOKIE, sign_session(email, expires), SESSION_TTL)
    for stale in ("pkce", "state", "next"):
        _set(response, stale, "", 0)
    return response


def logout() -> RedirectResponse:
    params = urllib.parse.urlencode({"client_id": CLIENT_ID, "logout_uri": f"{SITE_URL}/"})
    response = RedirectResponse(f"https://{COGNITO_DOMAIN}/logout?{params}", 302,
                                headers={"cache-control": "no-store"})
    _set(response, SESSION_COOKIE, "", 0)
    return response
