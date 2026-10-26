"""Voice call fixes ported from upstream 2.1.0: dead peers, one call per chat,
STT from config, short replies, replies spoken in order, transcript link."""

import asyncio
import json
import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("aiortc")
import call_handler as ch  # noqa: E402


class _FakePc:
    """Just enough RTCPeerConnection for the connection-state hook."""

    def __init__(self, state="new"):
        self.connectionState = state
        self._handlers = {}
        self.close = AsyncMock()

    def on(self, name, fn=None):
        if fn is not None:
            self._handlers[name] = fn
            return fn

        def deco(f):
            self._handlers[name] = f
            return f

        return deco

    def set_state(self, state):
        self.connectionState = state
        self._handlers["connectionstatechange"]()


def _manager():
    adapter = MagicMock()
    adapter.account_id = 1
    adapter.rpc.end_call = AsyncMock()
    mgr = ch.CallManager(adapter=adapter)
    mgr._note_call_ended = AsyncMock()
    return mgr, adapter


def _register(mgr, msg_id=5, chat_id="12", pc=None):
    pc = pc or _FakePc()
    return mgr._register_session(
        pc, None, ch.HermesAudioTrack(), MagicMock(), msg_id, chat_id, "10", "Eve"
    )


class TestDeadPeer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["failed", "closed"])
    async def test_vanished_peer_is_hung_up(self, state):
        """A peer that loses network never sends CallEnded."""
        mgr, adapter = _manager()
        session = _register(mgr)
        session.pc.set_state(state)
        await asyncio.sleep(0.05)
        adapter.rpc.end_call.assert_awaited_once_with(1, 5)
        assert mgr.has_active_call("12") is False

    @pytest.mark.asyncio
    async def test_our_own_teardown_does_not_hang_up_twice(self):
        mgr, adapter = _manager()
        session = _register(mgr)
        await mgr._teardown_session(5)
        session.pc.set_state("closed")  # what pc.close() causes
        await asyncio.sleep(0.05)
        adapter.rpc.end_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_old_call_teardown_keeps_a_redialled_calls_routing(self):
        mgr, _ = _manager()
        _register(mgr, msg_id=5)
        _register(mgr, msg_id=6)  # redial in the same chat
        await mgr._teardown_session(5)
        assert mgr._chat_to_msg["12"] == 6


class TestOneCallPerChat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("busy", ["active", "dialling"])
    async def test_second_call_into_a_busy_chat_is_refused(self, busy):
        mgr, _ = _manager()
        if busy == "active":
            _register(mgr)
        else:
            mgr._dialing.add("12")
        mgr._dial = AsyncMock()
        with pytest.raises(RuntimeError, match="already on a call"):
            await mgr._start_call("12", "Hi")
        mgr._dial.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dialling_mark_is_cleared_after_a_failed_dial(self):
        mgr, _ = _manager()
        mgr._dial = AsyncMock(side_effect=asyncio.TimeoutError)
        with pytest.raises(asyncio.TimeoutError):
            await mgr._start_call("12", "Hi")
        assert mgr._dialing == set()


