# -*- coding: utf-8 -*-
"""Simulate framework lifecycle against the patched plugin logic (v1.0.0)."""
import asyncio
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "tgc", str(pathlib.Path(__file__).resolve().parent.parent / "plugin.py"),
)
tgc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tgc)


class Msg:
    def __init__(self, mid):
        self.message_id = mid


class FakeBot:
    def __init__(self):
        self._next = 100
        self.deleted = []
        self.edited = {}

    def _mid(self):
        self._next += 1
        return self._next

    async def send_message(self, **kw):
        return Msg(self._mid())

    async def send_photo(self, **kw):
        return Msg(self._mid())

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edited[message_id] = text


class FakeApp:
    def __init__(self, bot):
        self.bot = bot


class FakeChannel:
    def __init__(self, bot):
        self._application = FakeApp(bot)

    # ---- originals (emulating qwenpaw behaviour) ----
    async def impl_on_streaming_start(self, request, to_handle, event,
                                      send_meta, stream_type,
                                      accumulated_text=""):
        msg = await self._application.bot.send_message(
            chat_id=to_handle, text="…")
        send_meta.setdefault("_tg_stream", {"message_ids": {}})
        send_meta["_tg_stream"]["message_ids"][stream_type] = msg.message_id

    async def impl_on_streaming_end(self, request, to_handle, event,
                                    send_meta, stream_type,
                                    accumulated_text=""):
        # normal case: placeholder edited in place, nothing new
        pass

    async def impl_send(self, to_handle, text, meta=None):
        await self._application.bot.send_message(
            chat_id=to_handle, text=text)

    async def impl_send_media(self, to_handle, part, meta=None):
        await self._application.bot.send_photo(chat_id=to_handle, photo="x")

    async def impl_send_content_parts(self, to_handle, parts, meta=None):
        text = "\n".join(p["text"] for p in parts if p["type"] == "text")
        if text:
            await self.send(to_handle, text, meta)
        for p in parts:
            if p["type"] == "image":
                await self.impl_send_media(to_handle, p, meta)

    async def impl_on_event_message_completed(self, request, to_handle,
                                              event, send_meta):
        await self.send_content_parts(to_handle, event["parts"], send_meta)

    async def impl_fallback_notice(self, to_handle, event, send_meta):
        await self.send_content_parts(
            to_handle, [{"type": "text", "text": "notice"}], send_meta)

    async def impl_on_process_completed(self, request, to_handle, send_meta):
        pass


# channel attr name -> (original impl name, patched func)
CHANNEL_PATCHES = [
    ("on_streaming_start", "impl_on_streaming_start",
     "_patched_on_streaming_start"),
    ("on_streaming_end", "impl_on_streaming_end",
     "_patched_on_streaming_end"),
    ("send", "impl_send", "_patched_send"),
    ("send_content_parts", "impl_send_content_parts",
     "_patched_send_content_parts"),
    ("on_event_message_completed", "impl_on_event_message_completed",
     "_patched_on_event_message_completed"),
    ("_send_model_fallback_notice", "impl_fallback_notice",
     "_patched_send_model_fallback_notice"),
    ("_on_process_completed", "impl_on_process_completed",
     "_patched_on_process_completed"),
]

BOT_METHODS = ("send_message", "send_photo")


def install(cls, bot_cls):
    """Mirror _install_patches onto the fakes (same attr conventions)."""
    for attr, impl_name, patched_name in CHANNEL_PATCHES:
        setattr(cls, "_tg_cleanup_orig_" + attr.lstrip("_"),
                getattr(cls, impl_name))
        setattr(cls, attr, getattr(tgc, patched_name))
    for m in BOT_METHODS:
        setattr(bot_cls, "_tg_cleanup_orig_bot_" + m, getattr(bot_cls, m))
        setattr(bot_cls, m, tgc._make_bot_patch("_tg_cleanup_orig_bot_" + m))


async def scenario_streaming_answer_with_notice():
    """reasoning + tool(text+img) streamed, answer via placeholder,
    fallback notice sent AFTER answer. Keep: answer + notice."""
    bot = FakeBot()
    ch = FakeChannel(bot)
    send_meta = {"chat_id": "123"}
    await ch.on_streaming_start(None, "123", None, send_meta, "reasoning")
    await ch.on_streaming_end(None, "123", None, send_meta, "reasoning")
    await ch.on_event_message_completed(
        None, "123",
        {"parts": [{"type": "text", "text": "调用工具 🔧"},
                   {"type": "image", "url": "x"}]},
        send_meta)
    await ch.on_streaming_start(None, "123", None, send_meta, "message")
    await ch.on_streaming_end(None, "123", None, send_meta, "message")
    await ch._send_model_fallback_notice("123", None, send_meta)
    await ch._on_process_completed(None, "123", send_meta)
    deleted = set(bot.deleted)
    # ids: 101 reasoning-ph, 102 tool-text, 103 tool-img,
    #      104 answer-ph, 105 notice
    keep = [m for m in range(101, 106) if m not in deleted]
    print("scenario1 deleted:", sorted(deleted), "kept:", keep)
    assert keep == [104, 105], keep
    assert sorted(deleted) == [101, 102, 103], sorted(deleted)


