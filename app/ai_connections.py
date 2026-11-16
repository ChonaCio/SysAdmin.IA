"""AI connections: which AI services the chat can use, and how it signs in to them.

A connection = provider (Anthropic Claude, OpenAI/ChatGPT, xAI Grok, Groq, Google Gemini,
Azure OpenAI, GitHub Models, ...) + connection method (API key, Google Cloud Vertex AI,
Amazon Bedrock, ...) + settings (model, endpoint, ...). Admins create, edit and delete them in
the "AI connections" module; each chat uses one (the default one unless chosen otherwise).

Secrets (API keys, credentials) are stored in the app database (readable only by the service
user, like before) and are never sent back to the browser: the API only reports whether
each secret is set. A blank secret field keeps the saved value.

About subscription logins: Claude.ai / Claude Code accounts (including "Sign in with Google")
can't be used by other applications under Anthropic's terms, so they are not a connection
method here. To use Claude with a Google sign-in, use Google Cloud Vertex AI.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import db
from .config import settings

# ---------------------------------------------------------------- provider catalog

F_API_KEY = {"key": "api_key", "label": "API key", "type": "password", "secret": True}

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic Claude", "short": "Claude", "kind": "anthropic", "color": "#c2571a",
        "description": "Claude models. Best tool use for server administration.",
        "default_model": "claude-opus-5-5",
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"],
        "env_key": "ANTHROPIC_API_KEY",
        "supports_effort": True,
        "methods": {
            "api_key": {
                "label": "Anthropic API key",
                "help": "Create a key at console.anthropic.com → API keys.",
                "fields": [dict(F_API_KEY, placeholder="sk-ant-…", required=True)],
            },
            "google_account": {
                "label": "Google account (Sign in with Google)",
                "help": "Sign in with your Google account to use Claude through Google Cloud Vertex AI (billed by Google). "
                        "Your account needs the “Vertex AI User” role in the project, with Claude enabled in the Model Garden.",
                "setup": [
                    "In the Google Cloud console, open APIs & Services → OAuth consent screen and set it up "
                    "(set the publishing status to “In production”: in “Testing”, Google ends the sign-in after 7 days).",
                    "Then Credentials → Create credentials → OAuth client ID → Application type “Desktop app”.",
                    "Copy its client ID and client secret into the fields below, then click Sign in with Google.",
                ],
                "fields": [
                    {"key": "project_id", "label": "Google Cloud project ID", "type": "text", "required": True,
                     "placeholder": "my-project-123"},
                    {"key": "region", "label": "Region", "type": "text", "default": "global",
                     "placeholder": "global, us-east5, europe-west1…"},
                    {"key": "google_client_id", "label": "OAuth client ID", "type": "text", "required": True,
                     "placeholder": "123…-abc.apps.googleusercontent.com"},
                    {"key": "google_client_secret", "label": "OAuth client secret", "type": "password",
                     "secret": True, "required": True, "placeholder": "GOCSPX-…"},
                    {"key": "google_refresh_token", "label": "Google account", "type": "google_signin",
                     "secret": True, "required": True},
                ],
                "extra_config": ["google_account"],
                "model_help": "Vertex model IDs may differ, e.g. claude-opus-5-5@20261001. Use Load models or check the Model Garden.",
            },
            "vertex": {
                "label": "Google Cloud service account (Vertex AI)",
                "help": "Claude through your Google Cloud project, signing in with a Google service account, "
                        "or with the server's own Google login (run `gcloud auth application-default login` "
                        "in the Console as the app's user and leave the credentials empty).",
                "fields": [
                    {"key": "project_id", "label": "Google Cloud project ID", "type": "text", "required": True,
                     "placeholder": "my-project-123"},
                    {"key": "region", "label": "Region", "type": "text", "default": "global",
                     "placeholder": "global, us-east5, europe-west1…"},
                    {"key": "credentials_json", "label": "Service account key (JSON)", "type": "textarea",
                     "secret": True, "placeholder": "{ \"type\": \"service_account\", … }",
                     "help": "Paste the key file's contents. Leave empty to use the server's Google login."},
                ],
                "model_help": "Vertex model IDs may differ, e.g. claude-opus-5-5@20261001. Use Load models or check the Model Garden.",
            },
            "bedrock": {
                "label": "Amazon Bedrock",
                "help": "Claude through your AWS account. Leave the keys empty to use the server's AWS credentials "
                        "(instance role or ~/.aws).",
                "fields": [
                    {"key": "aws_region", "label": "AWS region", "type": "text", "required": True, "default": "us-east-1"},
                    {"key": "aws_access_key", "label": "Access key ID", "type": "text", "placeholder": "AKIA…"},
                    {"key": "aws_secret_key", "label": "Secret access key", "type": "password", "secret": True},
                ],
                "model_help": "Bedrock model IDs look like anthropic.claude-opus-5-5-v1:0 or an inference profile such as us.anthropic.….",
            },
        },
    },
    "openai": {
        "label": "OpenAI (ChatGPT)", "short": "ChatGPT", "kind": "openai", "color": "#10a37f",
        "description": "GPT models from OpenAI, the models behind ChatGPT.",
        "base_url": "https://api.openai.com/v1", "default_model": "gpt-5",
        "models": ["gpt-5", "gpt-5-mini", "gpt-4.1", "o4-mini"], "env_key": "OPENAI_API_KEY",
        "methods": {"api_key": {
            "label": "OpenAI API key",
            "help": "Create a key at platform.openai.com → API keys. A ChatGPT Plus/Pro subscription is separate from API access.",
            "fields": [dict(F_API_KEY, placeholder="sk-…", required=True),
                       {"key": "organization", "label": "Organization ID", "type": "text", "placeholder": "optional, org-…"}],
        }},
    },
    "azure": {
        "label": "Microsoft Azure OpenAI (Copilot stack)", "short": "Azure", "kind": "openai", "color": "#0078d4",
        "description": "OpenAI models hosted in your Azure subscription (Azure AI Foundry) — the platform Microsoft Copilot is built on.",
        "default_model": "", "models": [], "env_key": "AZURE_OPENAI_API_KEY",
        "methods": {"api_key": {
            "label": "Azure API key",
            "help": "In Azure AI Foundry: your resource's endpoint and key, and the name of a model deployment.",
            "fields": [
                {"key": "endpoint", "label": "Endpoint", "type": "url", "required": True,
                 "placeholder": "https://my-resource.openai.azure.com"},
                dict(F_API_KEY, required=True),
                {"key": "api_version", "label": "API version", "type": "text", "default": "2024-10-21"},
            ],
            "model_label": "Deployment name",
        }},
    },
    "github": {
        "label": "GitHub Models (Copilot)", "short": "GitHub", "kind": "openai", "color": "#6e40c9",
        "description": "Models from OpenAI, Microsoft, Meta, xAI and others through GitHub, with your GitHub account.",
        "base_url": "https://models.github.ai/inference", "default_model": "openai/gpt-4.1",
        "models": ["openai/gpt-4.1", "openai/gpt-4.1-mini", "xai/grok-3", "meta/Llama-4-Maverick-17B-128E-Instruct-FP8"],
        "env_key": "GITHUB_TOKEN",
        "methods": {"token": {
            "label": "GitHub token",
            "help": "A fine-grained personal access token with the “Models: read” permission (github.com → Settings → "
                    "Developer settings). Usage counts against your GitHub Copilot / Models limits.",
            "fields": [dict(F_API_KEY, label="GitHub token", placeholder="github_pat_…", required=True)],
        }},
    },
    "xai": {
        "label": "xAI Grok", "short": "Grok", "kind": "openai", "color": "#111111",
        "description": "Grok models from xAI.",
        "base_url": "https://api.x.ai/v1", "default_model": "grok-4",
        "models": ["grok-4", "grok-3", "grok-3-mini"], "env_key": "XAI_API_KEY",
        "methods": {"api_key": {"label": "xAI API key", "help": "Create a key at console.x.ai.",
                                "fields": [dict(F_API_KEY, placeholder="xai-…", required=True)]}},
    },
    "groq": {
        "label": "Groq", "short": "Groq", "kind": "openai", "color": "#f55036",
        "description": "Very fast hosted open models (Llama, GPT-OSS, Qwen, …).",
        "base_url": "https://api.groq.com/openai/v1", "default_model": "openai/gpt-oss-120b",
        "models": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"], "env_key": "GROQ_API_KEY",
        "methods": {"api_key": {"label": "Groq API key", "help": "Create a key at console.groq.com.",
                                "fields": [dict(F_API_KEY, placeholder="gsk_…", required=True)]}},
    },
    "gemini": {
        "label": "Google Gemini", "short": "Gemini", "kind": "openai", "color": "#4285f4",
        "description": "Gemini models from Google AI Studio.",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "default_model": "gemini-2.5-pro",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"], "env_key": "GEMINI_API_KEY",
        "methods": {"api_key": {"label": "Gemini API key", "help": "Create a key at aistudio.google.com → Get API key.",
                                "fields": [dict(F_API_KEY, placeholder="AIza…", required=True)]}},
    },
    "mistral": {
        "label": "Mistral AI", "short": "Mistral", "kind": "openai", "color": "#fa520f",
        "description": "Mistral and Codestral models.",
        "base_url": "https://api.mistral.ai/v1", "default_model": "mistral-large-latest",
        "models": ["mistral-large-latest", "mistral-medium-latest", "codestral-latest"], "env_key": "MISTRAL_API_KEY",
        "methods": {"api_key": {"label": "Mistral API key", "help": "Create a key at console.mistral.ai.",
                                "fields": [dict(F_API_KEY, required=True)]}},
    },
    "deepseek": {
        "label": "DeepSeek", "short": "DeepSeek", "kind": "openai", "color": "#4d6bfe",
        "description": "DeepSeek chat and reasoning models.",
        "base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"], "env_key": "DEEPSEEK_API_KEY",
        "methods": {"api_key": {"label": "DeepSeek API key", "help": "Create a key at platform.deepseek.com.",
                                "fields": [dict(F_API_KEY, placeholder="sk-…", required=True)]}},
    },
    "openrouter": {
        "label": "OpenRouter", "short": "OpenRouter", "kind": "openai", "color": "#6467f2",
        "description": "One key for hundreds of models from many providers.",
        "base_url": "https://openrouter.ai/api/v1", "default_model": "openai/gpt-5",
        "models": ["openai/gpt-5", "x-ai/grok-4", "google/gemini-2.5-pro", "meta-llama/llama-3.3-70b-instruct"],
        "env_key": "OPENROUTER_API_KEY",
        "methods": {"api_key": {"label": "OpenRouter API key", "help": "Create a key at openrouter.ai/keys.",
                                "fields": [dict(F_API_KEY, placeholder="sk-or-…", required=True)]}},
    },
    "ollama": {
        "label": "Ollama (local models)", "short": "Ollama", "kind": "openai", "color": "#555555",
        "description": "Models running on this server or your network with Ollama. No account needed.",
        "base_url": "http://localhost:11434/v1", "default_model": "llama3.1",
        "models": ["llama3.1", "qwen2.5", "mistral"],
        "methods": {"local": {
            "label": "Local server (no sign-in)",
            "help": "Install Ollama and pull a model that supports tools, e.g. `ollama pull llama3.1`.",
            "fields": [{"key": "base_url", "label": "Ollama address", "type": "url", "default": "http://localhost:11434/v1"}],
        }},
    },
    "custom": {
        "label": "Other (OpenAI-compatible)", "short": "Custom", "kind": "openai", "color": "#677180",
        "description": "Any service with an OpenAI-compatible chat API (vLLM, LM Studio, Together, Fireworks, LiteLLM, …).",
        "default_model": "", "models": [],
        "methods": {"api_key": {
            "label": "Endpoint + optional key",
            "help": "The base URL usually ends in /v1. The model must support tool (function) calling.",
            "fields": [{"key": "base_url", "label": "Base URL", "type": "url", "required": True,
                        "placeholder": "https://api.example.com/v1"},
                       dict(F_API_KEY, label="API key (optional)")],
        }},
    },
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConnectionError_(ValueError):
    pass


def catalog() -> dict:
    """Provider catalog for the UI (no secrets here)."""
    out = {}
    for pid, p in PROVIDERS.items():
        out[pid] = {k: v for k, v in p.items() if k != "env_key"}
        out[pid]["env_key_available"] = bool(p.get("env_key") and os.environ.get(p["env_key"]))
        out[pid]["env_key"] = p.get("env_key")
    return {"providers": out, "effort_levels": list(EFFORT_LEVELS)}


# ---------------------------------------------------------------- storage

def _row(r) -> dict:
    d = dict(r)
    d["config"] = json.loads(d["config"] or "{}")
    d["secrets"] = json.loads(d["secrets"] or "{}")
    d["enabled"] = bool(d["enabled"])
    d["is_default"] = bool(d["is_default"])
    return d


def public(conn: dict) -> dict:
    """A connection without its secrets (what the browser sees)."""
    p = PROVIDERS.get(conn["provider"], {})
    method = p.get("methods", {}).get(conn["method"], {})
    secret_keys = [f["key"] for f in method.get("fields", []) if f.get("secret")]
    env_key = p.get("env_key")
    return {
        "id": conn["id"], "name": conn["name"], "provider": conn["provider"], "method": conn["method"],
        "provider_label": p.get("label", conn["provider"]), "method_label": method.get("label", conn["method"]),
        "short": p.get("short", conn["provider"]), "color": p.get("color", "#677180"),
        "config": conn["config"], "enabled": conn["enabled"], "is_default": conn["is_default"],
        "model": conn["config"].get("model") or p.get("default_model", ""),
        "secrets_set": {k: bool(conn["secrets"].get(k)) for k in secret_keys},
        "uses_env_key": bool("api_key" in secret_keys and not conn["secrets"].get("api_key")
                             and env_key and os.environ.get(env_key)),
        "updated_at": conn["updated_at"],
    }


def list_all() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM ai_connections ORDER BY is_default DESC, name COLLATE NOCASE")]


def get(conn_id: int | None) -> dict | None:
    if conn_id is None:
        return None
    r = db.query_one("SELECT * FROM ai_connections WHERE id=?", (conn_id,))
    return _row(r) if r else None


def default() -> dict | None:
    r = db.query_one("SELECT * FROM ai_connections WHERE enabled=1 ORDER BY is_default DESC, id LIMIT 1")
    return _row(r) if r else None


def resolve(conn_id: int | None) -> dict | None:
    """The connection a chat should use: its own if still enabled, else the default."""
    c = get(conn_id)
    return c if c and c["enabled"] else default()


def any_enabled() -> bool:
    return db.query_one("SELECT 1 FROM ai_connections WHERE enabled=1 LIMIT 1") is not None


def _clean(data: dict, existing: dict | None = None, consume: bool = True) -> tuple[dict, dict, dict]:
    """Validate a create/update request. Returns (fields, config, secrets).

    consume: swap a Google sign-in "grant:<id>" for the real token for good (saving),
    or only look at it (Test / Load models before saving)."""
    provider = data.get("provider") or (existing or {}).get("provider")
    if provider not in PROVIDERS:
        raise ConnectionError_("Choose a provider.")
    p = PROVIDERS[provider]
    method = data.get("method") or (existing or {}).get("method") or next(iter(p["methods"]))
    if method not in p["methods"]:
        raise ConnectionError_("Choose a connection method.")
    m = p["methods"][method]
    name = " ".join(str(data.get("name") or "").split()) or (existing or {}).get("name") or p["short"]
    if len(name) > 60:
        raise ConnectionError_("The name can be at most 60 characters.")

    old_cfg = dict((existing or {}).get("config") or {})
    old_sec = dict((existing or {}).get("secrets") or {}) if existing and existing["provider"] == provider \
        and existing["method"] == method else {}
    incoming = data.get("config") or {}
    incoming_secrets = data.get("secrets") or {}
    clear = set(data.get("clear_secrets") or [])

    config: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    same_method = bool(old_sec) or (existing and existing["provider"] == provider and existing["method"] == method)
    for k in m.get("extra_config", []):   # set by the app, not typed by the user (e.g. the signed-in account)
        if same_method and old_cfg.get(k):
            config[k] = old_cfg[k]
    for f in m["fields"]:
        k = f["key"]
        if f.get("type") == "google_signin":
            v = incoming_secrets.get(k)
            if v:
                from . import google_oauth
                g = google_oauth.take_grant(v) if consume else google_oauth.peek_grant(v)
                if not g:
                    raise ConnectionError_("The Google sign-in expired. Click “Sign in with Google” again.")
                if g["client_id"] != (incoming.get("google_client_id") or old_cfg.get("google_client_id")):
                    raise ConnectionError_("The OAuth client ID changed after signing in. Sign in with Google again.")
                secrets[k] = g["refresh_token"]
                config["google_account"] = g["email"]
            elif k in old_sec and k not in clear:
                secrets[k] = old_sec[k]
            else:
                config.pop("google_account", None)
            continue
        if f.get("secret"):
            v = incoming_secrets.get(k)
            v = v.strip() if isinstance(v, str) else v
            if v:
                secrets[k] = v
            elif k in old_sec and k not in clear:
                secrets[k] = old_sec[k]
        else:
            v = incoming.get(k, old_cfg.get(k, f.get("default", "")))
            config[k] = (str(v).strip() if v is not None else "")
        if f.get("type") == "url" and config.get(k) and not re.match(r"^https?://", config[k]):
            raise ConnectionError_(f"{f['label']} must start with http:// or https://")
        if f["key"] == "credentials_json" and secrets.get(k):
            try:
                cj = json.loads(secrets[k])
                assert isinstance(cj, dict) and cj.get("type")
            except Exception:
                raise ConnectionError_("The service account key must be the JSON key file's contents.")
    # required fields (an API key may also come from the server's environment)
    env_ok = bool(p.get("env_key") and os.environ.get(p["env_key"]))
    for f in m["fields"]:
        if not f.get("required"):
            continue
        have = secrets.get(f["key"]) if f.get("secret") else config.get(f["key"])
        if not have and not (f["key"] == "api_key" and env_ok):
            if f.get("type") == "google_signin":
                raise ConnectionError_("Sign in with Google first.")
            raise ConnectionError_(f"{f['label']} is required.")

    model = str(incoming.get("model", old_cfg.get("model", p.get("default_model", ""))) or "").strip()
    if not model:
        raise ConnectionError_(f"{m.get('model_label', 'Model')} is required.")
    if len(model) > 200 or any(c.isspace() for c in model):
        raise ConnectionError_("Invalid model name.")
    config["model"] = model
    if p.get("supports_effort"):
        effort = incoming.get("effort", old_cfg.get("effort", "high")) or "high"
        if effort not in EFFORT_LEVELS:
            raise ConnectionError_("Invalid effort level.")
        config["effort"] = effort
    fields = {"name": name, "provider": provider, "method": method,
              "enabled": bool(data.get("enabled", (existing or {}).get("enabled", True)))}
    return fields, config, secrets


def create(data: dict) -> dict:
    fields, config, secrets = _clean(data)
    now = time.time()
    cid = db.execute(
        "INSERT INTO ai_connections (name, provider, method, config, secrets, enabled, is_default, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,0,?,?)",
        (fields["name"], fields["provider"], fields["method"], json.dumps(config), json.dumps(secrets),
         int(fields["enabled"]), now, now))
    if data.get("is_default") or not db.query_one("SELECT 1 FROM ai_connections WHERE is_default=1"):
        set_default(cid)
    return get(cid)


def update(conn_id: int, data: dict) -> dict:
    existing = get(conn_id)
    if not existing:
        raise ConnectionError_("That connection no longer exists.")
    fields, config, secrets = _clean(data, existing)
    db.execute(
        "UPDATE ai_connections SET name=?, provider=?, method=?, config=?, secrets=?, enabled=?, updated_at=? WHERE id=?",
        (fields["name"], fields["provider"], fields["method"], json.dumps(config), json.dumps(secrets),
         int(fields["enabled"]), time.time(), conn_id))
    if data.get("is_default"):
        set_default(conn_id)
    return get(conn_id)


def draft(data: dict, conn_id: int | None = None) -> dict:
    """An unsaved connection (for Test / Load models in the editor), using saved secrets for blanks."""
    existing = get(conn_id) if conn_id else None
    fields, config, secrets = _clean(data, existing, consume=False)
    return {"id": conn_id or 0, **fields, "config": config, "secrets": secrets, "is_default": False,
            "created_at": 0, "updated_at": 0}


def delete(conn_id: int) -> None:
    was_default = db.query_one("SELECT is_default FROM ai_connections WHERE id=?", (conn_id,))
    db.execute("UPDATE conversations SET connection_id=NULL WHERE connection_id=?", (conn_id,))
    db.execute("DELETE FROM ai_connections WHERE id=?", (conn_id,))
    if was_default and was_default["is_default"]:
        nxt = db.query_one("SELECT id FROM ai_connections WHERE enabled=1 ORDER BY id LIMIT 1")
        if nxt:
            set_default(nxt["id"])


def set_default(conn_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE ai_connections SET is_default=0")
        c.execute("UPDATE ai_connections SET is_default=1, enabled=1 WHERE id=?", (conn_id,))


def secret(conn: dict, key: str) -> str | None:
    """A secret, falling back to the provider's environment variable for API keys."""
    v = conn["secrets"].get(key)
    if v:
        return v
    if key == "api_key":
        env_key = PROVIDERS.get(conn["provider"], {}).get("env_key")
        if env_key:
            return (os.environ.get(env_key) or "").strip() or None
    return None


# ---------------------------------------------------------------- first run: migrate the old Claude settings

def migrate_legacy() -> None:
    """Create a "Claude" connection from the pre-connections settings (Admin → Claude API)."""
    if db.query_one("SELECT 1 FROM ai_connections LIMIT 1") or db.get_setting("ai_connections_migrated"):
        return
    key = db.get_setting("anthropic_api_key")
    env_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key or env_key:
        config = {"model": db.get_setting("model") or settings.model,
                  "effort": db.get_setting("effort") or settings.effort}
        if config["effort"] not in EFFORT_LEVELS:
            config["effort"] = "high"
        now = time.time()
        db.execute(
            "INSERT INTO ai_connections (name, provider, method, config, secrets, enabled, is_default, created_at, updated_at) "
            "VALUES ('Claude','anthropic','api_key',?,?,1,1,?,?)",
            (json.dumps(config), json.dumps({"api_key": key} if key else {}), now, now))
    db.set_setting("ai_connections_migrated", "1")
