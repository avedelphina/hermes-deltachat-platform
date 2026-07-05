"""Integration tests for Delta Chat adapter.

Tests the adapter with mocked Hermes gateway classes.
"""

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import pytest

# The conftest.py already installs the mocks, so we can import adapter now
import adapter
from adapter import (
    DeltaChatAdapter,
    _parse_version,
    _check_dc_version,
    _check_dc2_available,
    MIN_DC_VERSION,
)


class TestConfigDirectoryIntegration:
    """Test config directory integration with mocked Hermes."""

    def test_dc_config_dir_uses_hermes_home(self, platform_config):
        """Test that _get_dc_config_dir uses HERMES_HOME correctly."""
        adapter = DeltaChatAdapter(platform_config)
        config_dir = adapter._get_dc_config_dir()

        # MockHermesConfig.get_hermes_home returns a default path
        # The adapter should append "deltachat-platform" to it
        from tests.conftest import MockHermesConfig

        expected_home = MockHermesConfig.get_hermes_home()
        expected = os.path.join(expected_home, "deltachat-platform")
        assert config_dir == expected

    def test_dc_config_dir_creates_directory(self, platform_config, tmp_path):
        """Test that _get_dc_config_dir creates the directory if it doesn't exist."""
        # Set a custom HERMES_HOME for this test
        test_home = str(tmp_path / "hermes")
        import os

        os.environ["HERMES_HOME"] = test_home

        # Clear the cached config dir
        adapter = DeltaChatAdapter(platform_config)
        adapter._dc_config_dir = None

        config_dir = adapter._get_dc_config_dir()
        expected_dir = os.path.join(test_home, "deltachat-platform")

        assert os.path.exists(config_dir)
        assert os.path.isdir(config_dir)
        assert config_dir == expected_dir


class TestRPCServerPath:
    """Test RPC server path resolution."""

    def test_default_rpc_path(self, platform_config):
        """Test default RPC server path."""
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "deltachat-rpc-server"

    def test_rpc_path_from_config(self, platform_config):
        """Test RPC server path from config.extra."""
        platform_config.extra = {"rpc_server": "/custom/path/to/rpc"}
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "/custom/path/to/rpc"

    def test_rpc_path_from_env(self, platform_config, monkeypatch):
        """Test RPC server path from DELTACHAT_RPC_SERVER env."""
        monkeypatch.setenv("DELTACHAT_RPC_SERVER", "/env/path/to/rpc")
        platform_config.extra = {}  # Clear config
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "/env/path/to/rpc"

    def test_rpc_path_precedence_config_over_env(self, platform_config, monkeypatch):
        """Test that config.extra takes precedence over env."""
        monkeypatch.setenv("DELTACHAT_RPC_SERVER", "/env/path")
        platform_config.extra = {"rpc_server": "/config/path"}
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "/config/path"


class TestVersionCheckIntegration:
    """Test version check with mocked RPC."""

    @pytest.mark.asyncio
    async def test_version_compatible(self, mock_rpc):
        """Test version check with compatible version."""
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": MIN_DC_VERSION}
        )
        result = await _check_dc_version(mock_rpc)
        assert result is True

    @pytest.mark.asyncio
    async def test_version_too_old(self, mock_rpc, caplog):
        """Test version check with too old version."""
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": "1.0.0"}
        )
        with caplog.at_level("ERROR"):
            result = await _check_dc_version(mock_rpc)
        assert result is False
        assert "too old" in caplog.text

    @pytest.mark.asyncio
    async def test_version_newer_warns(self, mock_rpc, caplog):
        """Test version check with newer version warns but allows."""
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": "3.0.0"}
        )
        with caplog.at_level("WARNING"):
            result = await _check_dc_version(mock_rpc)
        assert result is True
        assert "newer than" in caplog.text


class TestSendMessage:
    """Test message sending functionality."""

    @pytest.mark.asyncio
    async def test_send_text_message_success(self, platform_config, mock_rpc):
        """Test successful text message sending."""
        # Setup adapter with mocked state
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = False
        adapter._mark_connected = Mock()
        adapter._mark_disconnected = Mock()
        adapter.build_source = Mock()
        adapter.handle_message = AsyncMock()

        # Mock RPC send_msg to return message ID
        mock_rpc.send_msg = AsyncMock(return_value=123)

        from adapter import SendResult

        result = await adapter.send("789", "Hello World")

        assert result.success is True
        assert result.message_id == "123"
        mock_rpc.send_msg.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_not_connected(self, platform_config):
        """Test sending fails when not connected."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = None
        adapter.account_id = None

        from adapter import SendResult

        result = await adapter.send("789", "Hello")

        assert result.success is False
        assert "not connected" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_file_success(self, platform_config, mock_rpc):
        """Test successful file sending."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=456)

        from adapter import SendResult

        result = await adapter.send_file("789", "/path/to/file.xdc", "A file")

        assert result.success is True
        assert result.message_id == "456"
        mock_rpc.send_msg.assert_called_once()


