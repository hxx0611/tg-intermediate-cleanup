# -*- coding: utf-8 -*-
"""TG Intermediate Cleanup plugin (v0.2.0).

Telegram: after the final answer has been sent, automatically delete all
intermediate process messages (💭 reasoning, tool-call info, tool results,
intermediate cards/media) so the chat only keeps the final reply.

v0.2.0 changes
--------------
- Unit typing: every message unit produced during a request is recorded
  with a kind ("answer-stream" / "message" / "process-stream" / "notice").
  Cleanup keeps the LAST answer-like unit and anything appended after it
  (e.g. the model-fallback notice that the framework sends right after the
  final answer), instead of blindly keeping "the last unit".  This fixes
  the v0.1 bug where a fallback notice replaced (and thus deleted) the
  real final answer.
- Media & card tracking: ``send_content_parts`` / message-completed units
  now group text + media + card sends of one event into ONE unit, and
  ``Bot.send_photo/video/audio/document/voice/animation/media_group`` are
  tracked as well, so intermediate media and cards are cleaned up too
  while media attached to the final answer is kept.
- Deletion robustness: deletes go through ``bot.delete_message`` directly
  (the framework helper swallows failures).  ``RetryAfter`` is honoured
  with one sleep+retry; if deletion fails (e.g. no admin rights in a
  group) the message is collapsed via ``edit_message_text("…")``; a
  message already gone is not counted as failure.
- Install pre-flight: all patch targets are verified before any patch is
  applied, and a mid-install failure rolls back applied patches, so the
  plugin can never sit in a half-patched state.
- Degradation guard: if a completed request tracked messages but no
  answer-like unit, NOTHING is deleted and a WARNING is logged (fail-safe
  for future framework layout changes; worst case becomes "messages are
  not cleaned", never "the answer is deleted").
- Concurrency: per-batch state lives in its own ContextVar, so parallel
  sends inside one request can no longer merge batches.

Design (unchanged)
------------------
The Telegram channel displays streaming progress via placeholder messages
that are edited in place (``on_streaming_start`` / ``on_streaming_end``)
and via normal ``send()`` / ``send_content_parts()`` calls (tool-call
text, tool results, notices, cards, media).  This plugin tracks every
message id produced by one request and, when the request finishes
(``_on_process_completed``, success path only), deletes everything
except the final answer unit (and anything appended after it).
Errors/cancellations never reach ``_on_process_completed``, so the scene
is preserved for debugging.

Why a plugin / monkey-patch instead of editing framework source:
  - No changes to installed qwenpaw files -> qwenpaw upgrades do not
    overwrite or break this behaviour.
  - Patches are applied at startup and restored at shutdown, and are
    idempotent.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

logger = logging.getLogger("qwenpaw.tg_cleanup")

PLUGIN_ID = "tg-intermediate-cleanup"

# send_meta key under which this plugin keeps its per-request state.
CLEANUP_KEY = "_tg_cleanup"

# Unit kinds.  "answer-stream" and "message" are answer-eligible;
# "process-stream" (reasoning placeholders etc.) and "notice" are not.
KIND_ANSWER_STREAM = "answer-stream"
KIND_MESSAGE = "message"
KIND_PROCESS_STREAM = "process-stream"
KIND_NOTICE = "notice"
KEEP_ELIGIBLE = (KIND_ANSWER_STREAM, KIND_MESSAGE)

# ContextVar of the per-request cleanup state of the request currently
# performing a send.  ContextVars are task-local, so concurrent chats do
# not interfere.
_SEND_CTX: contextvars.ContextVar = contextvars.ContextVar(
    "qwenpaw_tg_cleanup_send_ctx",
    default=None,
)
# ContextVar of the *open unit* (list of message ids) of the current
# boundary call.  Separate from _SEND_CTX so that parallel sends within
# one request get separate batches instead of sharing a merged list.
_BATCH_CTX: contextvars.ContextVar = contextvars.ContextVar(
    "qwenpaw_tg_cleanup_batch_ctx",
    default=None,
)

_INSTALLED = False

# Telegram Bot methods whose return value carries new message ids.
# send_message is required; media methods are best-effort (skipped with a
# warning when the installed python-telegram-bot lacks them).
_BOT_METHODS_REQUIRED = ("send_message",)
_BOT_METHODS_OPTIONAL = (
    "send_photo",
    "send_video",
    "send_audio",
    "send_document",
    "send_voice",
    "send_animation",
    "send_media_group",
)


def _state(send_meta):
    """Return the per-request cleanup state dict (create if missing)."""
    if not isinstance(send_meta, dict):
        return None
    st = send_meta.get(CLEANUP_KEY)
    if st is None:
        st = {
            "tracked": [],  # every message id seen for this request
            "units": [],    # [{"ids": [...], "kind": str}], in send order
        }
        send_meta[CLEANUP_KEY] = st
    return st


def _extract_msg_ids(msg):
    """Pull message ids out of a bot API return value."""
    if msg is None:
        return []
    mid = getattr(msg, "message_id", None)
    if mid:
        return [mid]
    if isinstance(msg, (list, tuple)):
        out = []
        for item in msg:
            item_id = getattr(item, "message_id", None)
            if item_id:
                out.append(item_id)
        return out
    return []


class _Unit:
    """Open one unit: bind st + fresh batch to the current task context."""

    __slots__ = ("st", "batch", "tok_ctx", "tok_batch")

    def __init__(self, st):
        self.st = st
        self.batch = []
        self.tok_ctx = None
        self.tok_batch = None

    def __enter__(self):
        self.tok_ctx = _SEND_CTX.set(self.st)
        self.tok_batch = _BATCH_CTX.set(self.batch)
        return self

    def __exit__(self, exc_type, exc, tb):
        _BATCH_CTX.reset(self.tok_batch)
        _SEND_CTX.reset(self.tok_ctx)
        return False

    def close(self, kind):
        if self.batch:
            self.st["units"].append({"ids": list(self.batch), "kind": kind})


def _unit_active_for(st):
    """True when the current task is already inside a unit of *st*."""
    return _SEND_CTX.get() is st


def _orig_attr_name(attr):
    """Stored-original attribute name for a patched method name.

    ``send`` -> ``_tg_cleanup_orig_send``; the leading underscore of
    private names like ``_on_process_completed`` is stripped so we never
    build double-underscore names the patched functions do not call.
    """
    return "_tg_cleanup_orig_" + attr.lstrip("_")


# ---------------------------------------------------------------------------
# Patched methods
# ---------------------------------------------------------------------------

async def _patched_on_streaming_start(
    self,
    request,
    to_handle,
    event,
    send_meta,
    stream_type,
    accumulated_text="",
):
    """Original + track the placeholder message id."""
    await self._tg_cleanup_orig_on_streaming_start(
        request,
        to_handle,
        event,
        send_meta,
        stream_type,
        accumulated_text,
    )
    st = _state(send_meta)
    if st is None:
        return
    try:
        sid = send_meta["_tg_stream"]["message_ids"].get(stream_type)
    except Exception:
        sid = None
    if sid and sid not in st["tracked"]:
        st["tracked"].append(sid)


async def _patched_on_streaming_end(
    self,
    request,
    to_handle,
    event,
    send_meta,
    stream_type,
    accumulated_text="",
):
    """Record the placeholder as a unit *before* the original runs.

    If the final text is too long, the original deletes the placeholder
    and falls back to ``send()``; that fallback becomes a later unit, so
    the answer-eligible unit chosen at cleanup is the real final answer.
    """
    st = _state(send_meta)
    if st is not None:
        msg_id = None
        try:
            msg_id = send_meta["_tg_stream"]["message_ids"].get(stream_type)
        except Exception:
            msg_id = None
        if msg_id:
            kind = (
                KIND_ANSWER_STREAM
                if stream_type == "message"
                else KIND_PROCESS_STREAM
            )
            st["units"].append({"ids": [msg_id], "kind": kind})
    await self._tg_cleanup_orig_on_streaming_end(
        request,
        to_handle,
        event,
        send_meta,
        stream_type,
        accumulated_text,
    )


async def _patched_send(self, to_handle, text, meta=None):
    """Original + track message ids; a bare send() forms its own unit."""
    st = _state(meta)
    if st is None or _unit_active_for(st):
        # No state (proactive/foreign meta) or outer boundary owns the
        # unit — just contribute ids via the Bot patch.
        return await self._tg_cleanup_orig_send(to_handle, text, meta)
    with _Unit(st) as unit:
        await self._tg_cleanup_orig_send(to_handle, text, meta)
    unit.close(KIND_MESSAGE)


async def _patched_send_content_parts(self, to_handle, parts, meta=None):
    """Group one send_content_parts call (text + media) into one unit."""
    st = _state(meta)
    if st is None or _unit_active_for(st):
        return await self._tg_cleanup_orig_send_content_parts(
            to_handle,
            parts,
            meta,
        )
    with _Unit(st) as unit:
        await self._tg_cleanup_orig_send_content_parts(
            to_handle,
            parts,
            meta,
        )
    unit.close(KIND_MESSAGE)


async def _patched_on_event_message_completed(
    self,
    request,
    to_handle,
    event,
    send_meta,
):
    """Whole completed-message event (incl. card path) = one unit."""
    st = _state(send_meta)
    if st is None or _unit_active_for(st):
        return await self._tg_cleanup_orig_on_event_message_completed(
            request,
            to_handle,
            event,
            send_meta,
        )
    with _Unit(st) as unit:
        await self._tg_cleanup_orig_on_event_message_completed(
            request,
            to_handle,
            event,
            send_meta,
        )
    unit.close(KIND_MESSAGE)


async def _patched_send_model_fallback_notice(
    self,
    to_handle,
    event,
    send_meta,
):
    """Fallback notices are auxiliary: never replace the answer."""
    st = _state(send_meta)
    if st is None or _unit_active_for(st):
        return await self._tg_cleanup_orig_send_model_fallback_notice(
            to_handle,
            event,
            send_meta,
        )
    with _Unit(st) as unit:
        await self._tg_cleanup_orig_send_model_fallback_notice(
            to_handle,
            event,
            send_meta,
        )
    unit.close(KIND_NOTICE)


def _make_bot_patch(orig_attr):
    """Factory: collect new message ids into the active request state."""

    async def _patched(bot_self, *args, **kwargs):
        orig = getattr(bot_self, orig_attr)
        msg = await orig(*args, **kwargs)
        st = _SEND_CTX.get()
        if st is not None:
            ids = _extract_msg_ids(msg)
            if ids:
                batch = _BATCH_CTX.get()
                for mid in ids:
                    st["tracked"].append(mid)
                    if batch is not None:
                        batch.append(mid)
        return msg

    return _patched


async def _delete_or_collapse(bot, chat_id, mid):
    """Delete one message; on failure collapse it to '…'.

    Returns one of: deleted / collapsed / gone / failed.
    """
    from telegram.error import BadRequest, RetryAfter

    for attempt in (1, 2):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
            return "deleted"
        except RetryAfter as exc:
            if attempt == 2:
                break
            wait = min(
                float(getattr(exc, "retry_after", 1.0) or 1.0) + 0.5,
                5.0,
            )
            await asyncio.sleep(wait)
        except Exception:
            break
    # Delete did not succeed (permission / already gone / hard error).
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text="…",
        )
        return "collapsed"
    except BadRequest:
        return "gone"  # message not found / not ours to edit: harmless
    except Exception:
        logger.debug("tg_cleanup: collapse msg %s failed", mid, exc_info=True)
        return "failed"


async def _patched_on_process_completed(self, request, to_handle, send_meta):
    """Original + delete all intermediate messages, keep final answer."""
    await self._tg_cleanup_orig_on_process_completed(
        request,
        to_handle,
        send_meta,
    )
    st = send_meta.get(CLEANUP_KEY) if isinstance(send_meta, dict) else None
    if not st or not st.get("tracked"):
        return
    try:
        units = st.get("units") or []
        idx = None
        for i in range(len(units) - 1, -1, -1):
            if units[i]["kind"] in KEEP_ELIGIBLE:
                idx = i
                break
        if idx is None:
            # Fail-safe: nothing recognizable as the final answer.
            logger.warning(
                "tg_cleanup: %d tracked message(s) but no answer unit; "
                "skipping cleanup (framework layout may have changed)",
                len(st["tracked"]),
            )
            return
        keep = set()
        for unit in units[idx:]:
            keep.update(unit["ids"])
        bot = getattr(getattr(self, "_application", None), "bot", None)
        if bot is None:
            logger.warning("tg_cleanup: bot unavailable, cleanup skipped")
            return
        chat_id = send_meta.get("chat_id") or to_handle
        counts = {"deleted": 0, "collapsed": 0, "gone": 0, "failed": 0}
        seen = set()
        for mid in st["tracked"]:
            if mid in keep or mid in seen:
                continue
            seen.add(mid)
            outcome = await _delete_or_collapse(bot, chat_id, mid)
            counts[outcome] += 1
        removed = counts["deleted"] + counts["collapsed"]
        if removed or counts["failed"]:
            logger.info(
                "tg_cleanup: chat=%s removed %d (deleted %d, collapsed %d, "
                "gone %d, failed %d), kept %d",
                chat_id,
                removed,
                counts["deleted"],
                counts["collapsed"],
                counts["gone"],
                counts["failed"],
                len(keep),
            )
        if counts["failed"]:
            logger.warning(
                "tg_cleanup: %d message(s) could neither be deleted nor "
                "collapsed (check bot rights in this chat)",
                counts["failed"],
            )
    finally:
        send_meta.pop(CLEANUP_KEY, None)


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

def _install_patches():
    """Apply monkey-patches to TelegramChannel / telegram.Bot.

    Pre-flight first: every required patch target must exist *before*
    anything is applied, and any failure mid-apply rolls back what was
    already patched, so the plugin can never sit half-installed.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from qwenpaw.app.channels.telegram.channel import TelegramChannel
        from telegram import Bot
    except Exception as exc:  # pragma: no cover - host-specific
        logger.warning("tg_cleanup: cannot import channel classes: %s", exc)
        return

    specs = [
        (TelegramChannel, "on_streaming_start"),
        (TelegramChannel, "on_streaming_end"),
        (TelegramChannel, "send"),
        (TelegramChannel, "send_content_parts"),
        (TelegramChannel, "on_event_message_completed"),
        (TelegramChannel, "_send_model_fallback_notice"),
        (TelegramChannel, "_on_process_completed"),
    ]
    patched_attr = {
        "on_streaming_start": _patched_on_streaming_start,
        "on_streaming_end": _patched_on_streaming_end,
        "send": _patched_send,
        "send_content_parts": _patched_send_content_parts,
        "on_event_message_completed": _patched_on_event_message_completed,
        "_send_model_fallback_notice": _patched_send_model_fallback_notice,
        "_on_process_completed": _patched_on_process_completed,
    }

    # Pre-flight: framework API sanity check.
    missing = [
        f"{cls.__name__}.{attr}"
        for cls, attr in specs
        if not hasattr(cls, attr)
    ]
    if missing:
        logger.warning(
            "tg_cleanup: aborting install, framework API changed: "
            "missing %s",
            ", ".join(missing),
        )
        return

    applied = []  # (cls, attr, orig, orig_attr)
    try:
        for cls, attr in specs:
            current = getattr(cls, attr)
            if current is patched_attr[attr]:
                continue  # already patched (idempotent)
            orig_attr = _orig_attr_name(attr)
            setattr(cls, orig_attr, current)
            setattr(cls, attr, patched_attr[attr])
            applied.append((cls, attr, current, orig_attr))

        for method in _BOT_METHODS_REQUIRED + _BOT_METHODS_OPTIONAL:
            if not hasattr(Bot, method):
                if method in _BOT_METHODS_REQUIRED:
                    raise AttributeError(
                        f"telegram.Bot.{method} missing (unsupported "
                        "python-telegram-bot version)",
                    )
                logger.warning(
                    "tg_cleanup: telegram.Bot.%s not available, skipped",
                    method,
                )
                continue
            orig_attr = "_tg_cleanup_orig_bot_" + method
            current = getattr(Bot, method)
            patched = _make_bot_patch(orig_attr)
            if current is patched:
                continue
            setattr(Bot, orig_attr, current)
            setattr(Bot, method, patched)
            applied.append((Bot, method, current, orig_attr))
    except Exception:
        logger.exception("tg_cleanup: install failed, rolling back")
        for cls, attr, orig, orig_attr in reversed(applied):
            try:
                setattr(cls, attr, orig)
                delattr(cls, orig_attr)
            except Exception:  # pragma: no cover
                logger.debug("tg_cleanup: rollback failed", exc_info=True)
        return

    _INSTALLED = True
    logger.info("tg_cleanup: patches installed (v0.2.0)")


