"""“Sign in with Google” for Claude on Google Cloud Vertex AI.

Uses Google's OAuth 2.0 authorization-code flow with PKCE and an OAuth client of type
“Desktop app” that the admin creates in their own Google Cloud project (Google requires an
OAuth client per application; there is no shared one).

Because the server is usually reached on a LAN name or IP, which Google doesn't accept as a
redirect address for web clients, the flow uses the loopback address that Desktop clients
allow (http://127.0.0.1:53682/). After the admin approves access, Google sends their browser
there, the page doesn't load, and they paste that address back into the app, which then
exchanges the code for a refresh token. The refresh token never goes to the browser: the
editor only gets an opaque, short-lived “grant” id that is swapped for the token on save.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://127.0.0.1:53682/"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SCOPES = f"openid email {CLOUD_SCOPE}"
STATE_TTL = 20 * 60
GRANT_TTL = 60 * 60

_lock = threading.Lock()
_pending: dict[str, dict] = {}   # state -> {verifier, client_id, client_secret, created}
_grants: dict[str, dict] = {}    # grant id -> {refresh_token, email, client_id, created}


class GoogleSignInError(ValueError):
    pass


def _prune() -> None:
    now = time.time()
    for store, ttl in ((_pending, STATE_TTL), (_grants, GRANT_TTL)):
        for k in [k for k, v in store.items() if now - v["created"] > ttl]:
            store.pop(k, None)


def start(client_id: str, client_secret: str) -> dict:
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise GoogleSignInError("Enter the OAuth client ID first (it ends in .apps.googleusercontent.com).")
    if not client_secret:
        raise GoogleSignInError("Enter the OAuth client secret first.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    with _lock:
        _prune()
        _pending[state] = {"verifier": verifier, "client_id": client_id, "client_secret": client_secret,
                           "created": time.time()}
    params = {
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code", "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true",
    }
    return {"auth_url": f"{AUTH_URL}?{urllib.parse.urlencode(params)}", "redirect_uri": REDIRECT_URI}


def _email_from_id_token(id_token: str | None) -> str:
    if not id_token or id_token.count(".") != 2:
        return ""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except Exception:
        return ""


def finish(pasted_url: str) -> dict:
    """Exchange the code in the pasted redirect address for a refresh token (kept server-side)."""
    text = (pasted_url or "").strip()
    query = urllib.parse.urlparse(text).query if "?" in text else text
    params = urllib.parse.parse_qs(query)
    if params.get("error"):
        err = params["error"][0]
        raise GoogleSignInError("Access was not granted in Google." if err == "access_denied"
                                else f"Google returned an error: {err}")
    code, state = (params.get("code") or [""])[0], (params.get("state") or [""])[0]
    if not code or not state:
        raise GoogleSignInError("That address doesn't contain a sign-in code. Copy the whole address of the page "
                                "Google sent you to (it starts with http://127.0.0.1:53682/?state=…).")
    with _lock:
        _prune()
        pend = _pending.pop(state, None)
    if not pend:
        raise GoogleSignInError("This sign-in has expired or was already used. Click “Sign in with Google” again.")

    body = urllib.parse.urlencode({
        "code": code, "client_id": pend["client_id"], "client_secret": pend["client_secret"],
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code", "code_verifier": pend["verifier"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error_description") or e.reason
        except Exception:
            detail = e.reason
        raise GoogleSignInError(f"Google refused the sign-in: {detail}")
    except (urllib.error.URLError, OSError) as e:
        raise GoogleSignInError(f"Could not reach Google from the server: {getattr(e, 'reason', e)}")
    if CLOUD_SCOPE not in (tok.get("scope") or ""):
        raise GoogleSignInError("Google Cloud access wasn't granted. Sign in again and tick every permission.")
    if not tok.get("refresh_token"):
        raise GoogleSignInError("Google didn't return a long-lived sign-in. Remove this app's access at "
                                "myaccount.google.com/permissions, then sign in again.")
    grant = secrets.token_urlsafe(24)
    email = _email_from_id_token(tok.get("id_token"))
    with _lock:
        _grants[grant] = {"refresh_token": tok["refresh_token"], "email": email,
                          "client_id": pend["client_id"], "created": time.time()}
    return {"grant": f"grant:{grant}", "email": email}


def take_grant(value: str) -> dict | None:
    """Swap a "grant:<id>" placeholder for the stored sign-in (once)."""
    if not isinstance(value, str) or not value.startswith("grant:"):
        return None
    with _lock:
        _prune()
        return _grants.pop(value[len("grant:"):], None)


def peek_grant(value: str) -> dict | None:
    """Like take_grant, but leaves it in place (for Test / Load models before saving)."""
    if not isinstance(value, str) or not value.startswith("grant:"):
        return None
    with _lock:
        return _grants.get(value[len("grant:"):])