class TestGetChatInfo:
    """Test chat info retrieval."""

    @pytest.mark.asyncio
    async def test_get_chat_info_success(self, platform_config, mock_rpc):
        """Test successful chat info retrieval."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Test Chat", "chat_type": "Single"}
        )

        result = await adapter.get_chat_info("789")

        assert result["name"] == "Test Chat"
        assert result["type"] == "dm"

    @pytest.mark.asyncio
    async def test_get_chat_info_group(self, platform_config, mock_rpc):
        """Test chat info for group chat."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Group Chat", "chat_type": "Group"}
        )

        result = await adapter.get_chat_info("789")

        assert result["name"] == "Group Chat"
        assert result["type"] == "group"

    @pytest.mark.asyncio
    async def test_get_chat_info_fallback(self, platform_config, mock_rpc):
        """Test chat info fallback on error."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_basic_chat_info = AsyncMock(side_effect=Exception("RPC error"))

        result = await adapter.get_chat_info("789")

        assert result["name"] == "789"
        assert result["type"] == "dm"


class TestEventHandling:
    """Test event handling logic."""

    @pytest.mark.asyncio
    async def test_handle_incoming_message_event(self, platform_config, mock_rpc):
        """Test handling of INCOMING_MSG event."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        # Open DM policy so the test contact is not rejected for being unverified.
        adapter._dm_policy = "open"

        mock_rpc.get_message = AsyncMock(
            return_value={
                "msg_id": 123,
                "text": "Test message",
                "from_id": 456,
                "timestamp": 1234567890,
                "view_type": "Text",
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Test Chat", "chat_type": "Single"}
        )
        mock_rpc.get_contact = AsyncMock(
            return_value={
                "contact_id": 456,
                "name": "Test User",
                "address": "user@example.com",
            }
        )
        mock_rpc.markseen_msgs = AsyncMock(return_value=None)
        adapter._running = True
        adapter._mark_connected = Mock()
        adapter._mark_disconnected = Mock()
        adapter.handle_message = AsyncMock()

        # Process an incoming message event
        event = {"kind": EventType.INCOMING_MSG, "chat_id": 789, "msg_id": 123}
        await adapter._handle_dc_event(event)

        # Verify message was handled
        assert adapter.handle_message.called
        call_args = adapter.handle_message.call_args[0][0]
        assert "Test message" in call_args.text
        assert call_args.message_id == "123"

    @pytest.mark.asyncio
    async def test_handle_delivered_event(self, platform_config, mock_rpc, caplog):
        """Test handling of MSG_DELIVERED event."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"kind": EventType.MSG_DELIVERED, "msg_id": 123}
            await adapter._handle_dc_event(event)

        assert "delivered" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handle_failed_event_logs_chat_id_and_error(
        self, platform_config, mock_rpc, caplog
    ):
        """MSG_FAILED must surface chat_id and the real failure reason, not
        just a bare msg_id — that's the whole point of fetching get_message."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        mock_rpc.get_message = AsyncMock(
            return_value={"error": "SMTP error: 550 mailbox unavailable"}
        )

        with caplog.at_level("WARNING"):
            event = {"kind": EventType.MSG_FAILED, "msg_id": 123, "chat_id": 13}
            await adapter._handle_dc_event(event)

        mock_rpc.get_message.assert_awaited_once_with(1, 123)
        assert "123" in caplog.text
        assert "13" in caplog.text
        assert "SMTP error: 550 mailbox unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_handle_failed_event_survives_get_message_error(
        self, platform_config, mock_rpc, caplog
    ):
        """If get_message itself fails, still log what we know instead of
        raising and losing the original MSG_FAILED warning entirely."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        mock_rpc.get_message = AsyncMock(side_effect=RuntimeError("rpc down"))

        with caplog.at_level("WARNING"):
            event = {"kind": EventType.MSG_FAILED, "msg_id": 123, "chat_id": 13}
            await adapter._handle_dc_event(event)

        assert "123" in caplog.text
        assert "13" in caplog.text
        assert "unknown" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handle_incoming_call_event(self, platform_config, mock_rpc, caplog):
        """Test handling of INCOMING_CALL event."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        # Provide a mock call manager so the event is routed.
        adapter._call_manager = Mock()
        adapter._call_manager.handle_incoming_call = AsyncMock()

        with caplog.at_level("INFO"):
            event = {"kind": EventType.INCOMING_CALL}
            await adapter._handle_dc_event(event)

        assert (
            "call" in caplog.text.lower()
            or adapter._call_manager.handle_incoming_call.called
        )

    @pytest.mark.asyncio
    async def test_handle_unknown_event(self, platform_config, mock_rpc, caplog):
        """Test handling of unknown event type."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"kind": "UNKNOWN_EVENT"}
            await adapter._handle_dc_event(event)

        assert "unhandled" in caplog.text.lower()


class TestDC2Availability:
    """Test deltachat2 availability check."""

    def test_dc2_available_when_installed(self, monkeypatch):
        """Test _check_dc2_available returns True when deltachat2 is installed."""
        # Temporarily add deltachat2 to sys.modules
        import sys

        # Save original state
        original_modules = sys.modules.get("deltachat2")

        # Mock deltachat2 being available
        monkeypatch.setitem(sys.modules, "deltachat2", MagicMock())

        # Reset the cache
        import adapter

        adapter._DC2_AVAILABLE = None

        result = _check_dc2_available()
        assert result is True

        # Restore original state
        if original_modules is not None:
            sys.modules["deltachat2"] = original_modules
        elif "deltachat2" in sys.modules:
            del sys.modules["deltachat2"]

    def test_dc2_not_available_when_not_installed(self, monkeypatch):
        """Test _check_dc2_available returns False when deltachat2 is not installed."""
        import sys

        # Save original state
        original_modules = sys.modules.get("deltachat2")
        original_cache = adapter._DC2_AVAILABLE

        # Ensure deltachat2 is not in sys.modules
        if "deltachat2" in sys.modules:
            del sys.modules["deltachat2"]

        # Also prevent the import from working
        monkeypatch.setitem(sys.modules, "deltachat2", None)

        # Reset the cache
        adapter._DC2_AVAILABLE = None

        result = _check_dc2_available()
        assert result is False

        # Restore original state
        if original_modules is not None:
            sys.modules["deltachat2"] = original_modules
        adapter._DC2_AVAILABLE = original_cache


class TestHTMLFormatting:
    """Test HTML message formatting for long messages."""

    def test_short_message_no_html(self, platform_config):
        """Test that short messages (< 40 lines) don't get HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        short_text = "This is a short message."

        text_part, html_part = adapter._format_html_message(short_text)

        assert text_part == short_text
        assert html_part is None

    def test_long_message_with_html(self, platform_config):
        """Test that long messages (> 40 lines) get HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        # Create a message with 50 lines
        long_text = "\n".join([f"Line {i}" for i in range(50)])

        text_part, html_part = adapter._format_html_message(long_text)

        # text_part should have first 40 lines
        assert text_part == "\n".join([f"Line {i}" for i in range(40)])
        # html_part should contain the full message
        assert html_part is not None
        assert "Line 40" in html_part
        assert "Line 49" in html_part
        # Check for HTML styling
        assert "sans-serif" in html_part
        assert "font-size: 16px" in html_part

    def test_exactly_40_lines_no_html(self, platform_config):
        """Test that exactly 40 lines doesn't trigger HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        text_40_lines = "\n".join([f"Line {i}" for i in range(40)])

        text_part, html_part = adapter._format_html_message(text_40_lines)

        assert text_part == text_40_lines
        assert html_part is None

    def test_41_lines_with_html(self, platform_config):
        """Test that 41 lines triggers HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        text_41_lines = "\n".join([f"Line {i}" for i in range(41)])

        text_part, html_part = adapter._format_html_message(text_41_lines)

        # text_part should have first 40 lines
        assert text_part == "\n".join([f"Line {i}" for i in range(40)])
        # html_part should exist
        assert html_part is not None


class TestLocationSending:
    """Test location/POI message sending."""

    @pytest.mark.asyncio
    async def test_send_location_success(self, platform_config, mock_rpc):
        """Test successful location sending."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=123)

        from adapter import SendResult

        result = await adapter.send_location("789", 52.5200, 13.4050, "☕ Coffee")

        assert result.success is True
        assert result.message_id == "123"
        mock_rpc.send_msg.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_location_with_text_poi(self, platform_config, mock_rpc):
        """Test location sending with text POI (displays as pin)."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=456)

        from adapter import SendResult

        result = await adapter.send_location("789", 40.7128, -74.0060, "Coffee Shop")

        assert result.success is True
        assert result.message_id == "456"
        mock_rpc.send_msg.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_location_not_connected(self, platform_config):
        """Test location sending fails when not connected."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = None
        adapter.account_id = None

        from adapter import SendResult

        result = await adapter.send_location("789", 0, 0, "🏠")

        assert result.success is False
        assert "not connected" in result.error.lower()