class TestStt:
    def _tt(self, monkeypatch, provider="local", mistral_model=None):
        calls = []
        tt = types.ModuleType("tools.transcription_tools")
        tt.transcribe_audio = lambda path, *a: calls.append(("local", path, a)) or {
            "success": True,
            "transcript": "hi",
        }
        tt._transcribe_mistral = lambda path, model: calls.append(
            ("mistral", model)
        ) or {
            "success": True,
            "transcript": "hi",
        }
        tt._load_stt_config = lambda: {"mistral": {"model": mistral_model}}
        tt._get_provider = lambda cfg: provider
        tt.DEFAULT_MISTRAL_STT_MODEL = "voxtral-default"
        tools = types.ModuleType("tools")
        tools.transcription_tools = tt
        monkeypatch.setitem(sys.modules, "tools", tools)
        monkeypatch.setitem(sys.modules, "tools.transcription_tools", tt)
        return calls

    def test_configured_provider_picks_the_model(self, monkeypatch):
        """Passing "medium" broke every cloud provider ("Invalid model: medium")."""
        calls = self._tt(monkeypatch)
        ch.IncomingAudioBuffer._transcribe("/x.wav")
        assert calls == [("local", "/x.wav", ())]

    @pytest.mark.parametrize(
        "configured,expected", [("voxtral-x", "voxtral-x"), (None, "voxtral-default")]
    )
    def test_voxtral_uses_the_configured_model(self, monkeypatch, configured, expected):
        calls = self._tt(monkeypatch, mistral_model=configured)
        monkeypatch.setattr(ch, "_CALL_STT_VOXTRAL", True)
        monkeypatch.setenv("MISTRAL_API_KEY", "k")
        ch.IncomingAudioBuffer._transcribe("/x.wav")
        assert calls == [("mistral", expected)]

    def test_warmup_only_for_the_local_provider(self, monkeypatch, tmp_path):
        calls = self._tt(monkeypatch, provider="openai")
        ch.CallManager._warmup_stt_sync(str(tmp_path))
        assert calls == []
        self._tt(monkeypatch, provider="local")
        ch.CallManager._warmup_stt_sync(str(tmp_path / "fresh-home"))  # dir created
        assert (tmp_path / "fresh-home" / "audio_cache").is_dir()

    @pytest.mark.asyncio
    async def test_stt_failure_is_an_error_not_silence(
        self, tmp_path, monkeypatch, caplog
    ):
        buf = ch.IncomingAudioBuffer(
            hermes_home=str(tmp_path), on_utterance=MagicMock()
        )
        monkeypatch.setattr(
            buf, "_transcribe", lambda p: {"success": False, "error": "No STT provider"}
        )
        with caplog.at_level(logging.ERROR):
            await buf._process_utterance(b"\x00" * 3200)
        assert "No STT provider" in caplog.text


class TestShortReplies:
    @pytest.mark.parametrize(
        "text", ["OK", "Okay.", "Thanks!", "Thank you", "Bye bye", "ok, thanks"]
    )
    def test_short_answers_are_kept(self, text):
        assert ch._is_short_reply(text) is True

    @pytest.mark.parametrize(
        "text", ["", "...", "Thanks for watching, subscribe", "Hello"]
    )
    def test_anything_else_still_goes_through_the_filter(self, text):
        assert ch._is_short_reply(text) is False


class TestRepliesInOrder:
    @pytest.mark.asyncio
    async def test_two_replies_are_spoken_one_after_the_other(self):
        mgr, _ = _manager()
        order = []

        async def speak(chat_id, text):
            order.append(("start", text))
            await asyncio.sleep(0.02)
            order.append(("end", text))

        mgr._speak_response = speak
        await asyncio.gather(
            mgr._play_response("12", "a"), mgr._play_response("12", "b")
        )
        assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]

    def test_queued_audio_counts_toward_where_a_reply_starts(self):
        track = ch.HermesAudioTrack()
        track.enqueue_tts_frames([object()] * 3)
        assert track.queued_count == 3


class TestCallEndNote:
    @pytest.mark.asyncio
    async def test_text_chat_note_names_the_call_session(self, monkeypatch):
        mgr, adapter = _manager()
        mgr._note_call_ended = ch.CallManager._note_call_ended.__get__(mgr)
        gw = MagicMock()
        gw.session_store.peek_session_id.return_value = "sess-42"
        mgr._gateway = lambda: gw
        sent = []
        mgr._to_hermes = AsyncMock(side_effect=lambda e: sent.append(e))
        monkeypatch.setattr(
            sys.modules["gateway.platforms.base"],
            "MessageEvent",
            lambda **kw: types.SimpleNamespace(**kw),
        )
        monkeypatch.setattr(ch, "_CALL_THREAD_ID", "call")
        await mgr._note_call_ended("12", "10", "Eve", 5)
        assert 'session_search(session_id="sess-42")' in sent[-1].text
