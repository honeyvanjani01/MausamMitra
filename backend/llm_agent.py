"""
llm_agent.py
The conversational core of @MausamMitra, built on Google's Gemini API with
automatic function calling (AFC).

Gemini is given direct references to the real functions in weather_tools.py.
The SDK reads each function's name, type hints, and docstring to decide
when and how to call it — the model never invents a weather number itself;
every figure in a reply comes from an actual tool call made during that
turn.

v0.3 changes (speed + multilingual):
  - Uses the async Gemini client (`client.aio`) and async tool functions,
    so FastAPI's event loop is never blocked waiting on the model or on
    Open-Meteo.
  - Exposes `chat_stream`, which yields the reply incrementally (Gemini's
    native streaming) so the UI can show text as it's generated instead of
    waiting for the whole answer — this is the single biggest *perceived*
    speed win, independent of how long the model actually takes.
  - `get_weather_briefing` (see weather_tools.py) is listed first and the
    system prompt tells the model to prefer it, cutting a typical
    "what's the weather + should I irrigate" question from 3-4 tool-calling
    round trips down to 1.
  - Sessions now carry a `language` chosen at session start, and the system
    instruction asks Gemini to reply directly in that language — Gemini
    itself is multilingual, so this covers the "Indic language" requirement
    in the MVP without needing an external ASR/NMT service. Swap for
    Bhashini pre/post-processing later only if voice-quality Indic ASR is
    needed; text replies can stay on Gemini directly.
  - Old sessions are now evicted after a TTL so a long-running server
    doesn't leak memory across thousands of demo sessions.
"""

import logging
import os
import time
from google import genai
from google.genai import types

import weather_tools as wt

log = logging.getLogger("mausammitra.llm_agent")
logging.basicConfig(level=logging.INFO)

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

if not API_KEY:
    log.warning(
        "GEMINI_API_KEY is not set! Live chat requests will fail until it's "
        "set. Set it before starting uvicorn, e.g. $env:GEMINI_API_KEY=\"your-key\" "
        "(PowerShell) or export GEMINI_API_KEY=\"your-key\" (macOS/Linux). "
        "This is fine if you're only running with DEMO_MODE=true."
    )

# Created lazily (not at import time) so a missing/empty key doesn't crash
# the whole backend on startup — genai.Client() raises immediately on an
# empty key, which used to take the entire app down even when DEMO_MODE
# meant Gemini was never going to be called at all. Now the failure only
# happens if something actually tries to use Gemini without a key.
_client: "genai.Client | None" = None


def _get_client() -> "genai.Client":
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set — set it before sending a live "
                "chat message, or run with DEMO_MODE=true to skip Gemini entirely."
            )
        _client = genai.Client(api_key=API_KEY)
    return _client

LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "gu": "Gujarati", "mr": "Marathi",
    "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "bn": "Bengali",
    "pa": "Punjabi", "ml": "Malayalam", "or": "Odia", "as": "Assamese",
}

SESSION_TTL_SECONDS = 3600  # evict idle demo sessions after an hour


def _system_instruction(language: str) -> str:
    lang_name = LANGUAGE_NAMES.get(language, "English")
    lang_line = (
        "Always reply in English." if language == "en" else
        f"Always reply in {lang_name} (the user may type in English or {lang_name} — "
        f"either way, your reply text should be in {lang_name}), unless the user "
        f"explicitly asks you to switch language."
    )
    return f"""You are @MausamMitra, a conversational assistant for the India \
Meteorological Department. You give accurate, grounded weather, forecast, \
climate and disaster-alert information for locations in India.

Rules:
- NEVER state a specific weather number (temperature, rainfall, wind speed) unless \
it came from a tool result in this conversation.
- For an ordinary weather, forecast, alert, or farming question about one place, \
call get_weather_briefing ONCE — it already resolves the location, fetches the \
forecast, checks alerts, and (if you pass a crop) builds a crop advisory, all in \
one call. Only fall back to the individual tools (geocode_location, get_forecast, \
get_historical_climate, evaluate_alerts, generate_crop_advisory) for things the \
briefing doesn't cover, like historical climate trends.
- If the user asks about farming, aviation, or marine activity, pass the crop/context \
to get_weather_briefing (or call generate_crop_advisory) so the advisory is tailored, \
grounded in the forecast numbers you fetched.
- Always mention a severe alert from the briefing/evaluate_alerts even if the user \
didn't explicitly ask about it.
- If a query is ambiguous about location, ask a brief clarifying question instead of \
guessing.
- Keep answers concise and actionable — this may be read aloud via text-to-speech \
to a user in a rural area with a basic phone.
- {lang_line}
"""


