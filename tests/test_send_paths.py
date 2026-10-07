"""Outbound send paths: chat-token targets, non-numeric reply anchors, and
what a voice call speaks.

Ported from upstream 2.0.0 (#40, #41, #48).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import adapter
from adapter import DeltaChatAdapter, _quote_id


@pytest.fixture
def connected(platform_config, mock_rpc, monkeypatch):
    a = DeltaChatAdapter(platform_config)
    a.rpc = mock_rpc
    a.account_id = 1
    mock_rpc.send_msg = AsyncMock(return_value=123)
    mock_rpc.get_config = AsyncMock(return_value=None)
    monkeypatch.setattr(adapter, "_chat_token_to_id", {"79489f9c02ceb390": 42})
    return a


class TestQuoteId:
    def test_numeric_ids_are_quoted(self):
        assert _quote_id("1756") == 1756
        assert _quote_id(1756) == 1756

    @pytest.mark.parametrize("anchor", [None, "", "callend-35422583", "abc"])
    def test_synthetic_anchors_send_unquoted(self, anchor):
        assert _quote_id(anchor) is None


class TestChatTokenTarget:
    """A cron job the agent scheduled carries the chat token, not the id."""

    @pytest.mark.asyncio
    async def test_token_is_resolved_on_text_send(self, connected):
        result = await connected.send("79489f9c02ceb390", "hi")
        assert result.success is True
        assert connected.rpc.send_msg.await_args.args[1] == 42

    @pytest.mark.asyncio
    async def test_token_is_resolved_on_attachment_send(self, connected):
        result = await connected._send_msg_data(
            "79489f9c02ceb390", "files", "file x", file="/tmp/x", text=""
        )
        assert result.success is True
        assert connected.rpc.send_msg.await_args.args[1] == 42

    @pytest.mark.asyncio
    async def test_numeric_id_skips_the_lookup(self, connected):
        await connected.send("789", "hi")
        assert connected.rpc.send_msg.await_args.args[1] == 789
        connected.rpc.get_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_token_fails_readably(self, connected):
        result = await connected.send("deadbeefdeadbeef", "hi")
        assert result.success is False
        assert "unknown Delta Chat chat id or token" in result.error
        connected.rpc.send_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_numeric_reply_to_sends_without_quote(self, connected):
        result = await connected.send("789", "hi", reply_to="not-a-msg-id")
        assert result.success is True
        assert connected.rpc.send_msg.await_args.args[2].quoted_message_id is None


class TestCallSends:
    def _in_call(self, connected, monkeypatch, marks_final=True):
        monkeypatch.setattr(adapter, "_BASE_MARKS_FINAL_REPLY", marks_final)
        mgr = MagicMock()
        mgr.has_active_call.return_value = True
        mgr.is_call_thread.return_value = True
        mgr.consume_call_ack.return_value = False
        mgr.is_call_end_reply.return_value = False
        mgr.play_response = AsyncMock()
        connected._call_manager = mgr
        return mgr

    @pytest.mark.asyncio
    async def test_status_send_is_not_spoken(self, connected, monkeypatch):
        mgr = self._in_call(connected, monkeypatch)
        await connected.send(
            "12", "💾 Memory updated", metadata={"thread_id": "call-5"}
        )
        mgr.play_response.assert_not_called()
        mgr.consume_call_ack.assert_not_called()  # status must not eat the ack drop
        connected.rpc.send_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_final_reply_is_spoken(self, connected, monkeypatch):
        mgr = self._in_call(connected, monkeypatch)
        await connected.send(
            "12", "Hello!", metadata={"thread_id": "call-5", "notify": True}
        )
        mgr.play_response.assert_called_once_with("12", "Hello!")

    @pytest.mark.asyncio
    async def test_older_core_without_the_flag_still_speaks(
        self, connected, monkeypatch
    ):
        """No notify marking at all → filtering on it would silence every call."""
        mgr = self._in_call(connected, monkeypatch, marks_final=False)
        await connected.send("12", "Hello!", metadata={"thread_id": "call-5"})
        mgr.play_response.assert_called_once_with("12", "Hello!")

    @pytest.mark.asyncio
    async def test_call_end_note_reply_is_dropped_before_call_routing(
        self, connected, monkeypatch
    ):
        mgr = self._in_call(connected, monkeypatch)
        mgr.is_call_end_reply.return_value = True
        result = await connected.send("12", "Noted.", reply_to="callend-1")
        assert result.success is True
        mgr.play_response.assert_not_called()
        connected.rpc.send_msg.assert_not_awaited()


class TestSendVideo:
    @pytest.mark.asyncio
    async def test_video_is_sent_natively_with_keyword_args(self, connected):
        """Cron delivery calls send_video(chat_id=, video_path=, metadata=)."""
        result = await connected.send_video(
            chat_id="789", video_path="/tmp/clip.mp4", metadata={}
        )
        assert result.success is True
        data = connected.rpc.send_msg.await_args.args[2]
        assert data.file == "/tmp/clip.mp4"
        assert str(getattr(data.viewtype, "value", data.viewtype)) == "Video"
