"""Talking to the AI behind a connection.

Conversations are stored in Anthropic's message format (content blocks: text, thinking,
tool_use, tool_result). Two adapters turn that history into a reply:

  * AnthropicProvider     – Claude via the Anthropic API, Google Cloud Vertex AI or Amazon
                            Bedrock (Anthropic SDK). Uses Anthropic's built-in bash and text
                            editor tools, adaptive thinking and effort.
  * OpenAICompatProvider  – OpenAI (ChatGPT), Azure OpenAI, GitHub Models, xAI Grok, Groq,
                            Gemini, Mistral, DeepSeek, OpenRouter, Ollama and any other
                            OpenAI-compatible chat API. The same three tools are offered as
                            function definitions; history is translated on every call, so one
                            chat can switch between providers.

Each adapter streams events to the browser through `send` (same events as before) and
returns a TurnResult with the assistant's content blocks and why it stopped.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import anthropic

from . import ai_connections as ai
from .config import settings

Send = Callable[[dict], Awaitable[None]]
Stopped = Callable[[], bool]


class ProviderError(Exception):
    """A user-facing error message from talking to an AI service."""


@dataclass
class TurnResult:
    content: list[dict]
    stop: str                 # end_turn | tool_use | max_tokens | refusal | pause_turn
    note: str | None = None   # e.g. a refusal explanation
    usage: dict | None = None  # input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    rate: dict | None = None   # rate-limit snapshot from the response headers


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def rate_from_headers(headers) -> dict | None:
    """Rate limits the service reported (Anthropic `anthropic-ratelimit-*`, OpenAI-style `x-ratelimit-*`)."""
    if not headers:
        return None
    get = lambda k: headers.get(k)  # noqa: E731
    out: dict[str, Any] = {}
    if get("anthropic-ratelimit-requests-limit") or get("anthropic-ratelimit-tokens-limit") \
            or get("anthropic-ratelimit-input-tokens-limit"):
        for kind in ("requests", "tokens", "input-tokens", "output-tokens"):
            key = kind.replace("-", "_")
            lim, rem = _int(get(f"anthropic-ratelimit-{kind}-limit")), _int(get(f"anthropic-ratelimit-{kind}-remaining"))
            if lim is not None:
                out[key] = {"limit": lim, "remaining": rem, "reset": get(f"anthropic-ratelimit-{kind}-reset")}
    else:
        for kind in ("requests", "tokens"):
            lim, rem = _int(get(f"x-ratelimit-limit-{kind}")), _int(get(f"x-ratelimit-remaining-{kind}"))
            if lim is not None:
                out[kind] = {"limit": lim, "remaining": rem, "reset": get(f"x-ratelimit-reset-{kind}")}
    return out or None


# ---------------------------------------------------------------- tools

ANTHROPIC_TOOLS: list[dict] = [
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
    {
        "name": "restart_app",
        "description": (
            "Restart the SysAdmin.IA web application (this app) so that changes to its "
            "Python backend take effect. Call this only after you have modified files under the app "
            "directory, verified them (e.g. `python -m py_compile`), and committed them with git. "
            "The app first checks that the code still imports; if not, the restart is refused and "
            "the error is returned. The restart happens after your current reply finishes. "
            "Changes to static frontend files (app/static/) do NOT need a restart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Short summary of what changed."}},
            "required": ["reason"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
]

# The same tools as plain function definitions, for OpenAI-compatible APIs.
FUNCTION_TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "bash",
        "description": ("Run a command in a persistent bash shell on the server (working directory and "
                        "variables persist between calls). Stdin is closed: use non-interactive flags. "
                        "Set restart=true (without a command) to restart the shell if it hangs."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The bash command to run."},
            "restart": {"type": "boolean", "description": "Restart the shell session instead of running a command."},
        }},
    }},
    {"type": "function", "function": {
        "name": "str_replace_based_edit_tool",
        "description": ("View, create and edit text files by absolute path. Commands: "
                        "view (path, optional view_range [start, end] of line numbers; also lists directories), "
                        "create (path, file_text), "
                        "str_replace (path, old_str = exact unique text to replace, new_str), "
                        "insert (path, insert_line = line number to insert after, 0 for the top, insert_text)."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
            "path": {"type": "string", "description": "Absolute path of the file or directory."},
            "file_text": {"type": "string", "description": "Content of the new file (create)."},
            "old_str": {"type": "string", "description": "Exact text to replace; must occur exactly once (str_replace)."},
            "new_str": {"type": "string", "description": "Replacement text (str_replace)."},
            "insert_line": {"type": "integer", "description": "Insert after this line number (insert)."},
            "insert_text": {"type": "string", "description": "Text to insert (insert)."},
            "view_range": {"type": "array", "items": {"type": "integer"}, "description": "[start_line, end_line] (view)."},
        }, "required": ["command", "path"]},
    }},
    {"type": "function", "function": {
        "name": "restart_app",
        "description": ANTHROPIC_TOOLS[2]["description"],
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Short summary of what changed."}}, "required": ["reason"]},
    }},
]

_ANTHROPIC_ONLY = {"thinking", "redacted_thinking", "fallback", "server_tool_use"}


def _credential_hint(conn: dict, e: Exception) -> str:
    """A readable message for Google / AWS credential problems."""
    text = f"{type(e).__name__}: {e}"
    if "invalid_grant" in text or "RefreshError" in text:
        return (f"The Google sign-in for “{conn['name']}” is no longer valid (expired or revoked). "
                "Edit the connection and click Sign in with Google again.")
    if "DefaultCredentialsError" in text:
        return (f"“{conn['name']}” has no Google credentials: paste a service-account key, or sign the server in "
                "with `gcloud auth application-default login` in the Console.")
    if "NoCredentialsError" in text or "Unable to locate credentials" in text:
        return f"“{conn['name']}” has no AWS credentials: enter an access key, or configure the server's AWS credentials."
    return f"“{conn['name']}”: {text}"


def _safe_id(tool_id: str) -> str:
    """Anthropic tool ids must match ^[a-zA-Z0-9_-]+$ (ids from other providers may not)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_id or "") or "call"