TOOLS = [
    wt.get_weather_briefing,
    wt.geocode_location,
    wt.get_historical_climate,
    wt.get_forecast,
    wt.evaluate_alerts,
    wt.generate_crop_advisory,
]

# In-memory chat sessions, keyed by session_id. Each holds a live
# google-genai async Chat object plus a last-used timestamp for TTL
# eviction. Swap this dict for a proper session store (e.g. Redis, keyed
# by user/device id) for anything beyond a single-process demo.
_SESSIONS: dict[str, dict] = {}


def _evict_stale_sessions():
    now = time.time()
    stale = [sid for sid, s in _SESSIONS.items() if now - s["last_used"] > SESSION_TTL_SECONDS]
    for sid in stale:
        _SESSIONS.pop(sid, None)


def _model_is_gemini_3_family(model: str) -> bool:
    """Gemini 3.x uses a different thinking control (`thinking_level`) than
    Gemini 2.x (`thinking_budget`), and rejects `thinking_budget` outright
    with a bare 400 INVALID_ARGUMENT. We deliberately do NOT try to guess a
    `thinking_level` value for 3.x instead: the SDK does not validate that
    enum locally — an invalid string like "off" only produces a python
    UserWarning and is sent to the API anyway, surfacing as a live 400 with
    no local warning to catch it beforehand. Guessing values this way is
    exactly what caused two separate 400 bugs in this file already,
    so for Gemini 3.x we just leave thinking_config out entirely and give
    max_output_tokens enough headroom to absorb whatever the model spends
    on its own default reasoning.

    Detection is deliberately conservative: an explicit "gemini-3..." name
    is the clear case, but rolling aliases like "gemini-flash-lite-latest"
    or "gemini-flash-latest" don't encode a version number at all, and in
    practice always resolve to a current (3.x-generation) model once 2.x is
    no longer offered to new API keys — so anything that ISN'T explicitly
    an older "gemini-1"/"gemini-2" name is treated as 3.x-like too. Worst
    case for a genuinely 2.x model misclassified this way is a slightly
    larger prompt budget than strictly needed, not a broken request — safer
    than the reverse mistake of sending a rejected field.
    """
    return not (model.startswith("gemini-1") or model.startswith("gemini-2"))


# Whether thinking_config is currently believed to be safe to send to MODEL.
# False from the start for Gemini 3.x (see above). For everything else it
# starts True and is permanently flipped to False the first time a live
# request actually 400s with it — self-healing without needing a code
# change if a future model/SDK combination has the same issue.
_thinking_config_supported = not _model_is_gemini_3_family(MODEL)


def _build_generation_config(language: str, *, use_thinking_config: bool | None = None) -> types.GenerateContentConfig:
    """Build the per-session Gemini config.

    IMPORTANT: thinking tokens are drawn from the SAME max_output_tokens
    budget as the visible reply, and non-English scripts (Devanagari,
    Gujarati, Tamil, etc.) cost more tokens per word than English — so the
    cap here is generous rather than aggressively small, to make sure the
    model never spends its whole budget "thinking" and returns an empty
    final answer in any supported language.
    """
    if use_thinking_config is None:
        use_thinking_config = _thinking_config_supported
    base_kwargs = dict(
        system_instruction=_system_instruction(language),
        tools=TOOLS,
        max_output_tokens=4096,
    )
    if use_thinking_config and hasattr(types, "ThinkingConfig"):
        try:
            return types.GenerateContentConfig(
                **base_kwargs,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            )
        except Exception:
            pass  # fall through to the config without thinking_config
    return types.GenerateContentConfig(**base_kwargs)