def _uninstall_patches():
    """Restore original methods (idempotent)."""
    global _INSTALLED
    if not _INSTALLED:
        return
    try:
        from qwenpaw.app.channels.telegram.channel import TelegramChannel
        from telegram import Bot
    except Exception:  # pragma: no cover
        return

    def _restore(cls, attr, orig_attr):
        orig = getattr(cls, orig_attr, None)
        if orig is not None:
            setattr(cls, attr, orig)
            try:
                delattr(cls, orig_attr)
            except AttributeError:
                pass

    for attr in (
        "on_streaming_start",
        "on_streaming_end",
        "send",
        "send_content_parts",
        "on_event_message_completed",
        "_send_model_fallback_notice",
        "_on_process_completed",
    ):
        _restore(TelegramChannel, attr, _orig_attr_name(attr))
    for method in _BOT_METHODS_REQUIRED + _BOT_METHODS_OPTIONAL:
        _restore(Bot, method, "_tg_cleanup_orig_bot_" + method)
    _INSTALLED = False
    logger.info("tg_cleanup: patches uninstalled")


class TgIntermediateCleanupPlugin:
    """Plugin entry."""

    def register(self, api):
        api.register_startup_hook(
            "tg_intermediate_cleanup_install",
            _install_patches,
            priority=95,
        )
        api.register_shutdown_hook(
            "tg_intermediate_cleanup_uninstall",
            _uninstall_patches,
            priority=95,
        )


plugin = TgIntermediateCleanupPlugin()