async def scenario_send_path_answer_with_media_and_notice():
    """Answer (text+photo) sent non-streamed; notice after.
    Keep: answer text + answer photo + notice; delete tool narration."""
    bot = FakeBot()
    ch = FakeChannel(bot)
    send_meta = {"chat_id": "123"}
    await ch.on_event_message_completed(
        None, "123", {"parts": [{"type": "text", "text": "tool run"}]},
        send_meta)
    await ch.on_event_message_completed(
        None, "123",
        {"parts": [{"type": "text", "text": "FINAL"},
                   {"type": "image", "url": "p"}]},
        send_meta)
    await ch._send_model_fallback_notice("123", None, send_meta)
    await ch._on_process_completed(None, "123", send_meta)
    deleted = set(bot.deleted)
    # ids: 101 tool, 102 answer-text, 103 answer-photo, 104 notice
    keep = [m for m in range(101, 105) if m not in deleted]
    print("scenario2 deleted:", sorted(deleted), "kept:", keep)
    assert keep == [102, 103, 104], keep


async def scenario_fallback_to_send_on_long_stream():
    """Long streamed text: original deletes placeholder and falls back
    to send(); the send() unit must be the kept answer, not the dead ph."""
    bot = FakeBot()
    ch = FakeChannel(bot)

    async def end_with_fallback(self, request, to_handle, event, send_meta,
                                stream_type, accumulated_text=""):
        ph = send_meta["_tg_stream"]["message_ids"][stream_type]
        await bot.delete_message(chat_id=to_handle, message_id=ph)
        await ch.send(to_handle, "long answer", send_meta)

    FakeChannel._tg_cleanup_orig_on_streaming_end = end_with_fallback
    try:
        send_meta = {"chat_id": "123"}
        await ch.on_streaming_start(None, "123", None, send_meta, "message")
        await ch.on_streaming_end(None, "123", None, send_meta, "message")
        await ch._on_process_completed(None, "123", send_meta)
        # ids: 101 placeholder (deleted by framework), 102 fallback-send
        still_there = [m for m in (101, 102)
                       if bot.deleted.count(m) == 0]
        print("scenario3 deleted:", sorted(bot.deleted),
              "kept(never plugin-deleted):", still_there)
        # framework deletes placeholder(101) once; plugin may attempt one
        # extra harmless delete (real TG -> BadRequest -> "gone"). The
        # fallback answer(102) must never be deleted.
        assert 102 not in bot.deleted, bot.deleted
    finally:
        FakeChannel._tg_cleanup_orig_on_streaming_end = (
            FakeChannel.impl_on_streaming_end)


async def scenario_degraded_no_units():
    """Framework layout changed: tracked but no units -> delete nothing."""
    bot = FakeBot()
    ch = FakeChannel(bot)
    send_meta = {"chat_id": "123"}
    send_meta[tgc.CLEANUP_KEY] = {"tracked": [111, 112], "units": []}
    await ch._on_process_completed(None, "123", send_meta)
    print("scenario4 deleted (must be []):", bot.deleted,
          "state popped:", tgc.CLEANUP_KEY not in send_meta)
    assert bot.deleted == []
    assert tgc.CLEANUP_KEY not in send_meta


async def scenario_error_path_no_cleanup():
    """Errors never call _on_process_completed -> scene preserved."""
    bot = FakeBot()
    ch = FakeChannel(bot)
    send_meta = {"chat_id": "123"}
    await ch.on_streaming_start(None, "123", None, send_meta, "reasoning")
    await ch.on_event_message_completed(
        None, "123", {"parts": [{"type": "text", "text": "tool"}]}, send_meta)
    print("scenario5 deleted (must be []):", bot.deleted)
    assert bot.deleted == []


async def scenario_idempotent_and_real_install():
    """Real _install_patches/_uninstall_patches against the actual
    qwenpaw TelegramChannel + telegram.Bot in this (throwaway) process."""
    from qwenpaw.app.channels.telegram.channel import TelegramChannel
    from telegram import Bot

    orig_send = TelegramChannel.send
    orig_scp = TelegramChannel.send_content_parts
    orig_bot_msg = Bot.send_message
    orig_bot_photo = Bot.send_photo

    tgc._install_patches()
    assert TelegramChannel.send is not orig_send
    assert Bot.send_message is not orig_bot_msg
    assert getattr(TelegramChannel, "_tg_cleanup_orig_send") is orig_send
    assert tgc._INSTALLED is True
    tgc._install_patches()  # second call: no-op via _INSTALLED
    assert getattr(TelegramChannel, "_tg_cleanup_orig_send") is orig_send

    tgc._uninstall_patches()
    assert TelegramChannel.send is orig_send
    assert TelegramChannel.send_content_parts is orig_scp
    assert Bot.send_message is orig_bot_msg
    assert Bot.send_photo is orig_bot_photo
    assert tgc._INSTALLED is False
    print("scenario6 real install/uninstall/idempotent OK")


async def main():
    install(FakeChannel, FakeBot)
    await scenario_streaming_answer_with_notice()
    await scenario_send_path_answer_with_media_and_notice()
    await scenario_fallback_to_send_on_long_stream()
    await scenario_degraded_no_units()
    await scenario_error_path_no_cleanup()
    await scenario_idempotent_and_real_install()
    print("ALL SCENARIOS PASS")


asyncio.run(main())