def _is_invalid_argument_error(exc: Exception) -> bool:
    text = str(exc)
    return "INVALID_ARGUMENT" in text or " 400 " in f" {text} " or text.startswith("400")


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text or "quota" in text.lower()


RATE_LIMIT_MESSAGE = (
    "Gemini's API quota for this key has been used up for now "
    "(the free tier allows a limited number of requests per day). "
    "This isn't a bug — wait a bit and try again, or see "
    "https://ai.google.dev/gemini-api/docs/rate-limits for options "
    "like enabling billing for a much higher limit."
)


def _new_chat_session(language: str, *, use_thinking_config: bool | None = None):
    return _get_client().aio.chats.create(
        model=MODEL,
        config=_build_generation_config(language, use_thinking_config=use_thinking_config),
    )


def _get_chat(session_id: str, language: str = "en"):
    _evict_stale_sessions()
    entry = _SESSIONS.get(session_id)
    if entry is None:
        entry = {
            "chat": _new_chat_session(language),
            "language": language,
            "last_used": time.time(),
        }
        _SESSIONS[session_id] = entry
    entry["last_used"] = time.time()
    return entry["chat"]


def _rebuild_session_without_thinking_config(session_id: str):
    """Called after a live 400 that looks like the thinking_config problem.
    Marks the field as permanently unsupported for this model (so we don't
    keep tripping over it), then gives the session a fresh chat object with
    thinking_config omitted. Conversation history for that session is lost
    for this recovery turn — acceptable for a demo; the retried message
    still gets answered.
    """
    global _thinking_config_supported
    _thinking_config_supported = False
    entry = _SESSIONS.get(session_id)
    language = entry["language"] if entry else "en"
    new_entry = {
        "chat": _new_chat_session(language, use_thinking_config=False),
        "language": language,
        "last_used": time.time(),
    }
    _SESSIONS[session_id] = new_entry
    return new_entry["chat"]


def _extract_trace(response) -> list[dict]:
    trace = []
    try:
        for entry in response.automatic_function_calling_history or []:
            for part in getattr(entry, "parts", []) or []:
                if getattr(part, "function_call", None):
                    trace.append({"tool": part.function_call.name, "input": dict(part.function_call.args)})
                if getattr(part, "function_response", None):
                    if trace:
                        trace[-1]["output"] = part.function_response.response
    except Exception:
        pass  # trace is a nice-to-have for the demo UI, never block the reply on it
    return trace


def _log_response_diagnostics(response, label: str) -> None:
    """Print exactly why a response came back empty, to the backend console.
    This is the log to check (the terminal running `uvicorn`) whenever the
    UI shows the generic fallback message — it will show one of:
      - finish_reason=SAFETY / RECITATION -> the model refused/was blocked
      - finish_reason=MAX_TOKENS -> still ran out of budget before answering
      - a function_call with empty automatic_function_calling_history ->
        Automatic Function Calling did not execute the tool (SDK/model
        mismatch) — this is the case to report back for a code fix
      - prompt_feedback set -> the input itself was blocked
    """
    try:
        has_text = bool(response.text)
        finish_reasons = []
        has_function_call = False
        for cand in getattr(response, "candidates", None) or []:
            finish_reasons.append(getattr(cand, "finish_reason", None))
            for part in getattr(getattr(cand, "content", None), "parts", None) or []:
                if getattr(part, "function_call", None):
                    has_function_call = True
        afc_history_len = len(getattr(response, "automatic_function_calling_history", None) or [])
        prompt_feedback = getattr(response, "prompt_feedback", None)
        log.info(
            "[%s] has_text=%s finish_reasons=%s has_function_call=%s "
            "afc_history_len=%s prompt_feedback=%s",
            label, has_text, finish_reasons, has_function_call, afc_history_len, prompt_feedback,
        )
    except Exception as exc:
        log.info("[%s] could not introspect response: %s", label, exc)