# ---------------------------------------------------------------- Anthropic (API key / Vertex / Bedrock)

class AnthropicProvider:
    def __init__(self, conn: dict) -> None:
        self.conn = conn
        self.cfg = conn["config"]
        self.method = conn["method"]

    @property
    def model(self) -> str:
        return self.cfg.get("model") or ai.PROVIDERS["anthropic"]["default_model"]

    def client(self):
        if self.method == "api_key":
            key = ai.secret(self.conn, "api_key")
            if not key:
                raise ProviderError(f"Connection “{self.conn['name']}” has no API key. Add one in AI connections.")
            return anthropic.AsyncAnthropic(api_key=key)
        if self.method in ("vertex", "google_account"):
            try:
                from anthropic import AsyncAnthropicVertex
            except ImportError:
                raise ProviderError("Google Cloud support isn't installed on the server (pip package google-auth). "
                                    "Install the latest update to add it.")
            kwargs: dict[str, Any] = {"project_id": self.cfg.get("project_id"), "region": self.cfg.get("region") or "global"}
            if self.method == "google_account":
                refresh = self.conn["secrets"].get("google_refresh_token")
                if not refresh:
                    raise ProviderError(f"“{self.conn['name']}” isn't signed in to Google. Edit it and click Sign in with Google.")
                try:
                    from google.oauth2.credentials import Credentials
                except ImportError:
                    raise ProviderError("Google Cloud support isn't installed on the server (pip package google-auth).")
                from .google_oauth import CLOUD_SCOPE, TOKEN_URL
                kwargs["credentials"] = Credentials(
                    token=None, refresh_token=refresh, token_uri=TOKEN_URL,
                    client_id=self.cfg.get("google_client_id"),
                    client_secret=self.conn["secrets"].get("google_client_secret"), scopes=[CLOUD_SCOPE])
                return AsyncAnthropicVertex(**kwargs)
            creds_json = self.conn["secrets"].get("credentials_json")
            if creds_json:
                try:
                    from google.oauth2 import service_account
                except ImportError:
                    raise ProviderError("Google Cloud support isn't installed on the server (pip package google-auth).")
                info = json.loads(creds_json)
                kwargs["credentials"] = service_account.Credentials.from_service_account_info(
                    info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            return AsyncAnthropicVertex(**kwargs)
        if self.method == "bedrock":
            try:
                from anthropic import AsyncAnthropicBedrock
            except ImportError:
                raise ProviderError("Amazon Bedrock support isn't installed on the server (pip package boto3). "
                                    "Install the latest update to add it.")
            kwargs = {"aws_region": self.cfg.get("aws_region") or "us-east-1"}
            if self.cfg.get("aws_access_key") and self.conn["secrets"].get("aws_secret_key"):
                kwargs["aws_access_key"] = self.cfg["aws_access_key"]
                kwargs["aws_secret_key"] = self.conn["secrets"]["aws_secret_key"]
            return AsyncAnthropicBedrock(**kwargs)
        raise ProviderError(f"Unknown connection method: {self.method}")

    @staticmethod
    def prepare(messages: list[dict]) -> list[dict]:
        """Drop blocks from other providers (e.g. "reasoning") and fix foreign tool ids."""
        out = []
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append(m)
                continue
            blocks = []
            for b in c:
                t = b.get("type")
                if t == "reasoning":
                    continue
                if t == "tool_use":
                    b = {**b, "id": _safe_id(b["id"])}
                elif t == "tool_result":
                    b = {**b, "tool_use_id": _safe_id(b["tool_use_id"])}
                blocks.append(b)
            if blocks:
                out.append({"role": m["role"], "content": blocks})
        return out

    async def turn(self, messages: list[dict], system: str, send: Send, stopped: Stopped) -> TurnResult | None:
        client = self.client()
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=settings.max_tokens, system=system, tools=ANTHROPIC_TOOLS,
            messages=self.prepare(messages), thinking={"type": "adaptive", "display": "summarized"},
            cache_control={"type": "ephemeral"},
        )
        if self.method == "api_key":  # first-party API features
            kwargs["output_config"] = {"effort": self.cfg.get("effort") or "high"}
            if settings.fallbacks:
                kwargs["betas"] = ["server-side-fallback-2026-07-01"]
                kwargs["fallbacks"] = settings.fallbacks
        try:
            try:
                manager = client.beta.messages.stream(**kwargs)
            except TypeError:
                if "fallbacks" not in kwargs:
                    raise
                kwargs["extra_body"] = {"fallbacks": kwargs.pop("fallbacks")}
                manager = client.beta.messages.stream(**kwargs)
            async with manager as stream:
                async for event in stream:
                    if stopped():
                        return None
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            await send({"type": "tool_preparing", "name": block.name})
                        elif block.type == "thinking":
                            await send({"type": "thinking_start"})
                        elif block.type == "text":
                            await send({"type": "text_start"})
                        elif block.type == "fallback":
                            await send({"type": "notice", "message": "Request was rerouted to a fallback model."})
                    elif event.type == "content_block_delta":
                        d = event.delta
                        if d.type == "text_delta":
                            await send({"type": "text_delta", "text": d.text})
                        elif d.type == "thinking_delta" and d.thinking:
                            await send({"type": "thinking_delta", "text": d.thinking})
                response = await stream.get_final_message()
                headers = getattr(getattr(stream, "response", None), "headers", None)
        except anthropic.AuthenticationError:
            raise ProviderError(f"“{self.conn['name']}” was rejected: the API key or credentials are invalid. Check it in AI connections.")
        except anthropic.PermissionDeniedError as e:
            raise ProviderError(f"“{self.conn['name']}” isn't allowed to use this model: {e.message}")
        except anthropic.NotFoundError:
            raise ProviderError(f"Model “{self.model}” was not found for “{self.conn['name']}”. Check the model in AI connections.")
        except anthropic.RateLimitError:
            raise ProviderError(f"“{self.conn['name']}” is rate limited. Try again shortly.")
        except anthropic.BadRequestError as e:
            raise ProviderError(f"The AI service rejected the request: {e.message}")
        except anthropic.APIStatusError as e:
            raise ProviderError(f"AI service error ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            raise ProviderError(f"Could not reach the AI service for “{self.conn['name']}” (network error).")
        except ProviderError:
            raise
        except Exception as e:  # e.g. Google/AWS credential errors
            raise ProviderError(_credential_hint(self.conn, e))

        content = [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in response.content]
        stop = response.stop_reason or "end_turn"
        note = None
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            note = getattr(details, "explanation", None) if details else None
        u = getattr(response, "usage", None)
        usage = {
            "input_tokens": _int(getattr(u, "input_tokens", 0)) or 0,
            "output_tokens": _int(getattr(u, "output_tokens", 0)) or 0,
            "cache_read_tokens": _int(getattr(u, "cache_read_input_tokens", 0)) or 0,
            "cache_write_tokens": _int(getattr(u, "cache_creation_input_tokens", 0)) or 0,
        } if u is not None else None
        return TurnResult(content, stop, note, usage, rate_from_headers(headers))

    async def test(self) -> str:
        client = self.client()
        try:
            if self.method == "api_key":
                info = await client.models.retrieve(self.model)
                return f"Connected. Model: {info.display_name} ({info.id})."
            await client.messages.create(model=self.model, max_tokens=1,
                                         messages=[{"role": "user", "content": "Reply with OK."}])
        except anthropic.AuthenticationError:
            raise ProviderError("The key or credentials were rejected.")
        except anthropic.PermissionDeniedError as e:
            raise ProviderError(f"Signed in, but not allowed to use this model: {e.message} "
                                "(Google Cloud: the account needs the “Vertex AI User” role and Claude enabled in the Model Garden.)")
        except anthropic.NotFoundError:
            raise ProviderError(f"Model “{self.model}” was not found. Use Load models or check the model ID for this platform/region.")
        except anthropic.APIStatusError as e:
            raise ProviderError(f"AI service error ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            raise ProviderError("Could not reach the AI service from the server (network error).")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(_credential_hint(self.conn, e))
        return f"Connected. Model {self.model} answered."

    async def complete(self, prompt: str, system: str = "") -> tuple[str, dict | None]:
        """A single question/answer without tools (used by the "Ask the AI" job action)."""
        try:
            r = await self.client().messages.create(model=self.model, max_tokens=2000, system=system or anthropic.NOT_GIVEN,
                                                    messages=[{"role": "user", "content": prompt}])
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(_credential_hint(self.conn, e) if not hasattr(e, "status_code") else f"AI service error: {e}")
        text = "".join(getattr(b, "text", "") for b in r.content).strip()
        u = r.usage
        return text, {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                      "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}

    async def models(self) -> list[str]:
        if self.method != "api_key":
            return ai.PROVIDERS["anthropic"]["models"]
        page = await self.client().models.list(limit=100)
        return [m.id for m in page.data]


# ---------------------------------------------------------------- OpenAI-compatible

def _id_mapper(provider: str) -> Callable[[str], str]:
    if provider == "mistral":  # Mistral requires 9-character alphanumeric tool call ids
        return lambda i: hashlib.sha1((i or "").encode()).hexdigest()[:9]
    return lambda i: (i or "call")[:64]


def to_openai(messages: list[dict], system: str, idmap: Callable[[str], str]) -> list[dict]:
    """Anthropic-format history → OpenAI chat messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        if m["role"] == "user":
            texts = []
            for b in c:
                t = b.get("type")
                if t == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    content = content or "(no output)"
                    if b.get("is_error"):
                        content = "Error: " + content
                    out.append({"role": "tool", "tool_call_id": idmap(b["tool_use_id"]), "content": content})
                elif t == "text" and b.get("text"):
                    texts.append(b["text"])
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
        else:
            texts = [b["text"] for b in c if b.get("type") == "text" and b.get("text")]
            calls = [{"id": idmap(b["id"]), "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in c if b.get("type") == "tool_use"]
            if not texts and not calls:
                continue  # e.g. only thinking blocks
            msg: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


class OpenAICompatProvider:
    def __init__(self, conn: dict) -> None:
        self.conn = conn
        self.cfg = conn["config"]
        self.p = ai.PROVIDERS[conn["provider"]]

    @property
    def model(self) -> str:
        return self.cfg.get("model") or self.p.get("default_model", "")

    def client(self):
        try:
            import openai
        except ImportError:
            raise ProviderError("Support for this provider isn't installed on the server (pip package openai). "
                                "Install the latest update to add it.")
        key = ai.secret(self.conn, "api_key")
        if self.conn["provider"] == "azure":
            return openai.AsyncAzureOpenAI(api_key=key, azure_endpoint=self.cfg.get("endpoint"),
                                           api_version=self.cfg.get("api_version") or "2024-10-21", max_retries=1)
        base = self.cfg.get("base_url") or self.p.get("base_url")
        if self.conn["provider"] == "ollama_managed":
            from .local_ai import OLLAMA_URL
            base = f"{OLLAMA_URL}/v1"
            if self.cfg.get("device"):      # a model on a device (Server → Devices), through an SSH tunnel
                from . import devices
                try:
                    base = f"{devices.ollama_url(self.cfg['device'])}/v1"
                except devices.DeviceError as e:
                    raise ProviderError(f"“{self.conn['name']}” runs on a device that can't be reached: {e}")
        headers = {}
        if self.conn["provider"] == "openrouter":
            headers = {"X-Title": "SysAdmin.IA"}
        return openai.AsyncOpenAI(api_key=key or "not-needed", base_url=base,
                                  organization=self.cfg.get("organization") or None,
                                  default_headers=headers or None, max_retries=1)

    def _error(self, e: Exception) -> ProviderError:
        import openai
        name = self.conn["name"]
        if isinstance(e, openai.AuthenticationError):
            return ProviderError(f"“{name}” was rejected: the API key or token is invalid. Check it in AI connections.")
        if isinstance(e, openai.PermissionDeniedError):
            return ProviderError(f"“{name}” isn't allowed to do this: {getattr(e, 'message', e)}")
        if isinstance(e, openai.NotFoundError):
            return ProviderError(f"Model “{self.model}” was not found for “{name}”. Check the model in AI connections.")
        if isinstance(e, openai.RateLimitError):
            return ProviderError(f"“{name}” is rate limited or out of quota. Try again later.")
        if isinstance(e, openai.BadRequestError):
            msg = getattr(e, "message", str(e))
            if "tool" in msg.lower() or "function" in msg.lower():
                msg += " (This model may not support tool calling, which the assistant needs.)"
            return ProviderError(f"The AI service rejected the request: {msg}")
        if isinstance(e, openai.APIStatusError):
            return ProviderError(f"AI service error ({e.status_code}): {getattr(e, 'message', e)}")
        if isinstance(e, openai.APIConnectionError):
            if self.conn["provider"] == "ollama_managed":
                return ProviderError("Ollama isn't running on this server. Start it in Local AI.")
            return ProviderError(f"Could not reach the AI service for “{name}” (network error or wrong address).")
        return ProviderError(f"“{name}”: {type(e).__name__}: {e}")

    async def turn(self, messages: list[dict], system: str, send: Send, stopped: Stopped) -> TurnResult | None:
        client = self.client()
        idmap = _id_mapper(self.conn["provider"])
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        usage = None
        headers = None
        try:
            stream, headers = await self._open_stream(client, model=self.model, tools=FUNCTION_TOOLS, stream=True,
                                                      messages=to_openai(messages, system, idmap))
            async for chunk in stream:
                if stopped():
                    await stream.close()
                    return None
                u = getattr(chunk, "usage", None)
                if u is not None:  # sent at the end of the stream when include_usage is supported
                    prompt = _int(getattr(u, "prompt_tokens", 0)) or 0
                    details = getattr(u, "prompt_tokens_details", None)
                    cached = _int(getattr(details, "cached_tokens", 0)) or 0 if details is not None else 0
                    usage = {"input_tokens": max(0, prompt - cached), "cache_read_tokens": cached,
                             "output_tokens": _int(getattr(u, "completion_tokens", 0)) or 0, "cache_write_tokens": 0}
                if not chunk.choices:
                    continue
                ch = chunk.choices[0]
                d = ch.delta
                if d is not None:
                    r = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None)
                    if isinstance(r, str) and r:
                        if not reasoning:
                            await send({"type": "thinking_start"})
                        reasoning.append(r)
                        await send({"type": "thinking_delta", "text": r})
                    if d.content:
                        if not text:
                            await send({"type": "text_start"})
                        text.append(d.content)
                        await send({"type": "text_delta", "text": d.content})
                    for tc in d.tool_calls or []:
                        idx = tc.index if tc.index is not None else len(calls)
                        entry = calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                        if tc.id:
                            entry["id"] = tc.id
                        fn = tc.function
                        if fn is not None:
                            if fn.name and not entry["name"]:
                                entry["name"] = fn.name
                                await send({"type": "tool_preparing", "name": fn.name})
                            if fn.arguments:
                                entry["args"] += fn.arguments
                if ch.finish_reason:
                    finish = ch.finish_reason
        except ProviderError:
            raise
        except Exception as e:
            raise self._error(e)

        content: list[dict] = []
        if reasoning:
            content.append({"type": "reasoning", "text": "".join(reasoning)})
        if text:
            content.append({"type": "text", "text": "".join(text)})
        for idx in sorted(calls):
            c = calls[idx]
            try:
                inp = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                inp = {"_invalid_json": c["args"][:2000]}
            content.append({"type": "tool_use", "id": _safe_id(c["id"] or f"call_{uuid.uuid4().hex[:24]}"),
                            "name": c["name"] or "unknown", "input": inp})
        if any(b["type"] == "tool_use" for b in content):
            stop = "tool_use"
        elif finish == "length":
            stop = "max_tokens"
        elif finish == "content_filter":
            stop = "refusal"
        else:
            stop = "end_turn"
        if not content:
            content = [{"type": "text", "text": "(The model returned an empty reply.)"}]
        return TurnResult(content, stop, usage=usage, rate=rate_from_headers(headers))

    # Services that rejected stream_options (so usage isn't requested from them again).
    _no_stream_usage: set[str] = set()

    async def _open_stream(self, client, **kwargs):
        """Start a streamed completion; returns (stream, response headers).

        Asks for token usage in the stream (stream_options.include_usage) and falls back
        without it for services that don't accept the option."""
        import inspect

        import openai
        key = f"{self.conn['provider']}|{self.cfg.get('base_url') or self.p.get('base_url') or self.cfg.get('endpoint')}"
        if key not in self._no_stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        try:
            raw = await client.chat.completions.with_raw_response.create(**kwargs)
        except openai.BadRequestError as e:
            if "stream_options" not in kwargs or "stream" not in str(e).lower():
                raise
            self._no_stream_usage.add(key)
            kwargs.pop("stream_options")
            raw = await client.chat.completions.with_raw_response.create(**kwargs)
        stream = raw.parse()
        if inspect.isawaitable(stream):
            stream = await stream
        return stream, getattr(raw, "headers", None)

    async def test(self) -> str:
        client = self.client()
        try:
            # No max_tokens: newer reasoning models reject it (they want max_completion_tokens).
            r = await client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": "Reply with just: OK"}])
            reply = (r.choices[0].message.content or "").strip() if r.choices else ""
        except Exception as e:
            raise self._error(e)
        return f"Connected. {self.model} answered{': ' + reply[:40] if reply else ''}."

    async def complete(self, prompt: str, system: str = "") -> tuple[str, dict | None]:
        """A single question/answer without tools (used by the "Ask the AI" job action)."""
        client = self.client()
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        try:
            r = await client.chat.completions.create(model=self.model, messages=msgs)
        except Exception as e:
            raise self._error(e)
        text = (r.choices[0].message.content or "").strip() if r.choices else ""
        u = getattr(r, "usage", None)
        usage = None
        if u is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            usage = {"input_tokens": max(0, (u.prompt_tokens or 0) - cached), "cache_read_tokens": cached,
                     "output_tokens": u.completion_tokens or 0, "cache_write_tokens": 0}
        return text, usage

    async def models(self) -> list[str]:
        client = self.client()
        try:
            page = await client.models.list()
            ids = [m.id for m in page.data]
        except Exception as e:
            raise self._error(e)
        return sorted(ids)


def for_connection(conn: dict):
    kind = ai.PROVIDERS.get(conn["provider"], {}).get("kind")
    if kind == "anthropic":
        return AnthropicProvider(conn)
    if kind == "openai":
        return OpenAICompatProvider(conn)
    raise ProviderError(f"Unknown AI provider: {conn['provider']}")
