import json
import os
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.http import (
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .tools import ICONS, TOOLS

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
NO_KEY_MESSAGE = (
    "The assistant isn't wired up yet. Set ANTHROPIC_API_KEY (as an environment "
    "variable or a Django setting) and I'll be able to answer here."
)


# --------------------------------------------------------------------------- #
#  Launchpad page
# --------------------------------------------------------------------------- #
def _href(tool):
    """Resolve a tile's link, safely falling back so the page never 500s."""
    url_name = tool.get("url_name")
    if url_name:
        try:
            return reverse(url_name)
        except NoReverseMatch:
            pass
    return tool.get("url") or "/{}/".format(tool["slug"])


def _decorate(tool):
    return {
        **tool,
        "icon": ICONS.get(tool.get("icon", tool["slug"]), ""),
        "href": _href(tool),
        "search": "{name} {sub}".format(**tool).lower(),
    }


@ensure_csrf_cookie  # sets the csrftoken cookie so the chat POST can send it
def home(request):
    return render(request, "launchpad/home.html", {"tools": [_decorate(t) for t in TOOLS]})


# --------------------------------------------------------------------------- #
#  Assistant  (called when a search finds no tool)
# --------------------------------------------------------------------------- #
class LLMError(Exception):
    pass


def _api_key():
    return getattr(settings, "ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")


def _model():
    return getattr(settings, "LAUNCHPAD_ASSISTANT_MODEL", "claude-sonnet-4-6")


RATE_MESSAGE = "Rate limit reached. Try again in about {} seconds."


def _rate_limited(request):
    """Fixed-window rate limit per authenticated user (or client IP).

    Returns seconds-to-wait when over the limit, 0 when allowed. Configure via
    LAUNCHPAD_ASSISTANT_RATE_LIMIT (requests, default 20; 0 disables) and
    LAUNCHPAD_ASSISTANT_RATE_WINDOW (seconds, default 300). Uses the default
    Django cache — for multi-worker deployments use a shared backend
    (Redis/Memcached) so the limit is enforced globally.
    """
    limit = getattr(settings, "LAUNCHPAD_ASSISTANT_RATE_LIMIT", 20)
    window = getattr(settings, "LAUNCHPAD_ASSISTANT_RATE_WINDOW", 300)
    if not limit or limit <= 0:
        return 0
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        ident = "u:{}".format(user.pk)
    else:
        ident = "ip:{}".format(request.META.get("REMOTE_ADDR", "unknown"))
    bucket = int(time.time() // window)
    key = "launchpad:ask:{}:{}".format(ident, bucket)
    if cache.add(key, 1, timeout=window):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:  # key expired between add and incr
            cache.add(key, 1, timeout=window)
            count = 1
    if count > limit:
        return max(1, window - int(time.time() % window))
    return 0


def _system_prompt():
    catalogue = "\n".join("- {name}: {sub}".format(**t) for t in TOOLS)
    return (
        "You are the Integration Ops assistant embedded in a Bosch SAP CPI "
        "operations portal. A user searched the tool launchpad and no tool "
        "matched their query. Here are the tools that DO exist:\n\n"
        "{}\n\n"
        "If one of these tools fits the user's need, recommend it by name and "
        "briefly say what it does. If none fit, answer their SAP CPI / "
        "integration question directly and concisely. Prefer short, practical "
        "answers with markdown formatting where helpful. Never invent tools "
        "that are not in the list above."
    ).format(catalogue)


def _clean_messages(raw):
    """Whitelist roles/content and cap size before sending upstream."""
    out = []
    for m in raw or []:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": str(content)[:4000]})
    return out[-20:]


def _payload(messages, stream=False):
    body = {
        "model": _model(),
        "max_tokens": 1024,
        "system": _system_prompt(),
        "messages": messages,
    }
    if stream:
        body["stream"] = True
    return json.dumps(body).encode()


def _headers(stream=False):
    h = {
        "content-type": "application/json",
        "x-api-key": _api_key(),
        "anthropic-version": "2023-06-01",
    }
    if stream:
        h["accept"] = "text/event-stream"
    return h


# ---- non-streaming (kept as a simple fallback: {messages} -> {reply}) ------ #
def _call_llm(messages):
    if not _api_key():
        return NO_KEY_MESSAGE
    req = urllib.request.Request(
        ANTHROPIC_URL, data=_payload(messages), method="POST", headers=_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise LLMError("Assistant API error ({}).".format(exc.code))
    except urllib.error.URLError:
        raise LLMError("Couldn't reach the assistant service.")
    text = "".join(
        b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"
    ).strip()
    return text or "(the assistant returned no text)"


@require_POST
def ask(request):
    retry = _rate_limited(request)
    if retry:
        return JsonResponse({"error": RATE_MESSAGE.format(retry)}, status=429)
    try:
        data = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Invalid JSON")
    messages = _clean_messages(data.get("messages"))
    if not messages:
        return HttpResponseBadRequest("No messages provided")
    try:
        reply = _call_llm(messages)
    except LLMError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"reply": reply})


# ---- streaming (token-by-token via Server-Sent Events) --------------------- #
def _stream_llm(messages):
    """Yield text deltas from the Anthropic streaming API."""
    if not _api_key():
        yield NO_KEY_MESSAGE
        return
    req = urllib.request.Request(
        ANTHROPIC_URL, data=_payload(messages, stream=True), method="POST",
        headers=_headers(stream=True),
    )
    resp = urllib.request.urlopen(req, timeout=60)
    for raw in resp:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            evt = json.loads(data)
        except ValueError:
            continue
        etype = evt.get("type")
        if etype == "content_block_delta":
            delta = evt.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield delta["text"]
        elif etype == "message_stop":
            break
        elif etype == "error":
            raise LLMError((evt.get("error") or {}).get("message", "assistant error"))


@require_POST
def ask_stream(request):
    retry = _rate_limited(request)
    if retry:
        return JsonResponse({"error": RATE_MESSAGE.format(retry)}, status=429)
    try:
        data = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Invalid JSON")
    messages = _clean_messages(data.get("messages"))
    if not messages:
        return HttpResponseBadRequest("No messages provided")

    def sse():
        try:
            for chunk in _stream_llm(messages):
                if chunk:
                    yield "data: " + json.dumps({"t": chunk}) + "\n\n"
        except LLMError as exc:
            yield "data: " + json.dumps({"error": str(exc)}) + "\n\n"
        except urllib.error.HTTPError as exc:
            yield "data: " + json.dumps({"error": "Assistant API error ({}).".format(exc.code)}) + "\n\n"
        except urllib.error.URLError:
            yield "data: " + json.dumps({"error": "Couldn't reach the assistant service."}) + "\n\n"
        yield "data: " + json.dumps({"done": True}) + "\n\n"

    resp = StreamingHttpResponse(sse(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # tell nginx not to buffer the stream
    return resp