FALLBACK_EMPTY_REPLY = (
    "Sorry, I couldn't put together an answer for that one — could you try "
    "rephrasing, or ask again?"
)


async def chat(session_id: str, user_message: str, language: str = "en") -> dict:
    """Run one turn of the @MausamMitra agent for a given session (non-streaming).

    Returns a dict with the model's reply text and a best-effort trace of
    which tools were called (useful for a demo UI / judges), extracted
    from the chat's automatic-function-calling history.
    """
    session = _get_chat(session_id, language)
    try:
        response = await session.send_message(user_message)
    except Exception as exc:
        log.exception("send_message raised for session %s", session_id)
        if _is_rate_limit_error(exc):
            raise  # exhausted quota — retrying won't help, and only burns more of it
        if _thinking_config_supported and _is_invalid_argument_error(exc):
            # Live confirmation that this model rejects thinking_config — rebuild
            # without it and retry this same message once before giving up.
            session = _rebuild_session_without_thinking_config(session_id)
            response = await session.send_message(user_message)
        else:
            # A different, less predictable failure — e.g. a one-off SDK/model
            # glitch such as Gemini calling a slightly wrong/hallucinated tool
            # name, which surfaces as a raw KeyError from the SDK's automatic
            # function-calling dispatcher, not something this app's code
            # caused. A fresh session with no history is often enough to get
            # past it on retry, so try that once before surfacing an error.
            log.info("retrying in a fresh session after unexpected error for %s", session_id)
            session = _new_chat_session(language)
            response = await session.send_message(user_message)
            _SESSIONS[session_id] = {"chat": session, "language": language, "last_used": time.time()}

    if not response.text:
        _log_response_diagnostics(response, "chat: first response empty")
        # The model (usually mid-conversation, after a tool call) spent its
        # whole token budget "thinking" and left nothing for the visible
        # answer. Nudge it once, in the SAME session — the tool result is
        # already in history, so this doesn't repeat any tool calls.
        try:
            response = await session.send_message(
                "Please give your answer now, in plain text, based on the "
                "information you already have."
            )
        except Exception:
            log.exception("nudge retry raised for session %s", session_id)
        if not response.text:
            _log_response_diagnostics(response, "chat: nudge retry also empty")
            # Same fresh-session fallback as chat_stream(): the session's
            # history is likely stuck, so start clean and re-ask directly.
            try:
                fresh = _new_chat_session(language)
                fresh_response = await fresh.send_message(user_message)
                if fresh_response.text:
                    _SESSIONS[session_id] = {
                        "chat": fresh, "language": language, "last_used": time.time(),
                    }
                    response = fresh_response
                else:
                    _log_response_diagnostics(fresh_response, "chat: fresh-session retry also empty")
            except Exception:
                log.exception("fresh-session retry raised for session %s", session_id)

    return {"reply": response.text or FALLBACK_EMPTY_REPLY, "trace": _extract_trace(response)}