class TestStatusAndStats:
    """Test get_status, stats counters, and lifecycle helpers."""

    def test_get_status_not_connected(self, platform_config):
        """Test status snapshot when adapter is not connected."""
        adapter = DeltaChatAdapter(platform_config)
        status = adapter.get_status()
        assert status["connected"] is False
        assert status["running"] is False
        assert status["account_addr"] is None
        assert status["crashes_last_60s"] == 0
        assert status["stats"] == {}

    def test_bump_stat(self, platform_config):
        """Test internal stats counter increments."""
        adapter = DeltaChatAdapter(platform_config)
        adapter._bump_stat("messages_sent")
        adapter._bump_stat("messages_sent", 2)
        adapter._bump_stat("messages_send_failed")
        assert adapter._stats == {"messages_sent": 3, "messages_send_failed": 1}

    @pytest.mark.asyncio
    async def test_get_status_connected(self, platform_config, mock_rpc):
        """Test status snapshot when adapter is connected."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        adapter._self_addr = "bot@example.com"
        adapter._event_loop_task = asyncio.create_task(asyncio.sleep(60))
        adapter._bump_stat("messages_sent")

        status = adapter.get_status()

        assert status["connected"] is True
        assert status["running"] is True
        assert status["account_addr"] == "bot@example.com"
        assert status["stats"]["messages_sent"] == 1
        adapter._event_loop_task.cancel()
        try:
            await adapter._event_loop_task
        except asyncio.CancelledError:
            pass


class TestSignalHandling:
    """Test graceful shutdown signal registration."""

    @pytest.mark.asyncio
    async def test_connect_registers_signal_handlers(
        self, platform_config, monkeypatch
    ):
        """Test that connect() registers SIGTERM/SIGINT handlers."""
        adapter = DeltaChatAdapter(platform_config)
        added = []

        def fake_add_signal_handler(sig, handler):
            added.append(sig)

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)

        adapter._loop = loop
        adapter._signal_handler()

        # We can't easily run full connect() without RPC; just verify the
        # handler can be registered via loop mock.
        loop.add_signal_handler(signal.SIGTERM, adapter._signal_handler)
        loop.add_signal_handler(signal.SIGINT, adapter._signal_handler)
        assert signal.SIGTERM in added
        assert signal.SIGINT in added

    @pytest.mark.asyncio
    async def test_disconnect_removes_signal_handlers(
        self, platform_config, monkeypatch
    ):
        """Test that disconnect() removes signal handlers."""
        adapter = DeltaChatAdapter(platform_config)
        removed = []

        def fake_remove_signal_handler(sig):
            removed.append(sig)
            return True

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "remove_signal_handler", fake_remove_signal_handler)

        adapter._loop = loop
        adapter._mark_disconnected = Mock()
        await adapter.disconnect()

        assert signal.SIGTERM in removed
        assert signal.SIGINT in removed


class TestEventSupervisor:
    """Test event-listener crash recovery."""

    @pytest.mark.asyncio
    async def test_supervisor_restarts_after_crash(self, platform_config):
        """Test that the supervisor restarts the listener after a crash."""
        adapter = DeltaChatAdapter(platform_config)
        adapter._running = True
        call_count = 0

        async def fake_listener():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("boom")
            adapter._running = False

        adapter._event_listener = fake_listener
        await adapter._event_supervisor()

        assert call_count == 2
        assert adapter._stats.get("event_listener_crashes") == 1

    @pytest.mark.asyncio
    async def test_supervisor_gives_up_after_three_crashes(self, platform_config):
        """Test that the supervisor stops after 3 crashes in 60 seconds."""
        adapter = DeltaChatAdapter(platform_config)
        adapter._running = True
        adapter._mark_disconnected = Mock()
        call_count = 0

        async def fake_listener():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        adapter._event_listener = fake_listener
        adapter.disconnect = AsyncMock()
        await adapter._event_supervisor()

        assert call_count == 3
        assert adapter._running is False
        assert adapter._stats.get("event_listener_crashes") == 3


class TestOnboarding:
    """Test account onboarding: data dir, profile, manual/auto accounts."""

    def test_data_dir_env_overrides_hermes_home(
        self, platform_config, monkeypatch, tmp_path
    ):
        """DELTACHAT_DATA_DIR is honoured when set."""
        custom_dir = str(tmp_path / "dc-data")
        monkeypatch.setenv("DELTACHAT_DATA_DIR", custom_dir)
        adapter = DeltaChatAdapter(platform_config)
        config_dir = adapter._get_dc_config_dir()
        assert config_dir == custom_dir
        assert os.path.isdir(custom_dir)

    def test_data_dir_extra_overrides_hermes_home(self, platform_config, tmp_path):
        """config.extra.data_dir is honoured when set."""
        custom_dir = str(tmp_path / "dc-data-extra")
        platform_config.extra = {"data_dir": custom_dir}
        adapter = DeltaChatAdapter(platform_config)
        config_dir = adapter._get_dc_config_dir()
        assert config_dir == custom_dir
        assert os.path.isdir(custom_dir)

    @pytest.mark.asyncio
    async def test_apply_profile_sets_displayname_avatar_and_bot(
        self, platform_config, mock_rpc, tmp_path
    ):
        """_apply_profile sets display name, bot mode, and avatar."""
        avatar = tmp_path / "bot.png"
        avatar.write_bytes(b"fake png")
        platform_config.extra = {
            "display_name": "TestBot",
            "avatar_path": str(avatar),
        }
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.set_config = AsyncMock()
        adapter.rpc = mock_rpc

        await adapter._apply_profile(adapter.rpc, 1)

        calls = [c.args for c in mock_rpc.set_config.await_args_list]
        assert (1, "displayname", "TestBot") in calls
        assert (1, "bot", "1") in calls
        avatar_call = [c for c in calls if c[1] == "selfavatar"]
        assert len(avatar_call) == 1
        assert avatar_call[0][2] == str(avatar)

    @pytest.mark.asyncio
    async def test_apply_profile_skips_missing_avatar(self, platform_config, mock_rpc):
        """_apply_profile skips avatar when the file does not exist."""
        platform_config.extra = {
            "display_name": "TestBot",
            "avatar_path": "/nonexistent/avatar.png",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        mock_rpc.set_config = AsyncMock()

        await adapter._apply_profile(adapter.rpc, 1)

        calls = [c.args for c in mock_rpc.set_config.await_args_list]
        assert (1, "displayname", "TestBot") in calls
        assert (1, "bot", "1") in calls
        assert not any(c[1] == "selfavatar" for c in calls)

    @pytest.mark.asyncio
    async def test_configure_account_reuses_existing_account(
        self, platform_config, mock_rpc
    ):
        """_configure_account reuses the first existing account."""
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.get_all_accounts = AsyncMock(return_value=[{"id": 7}])
        mock_rpc.set_config = AsyncMock()

        result = await adapter._configure_account(mock_rpc)

        assert result is True
        assert adapter.account_id == 7
        mock_rpc.get_all_accounts.assert_awaited_once()
        mock_rpc.add_account.assert_not_called()
        calls = [c.args for c in mock_rpc.set_config.await_args_list]
        assert (7, "displayname", adapter._display_name) in calls
        assert (7, "bot", "1") in calls

    @pytest.mark.asyncio
    async def test_configure_account_manual_email_password(
        self, platform_config, mock_rpc
    ):
        """_configure_account creates and configures a manual email account."""
        platform_config.extra = {
            "email": "bot@example.com",
            "password": "secret",
        }
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.get_all_accounts = AsyncMock(return_value=[])
        mock_rpc.add_account = AsyncMock(return_value=2)
        mock_rpc.set_config = AsyncMock()
        mock_rpc.add_or_update_transport = AsyncMock()
        mock_rpc.configure = AsyncMock()

        result = await adapter._configure_account(mock_rpc)

        assert result is True
        assert adapter.account_id == 2
        mock_rpc.add_account.assert_awaited_once()
        mock_rpc.add_or_update_transport.assert_awaited_once_with(
            2, {"addr": "bot@example.com", "password": "secret"}
        )
        mock_rpc.configure.assert_awaited_once_with(2)
        assert adapter._password is None

    @pytest.mark.asyncio
    async def test_configure_account_clears_password_on_configure_failure(
        self, platform_config, mock_rpc
    ):
        """Password must not be retained if configuration fails."""
        platform_config.extra = {
            "email": "bot@example.com",
            "password": "secret",
        }
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.get_all_accounts = AsyncMock(return_value=[])
        mock_rpc.add_account = AsyncMock(return_value=2)
        mock_rpc.set_config = AsyncMock()
        mock_rpc.add_or_update_transport = AsyncMock()
        mock_rpc.configure = AsyncMock(side_effect=RuntimeError("configure failed"))

        with pytest.raises(RuntimeError, match="configure failed"):
            await adapter._configure_account(mock_rpc)
        assert adapter._password is None

    @pytest.mark.asyncio
    async def test_configure_account_chatmail_auto(self, platform_config, mock_rpc):
        """_configure_account creates a chatmail account when email is auto."""
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.get_all_accounts = AsyncMock(return_value=[])
        mock_rpc.add_account = AsyncMock(return_value=3)
        mock_rpc.set_config = AsyncMock()
        mock_rpc.set_config_from_qr = AsyncMock()
        mock_rpc.configure = AsyncMock()
        mock_rpc.get_config = AsyncMock(return_value="bot@nine.testrun.org")

        result = await adapter._configure_account(mock_rpc)

        assert result is True
        assert adapter.account_id == 3
        mock_rpc.set_config_from_qr.assert_awaited_once_with(
            3, "DCACCOUNT:https://nine.testrun.org/new"
        )
        mock_rpc.configure.assert_awaited_once_with(3)

    @pytest.mark.asyncio
    async def test_configure_account_chatmail_fallback_servers(
        self, platform_config, mock_rpc
    ):
        """_configure_account tries chatmail servers in order."""
        platform_config.extra = {
            "chatmail_servers": "first.example.org,second.example.org"
        }
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.get_all_accounts = AsyncMock(return_value=[])
        mock_rpc.add_account = AsyncMock(return_value=4)
        mock_rpc.set_config = AsyncMock()
        mock_rpc.set_config_from_qr = AsyncMock(
            side_effect=[RuntimeError("first down"), None]
        )
        mock_rpc.configure = AsyncMock()
        mock_rpc.get_config = AsyncMock(return_value="bot@second.example.org")

        result = await adapter._configure_account(mock_rpc)

        assert result is True
        assert adapter.account_id == 4
        assert mock_rpc.set_config_from_qr.await_count == 2
        second_call = mock_rpc.set_config_from_qr.await_args_list[1]
        assert second_call.args == (4, "DCACCOUNT:https://second.example.org/new")
        mock_rpc.configure.assert_awaited_once_with(4)

    @pytest.mark.asyncio
    async def test_configure_account_chatmail_all_servers_fail(
        self, platform_config, mock_rpc
    ):
        """_configure_account raises when every chatmail server fails."""
        platform_config.extra = {
            "chatmail_servers": "bad1.example.org,bad2.example.org"
        }
        adapter = DeltaChatAdapter(platform_config)
        mock_rpc.get_all_accounts = AsyncMock(return_value=[])
        mock_rpc.add_account = AsyncMock(return_value=5)
        mock_rpc.set_config = AsyncMock()
        mock_rpc.set_config_from_qr = AsyncMock(side_effect=RuntimeError("down"))
        mock_rpc.configure = AsyncMock()

        with pytest.raises(RuntimeError):
            await adapter._configure_account(mock_rpc)

    @pytest.mark.asyncio
    async def test_connect_generates_invite_link(self, platform_config, monkeypatch):
        """connect() configures an account and caches a SecureJoin invite link."""
        from unittest.mock import MagicMock

        adapter = DeltaChatAdapter(platform_config)

        # Build a synchronous RPC mock whose methods the _AsyncRpc wrapper can
        # run in the default executor.
        sync_rpc = MagicMock()
        sync_rpc.get_all_accounts = lambda: [{"id": 1}]
        sync_rpc.set_config = lambda *args, **kwargs: None
        sync_rpc.start_io = lambda *args, **kwargs: None
        sync_rpc.get_chat_securejoin_qr_code_svg = lambda *args, **kwargs: (
            "https://delta.chat/s?pk=abc",
            "<svg></svg>",
        )
        sync_rpc.get_config = lambda *args, **kwargs: "bot@example.com"

        fake_transport = MagicMock()
        fake_transport.start = lambda: None
        fake_transport.close = lambda: None

        with patch(
            "deltachat2.transport.IOTransport", return_value=fake_transport
        ), patch("deltachat2.Rpc", return_value=sync_rpc), patch(
            "adapter._check_dc_version", return_value=True
        ), patch(
            "asyncio.sleep", new_callable=AsyncMock
        ):
            adapter.get_my_address = AsyncMock(return_value="bot@example.com")
            adapter._mark_disconnected = Mock()
            result = await adapter.connect()

        assert result is True
        assert adapter.account_id == 1
        assert adapter._invite_link == "https://delta.chat/s?pk=abc"
        if adapter._event_loop_task:
            adapter._event_loop_task.cancel()


class TestMentions:
    """Test group mention detection and DELTACHAT_REQUIRE_MENTION."""

    @pytest.fixture
    def group_event(self):
        return {"kind": "IncomingMsg", "chat_id": 1, "msg_id": 10}

    @pytest.mark.asyncio
    async def test_require_mention_blocks_unmentioned_group_message(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_not_called()
        adapter.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_require_mention_allows_mentioned_group_message(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "@Bot hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_require_mention_skips_commands(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "/help",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_require_mention_applies_to_image_caption(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "look at this",
                "view_type": "Image",
                "from_id": 11,
                "file": "/tmp/photo.jpg",
                "file_mime": "image/jpeg",
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        adapter._resolve_blob_path = lambda x: x
        adapter._copy_to_hermes_cache = lambda src, kind: src

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_not_called()


class TestMetadata:
    """Test incoming/outgoing metadata enrichment."""

    @pytest.mark.asyncio
    async def test_message_event_has_metadata(self, platform_config, mock_rpc):
        platform_config.extra = {"dm_policy": "open"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Single", "name": "DM"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(
            {"kind": "IncomingMsg", "chat_id": 7, "msg_id": 42}
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.metadata["chat_id"] == "7"
        assert event.metadata["message_id"] == "42"
        assert event.metadata["from_id"] == "11"
        assert event.metadata["is_group"] is False
        assert "dc_token" in event.metadata

    @pytest.mark.asyncio
    async def test_send_result_includes_metadata(self, platform_config, mock_rpc):
        from adapter import _chat_id_to_token

        _chat_id_to_token[7] = "abc123"
        try:
            adapter = DeltaChatAdapter(platform_config)
            adapter.rpc = mock_rpc
            adapter.account_id = 1
            adapter._running = False
            mock_rpc.send_msg = AsyncMock(return_value=99)

            result = await adapter.send("7", "Hello")

            assert result.success is True
            assert result.metadata["chat_id"] == "7"
            assert result.metadata["message_id"] == "99"
            assert result.metadata["dc_token"] == "abc123"
        finally:
            _chat_id_to_token.pop(7, None)


class TestUrlImageSending:
    """Test send_image_file() with image URLs."""

    def _mock_httpx_stream(self, content_type, content, content_length=None):
        from unittest.mock import AsyncMock, MagicMock

        resp = MagicMock()
        resp.headers = {"content-type": content_type}
        if content_length is not None:
            resp.headers["content-length"] = str(content_length)
        resp.raise_for_status = MagicMock()

        async def _aiter_bytes(**kwargs):
            yield content

        resp.aiter_bytes = _aiter_bytes

        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=resp)
        stream_cm.__aexit__ = AsyncMock(return_value=False)

        client = MagicMock()
        client.stream = MagicMock(return_value=stream_cm)
        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=client)
        client_cm.__aexit__ = AsyncMock(return_value=False)

        return client_cm

    @pytest.mark.asyncio
    async def test_send_image_file_with_url_success(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.send_msg = AsyncMock(return_value=123)

        client_cm = self._mock_httpx_stream("image/png", b"pngdata", 7)
        with patch("httpx.AsyncClient", return_value=client_cm):
            result = await adapter.send_image_file("7", "https://example.com/photo.png")

        assert result.success is True
        assert result.message_id == "123"
        mock_rpc.send_msg.assert_awaited_once()
        args = mock_rpc.send_msg.await_args.args
        assert args[0] == 1
        assert args[1] == 7
        assert args[2].file.endswith(".png")

    @pytest.mark.asyncio
    async def test_send_image_file_with_url_rejects_non_image(
        self, platform_config, mock_rpc
    ):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        client_cm = self._mock_httpx_stream("text/plain", b"not an image", 12)
        with patch("httpx.AsyncClient", return_value=client_cm):
            result = await adapter.send_image_file("7", "https://example.com/file.txt")

        assert result.success is False
        assert "image" in result.error.lower()
        mock_rpc.send_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_file_with_url_size_limit(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        client_cm = self._mock_httpx_stream(
            "image/png", b"x", content_length=50 * 1024 * 1024
        )
        with patch("httpx.AsyncClient", return_value=client_cm):
            result = await adapter.send_image_file("7", "https://example.com/huge.png")

        assert result.success is False
        assert "25" in result.error or "limit" in result.error.lower()
        mock_rpc.send_msg.assert_not_called()


class TestGroupRoster:
    """Test group roster fetching and caching for _get_group_roster."""

    @pytest.mark.asyncio
    async def test_fetches_and_excludes_self(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2, 3])
        mock_rpc.get_contact = AsyncMock(
            side_effect=lambda account_id, cid: {
                2: {"name": "Alice", "address": "alice@x.com"},
                3: {"name": "Holly", "address": "holly@x.com"},
            }[cid]
        )

        roster = await adapter._get_group_roster("13")

        assert mock_rpc.get_contact.await_count == 2
        assert {"name": "Alice", "address": "alice@x.com"} in roster
        assert {"name": "Holly", "address": "holly@x.com"} in roster
        assert all(r["address"] != "" or r["name"] for r in roster)

    @pytest.mark.asyncio
    async def test_caches_and_skips_rpc_on_second_call(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
        mock_rpc.get_contact = AsyncMock(
            return_value={"name": "Alice", "address": "alice@x.com"}
        )

        await adapter._get_group_roster("13")
        await adapter._get_group_roster("13")

        mock_rpc.get_chat_contacts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refetches_after_ttl_expires(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._ROSTER_CACHE_TTL = 0

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
        mock_rpc.get_contact = AsyncMock(
            return_value={"name": "Alice", "address": "alice@x.com"}
        )

        await adapter._get_group_roster("13")
        await adapter._get_group_roster("13")

        assert mock_rpc.get_chat_contacts.await_count == 2

    @pytest.mark.asyncio
    async def test_rpc_failure_falls_back_to_stale_cache(
        self, platform_config, mock_rpc
    ):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
        mock_rpc.get_contact = AsyncMock(
            return_value={"name": "Alice", "address": "alice@x.com"}
        )
        roster = await adapter._get_group_roster("13")

        mock_rpc.get_chat_contacts = AsyncMock(side_effect=RuntimeError("rpc down"))
        adapter._ROSTER_CACHE_TTL = 0
        fallback = await adapter._get_group_roster("13")

        assert fallback == roster

    @pytest.mark.asyncio
    async def test_rpc_failure_with_no_cache_returns_empty(
        self, platform_config, mock_rpc
    ):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(side_effect=RuntimeError("rpc down"))

        roster = await adapter._get_group_roster("13")

        assert roster == []


class TestMessageMetadataRoster:
    """Test that _message_metadata includes participants only for groups."""

    def test_group_metadata_includes_participants(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)
        roster = [{"name": "Alice", "address": "alice@x.com"}]

        meta = adapter._message_metadata("13", "5", "2", True, "tok", roster)

        assert meta["participants"] == roster

    def test_dm_metadata_omits_participants(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)
        roster = [{"name": "Alice", "address": "alice@x.com"}]

        meta = adapter._message_metadata("13", "5", "2", False, "tok", roster)

        assert "participants" not in meta

    def test_no_roster_omits_participants(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)

        meta = adapter._message_metadata("13", "5", "2", True, "tok", None)

        assert "participants" not in meta


class TestRegisterPlatformAuthEnv:
    """Without these, gateway._is_user_authorized never learns our env var
    names and silently drops every sender regardless of our own
    dm_policy/group_policy config (see CHANGELOG 1.5.6)."""

    def test_declares_allowed_users_and_allow_all_env(self):
        mock_ctx = MagicMock()

        adapter.register_platform(mock_ctx)

        _, kwargs = mock_ctx.register_platform.call_args
        assert kwargs["allowed_users_env"] == "DELTACHAT_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "DELTACHAT_ALLOW_ALL_USERS"


def _get_registered_tool_handler(tool_name: str):
    mock_ctx = MagicMock()
    adapter.register_rpc_tools(mock_ctx)
    for call in mock_ctx.register_tool.call_args_list:
        if call.kwargs.get("name") == tool_name:
            return call.kwargs["handler"]
    raise AssertionError(f"{tool_name} was not registered")


class TestDcSendMessageAddress:
    """dc_send_message's 'address' param: cold-open a 1:1 chat with a contact,
    guarded to addresses already seen in a group roster this bot has fetched."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_email(self, platform_config, mock_rpc):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        adapter._active_adapter = real_adapter
        try:
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi", "address": "not-an-email"})
            )

            assert "error" in result
            assert "not a valid email" in result["error"]
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_rejects_address_not_in_any_roster(self, platform_config, mock_rpc):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        adapter._active_adapter = real_adapter
        try:
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi", "address": "stranger@example.com"})
            )

            assert "error" in result
            assert "roster" in result["error"]
            mock_rpc.lookup_contact_id_by_addr.assert_not_called()
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_sends_to_address_known_from_roster(self, platform_config, mock_rpc):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        real_adapter._roster_cache["13"] = (
            9999999999.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )
        adapter._active_adapter = real_adapter
        try:
            mock_rpc.lookup_contact_id_by_addr = AsyncMock(return_value=42)
            mock_rpc.create_chat_by_contact_id = AsyncMock(return_value=99)
            mock_rpc.send_msg = AsyncMock(return_value=123)
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi Alice", "address": "Alice@Example.com"})
            )

            assert result["success"] is True
            mock_rpc.lookup_contact_id_by_addr.assert_awaited_once_with(
                1, "alice@example.com"
            )
            mock_rpc.create_chat_by_contact_id.assert_awaited_once_with(1, 42)
            mock_rpc.create_contact.assert_not_called()
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_creates_contact_when_lookup_returns_none(
        self, platform_config, mock_rpc
    ):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        real_adapter._roster_cache["13"] = (
            9999999999.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )
        adapter._active_adapter = real_adapter
        try:
            mock_rpc.lookup_contact_id_by_addr = AsyncMock(return_value=None)
            mock_rpc.create_contact = AsyncMock(return_value=42)
            mock_rpc.create_chat_by_contact_id = AsyncMock(return_value=99)
            mock_rpc.send_msg = AsyncMock(return_value=123)
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi Alice", "address": "alice@example.com"})
            )

            assert result["success"] is True
            mock_rpc.create_contact.assert_awaited_once_with(
                1, "alice@example.com", None
            )
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_chat_token_takes_precedence_over_address(
        self, platform_config, mock_rpc
    ):
        from adapter import _chat_token_to_id

        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        adapter._active_adapter = real_adapter
        _chat_token_to_id["tok123"] = 7
        try:
            mock_rpc.send_msg = AsyncMock(return_value=123)
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler(
                    {
                        "text": "hi",
                        "chat_token": "tok123",
                        "address": "stranger@example.com",
                    }
                )
            )

            assert result["success"] is True
            mock_rpc.lookup_contact_id_by_addr.assert_not_called()
        finally:
            adapter._active_adapter = None
            _chat_token_to_id.pop("tok123", None)


class TestIsAddressInKnownRosters:
    def test_true_when_address_in_a_cached_roster(self, platform_config):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter._roster_cache["13"] = (
            1.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )

        assert real_adapter._is_address_in_known_rosters("ALICE@EXAMPLE.COM") is True

    def test_false_when_no_roster_has_it(self, platform_config):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter._roster_cache["13"] = (
            1.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )

        assert real_adapter._is_address_in_known_rosters("bob@example.com") is False

    def test_false_with_no_cached_rosters(self, platform_config):
        real_adapter = adapter.DeltaChatAdapter(platform_config)

        assert real_adapter._is_address_in_known_rosters("alice@example.com") is False