async def chat_stream(session_id: str, user_message: str, language: str = "en"):
    """Run one turn of the @MausamMitra agent, yielding reply text incrementally.

    Yields dicts of the shape {"type": "token", "text": "..."} while the
    reply streams in, then a final {"type": "done", "trace": [...]} once
    Gemini (and any tool calls it made) has finished, so the UI can render
    text as it arrives instead of waiting for the whole turn.
    """
    session = _get_chat(session_id, language)
    yielded_any_text = False
    try:
        stream = await session.send_message_stream(user_message)
        final_response = None
        async for chunk in stream:
            if chunk.text:
                yielded_any_text = True
                yield {"type": "token", "text": chunk.text}
            final_response = chunk
    except Exception as exc:
        log.exception("send_message_stream raised for session %s", session_id)
        if _is_rate_limit_error(exc):
            # Don't burn more of an already-exhausted quota on a retry —
            # surface the real reason immediately instead.
            yield {"type": "error", "message": RATE_LIMIT_MESSAGE}
            return
        if _thinking_config_supported and _is_invalid_argument_error(exc):
            # Same live-confirmation retry as chat(), but streaming: nothing has
            # been yielded to the client yet if the error happens on the first
            # chunk, so it's safe to swap sessions and start the stream over.
            session = _rebuild_session_without_thinking_config(session_id)
        else:
            # A different, less predictable failure — e.g. a one-off SDK/model
            # glitch such as Gemini calling a slightly wrong/hallucinated tool
            # name, which surfaces as a raw KeyError from the SDK's automatic
            # function-calling dispatcher, not something this app's code
            # caused. A fresh session with no history is often enough to get
            # past it on retry, so try that once before surfacing an error.
            log.info("retrying in a fresh session after unexpected error for %s", session_id)
            session = _new_chat_session(language)
            _SESSIONS[session_id] = {"chat": session, "language": language, "last_used": time.time()}
        final_response = None
        try:
            stream = await session.send_message_stream(user_message)
            async for chunk in stream:
                if chunk.text:
                    yielded_any_text = True
                    yield {"type": "token", "text": chunk.text}
                final_response = chunk
        except Exception as exc2:
            log.exception("retry send_message_stream also raised for session %s", session_id)
            yield {"type": "error", "message": str(exc2)}
            return

    if not yielded_any_text:
        if final_response is not None:
            _log_response_diagnostics(final_response, "chat_stream: first response empty")
        else:
            log.info("chat_stream: stream produced no chunks at all for session %s", session_id)
        # Same "spent the whole budget thinking, nothing left to say" case
        # as chat() — nudge once in the same session (tool results are
        # already in history) and stream whatever comes back, or fall back
        # to a plain message so the UI never shows a blank reply.
        try:
            stream = await session.send_message_stream(
                "Please give your answer now, in plain text, based on the "
                "information you already have."
            )
            async for chunk in stream:
                if chunk.text:
                    yielded_any_text = True
                    yield {"type": "token", "text": chunk.text}
                final_response = chunk
        except Exception as exc:
            if _is_rate_limit_error(exc):
                log.info("nudge skipped/failed due to rate limit for session %s", session_id)
                yielded_any_text = False
                yield {"type": "token", "text": RATE_LIMIT_MESSAGE}
                yield {"type": "done", "trace": []}
                return
            log.exception("nudge send_message_stream raised for session %s", session_id)
        if not yielded_any_text:
            if final_response is not None:
                _log_response_diagnostics(final_response, "chat_stream: nudge retry also empty")
            # The nudge came back empty too, in the SAME session — that
            # session's history is likely stuck in a state the model won't
            # continue from (a known quirk right after a streamed tool
            # call). Rather than give up, start a completely fresh session
            # (no history to get stuck on) and ask the original question
            # again, non-streaming — simpler code path, and sidesteps
            # whatever the streaming-specific issue was.
            log.info("chat_stream: retrying in a fresh session for %s", session_id)
            try:
                fresh = _new_chat_session(language)
                fresh_response = await fresh.send_message(user_message)
                if fresh_response.text:
                    _SESSIONS[session_id] = {
                        "chat": fresh, "language": language, "last_used": time.time(),
                    }
                    yielded_any_text = True
                    yield {"type": "token", "text": fresh_response.text}
                    final_response = fresh_response
                else:
                    _log_response_diagnostics(fresh_response, "chat_stream: fresh-session retry also empty")
            except Exception:
                log.exception("fresh-session retry raised for session %s", session_id)
        if not yielded_any_text:
            yield {"type": "token", "text": FALLBACK_EMPTY_REPLY}

    trace = _extract_trace(final_response) if final_response is not None else []
    yield {"type": "done", "trace": trace}


def reset_session(session_id: str) -> None:
    """Clear a session's conversation history (e.g. on 'new chat')."""
    _SESSIONS.pop(session_id, None)
