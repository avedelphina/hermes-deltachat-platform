"""Lifecycle: dead deltachat-rpc-server subprocess, and teardown resilience.

The vendored transport (post-1.7.3) raises "RPC server disconnected" once the
subprocess exits. That error alone does not escape the listen loop, so the
adapter probes process.poll() in the error path: a dead subprocess stops the
loop and escalates to the gateway (which respawns the server by rebuilding the
adapter); a live one is a transient error and is retried.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adapter import DeltaChatAdapter


@pytest.fixture
def adapter(platform_config):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a._transport = MagicMock()
    a._mark_connected()
    return a


def _server(adapter, *, exit_code):
    adapter._transport.process.poll.return_value = exit_code


class TestExitCodeProbe:
    def test_alive_server_reports_none(self, adapter):
        _server(adapter, exit_code=None)
        assert adapter._rpc_server_exit_code() is None

    def test_dead_server_reports_its_code(self, adapter):
        _server(adapter, exit_code=1)
        assert adapter._rpc_server_exit_code() == 1

    def test_exit_code_zero_still_counts_as_dead(self, adapter):
        _server(adapter, exit_code=0)
        assert adapter._rpc_server_exit_code() == 0

    def test_unstarted_transport_is_not_dead(self, adapter):
        adapter._transport = object()  # no .process attribute
        assert adapter._rpc_server_exit_code() is None

    def test_no_transport_at_all_is_not_dead(self, adapter):
        adapter._transport = None
        assert adapter._rpc_server_exit_code() is None


class TestTransientErrorsStillRetry:
    @pytest.mark.asyncio
    async def test_live_server_keeps_polling(self, adapter):
        _server(adapter, exit_code=None)
        with patch("asyncio.sleep", AsyncMock()) as sleep:
            assert await adapter._handle_listener_error(OSError("blip")) is True
        sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_live_server_does_not_escalate(self, adapter):
        _server(adapter, exit_code=None)
        with patch("asyncio.sleep", AsyncMock()):
            await adapter._handle_listener_error(OSError("blip"))
        assert adapter.has_fatal_error is False


class TestDeadServerStopsAndEscalates:
    @pytest.mark.asyncio
    async def test_stops_polling(self, adapter):
        _server(adapter, exit_code=1)
        assert await adapter._handle_listener_error(OSError("disconnected")) is False

    @pytest.mark.asyncio
    async def test_marks_a_retryable_fatal_error(self, adapter):
        _server(adapter, exit_code=1)
        await adapter._handle_listener_error(OSError("disconnected"))

        assert adapter.fatal_error_code == "rpc_server_died"
        assert adapter.fatal_error_retryable is True
        assert "1" in adapter.fatal_error_message

    @pytest.mark.asyncio
    async def test_notifies_the_gateway(self, adapter):
        _server(adapter, exit_code=1)
        handler = AsyncMock()
        adapter.set_fatal_error_handler(handler)

        await adapter._handle_listener_error(OSError("disconnected"))
        await asyncio.sleep(0)  # the notify runs as its own task

        handler.assert_awaited_once_with(adapter)

    @pytest.mark.asyncio
    async def test_does_not_escalate_during_a_deliberate_teardown(self, adapter):
        _server(adapter, exit_code=0)
        adapter._running = False  # as _cleanup() would have left it

        assert await adapter._handle_listener_error(OSError("closed")) is False
        assert adapter.has_fatal_error is False


class TestCleanupReportsStatus:
    def test_cleanup_marks_disconnected(self, adapter):
        adapter._cleanup()
        assert adapter.is_connected is False
        assert adapter._disconnected is True

    def test_cleanup_does_not_downgrade_a_fatal_error(self, adapter):
        adapter._set_fatal_error("rpc_server_died", "x", retryable=True)
        adapter._cleanup()

        assert adapter.has_fatal_error is True
        assert adapter._disconnected is False

    def test_cleanup_closes_the_transport(self, adapter):
        """Off the gateway loop: _cleanup() returns the close's future."""
        transport = MagicMock()
        adapter._transport = transport
        closing = adapter._cleanup()

        assert adapter._transport is None  # state is reset before the close runs
        assert adapter.rpc is None
        closing.result(timeout=5)
        transport.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_slow_close_does_not_block_the_event_loop(self, adapter):
        """close() can take ~30 s (core's stop_io); the gateway loop must keep running."""
        import threading

        release = threading.Event()
        transport = MagicMock()
        transport.close.side_effect = lambda: release.wait(5)
        adapter._transport = transport
        closing = adapter._cleanup()  # returns at once
        await asyncio.sleep(0.05)  # the loop is free while close() blocks
        assert not closing.done()
        release.set()
        closing.result(timeout=5)

    @pytest.mark.asyncio
    async def test_connect_waits_for_the_previous_close(self, adapter, monkeypatch):
        """The old server holds accounts.lock until it exits."""
        import concurrent.futures

        import adapter as adapter_mod

        pending = concurrent.futures.Future()
        monkeypatch.setattr(adapter_mod, "_last_transport_close", pending)
        monkeypatch.setattr(adapter_mod, "_check_dc2_available", lambda: True)
        started = []

        class FakeTransport:
            def __init__(self, **kw):
                started.append(pending.done())

            def start(self):
                raise RuntimeError("stop here")

            def close(self):
                pass

        monkeypatch.setitem(
            __import__("sys").modules,
            "deltachat2.transport",
            type("m", (), {"IOTransport": FakeTransport}),
        )
        task = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.05)
        assert started == []  # still waiting for the old server
        pending.set_result(None)
        await task
        assert started == [True]


class TestDisconnectIsResilient:
    @pytest.mark.asyncio
    async def test_teardown_failure_still_cleans_up(self, adapter):
        transport = MagicMock()
        adapter._transport = transport
        adapter._call_manager = MagicMock()
        adapter._call_manager.teardown = AsyncMock(side_effect=Exception("nope"))

        await adapter.disconnect()

        transport.close.assert_called_once()
        assert adapter.rpc is None
        assert adapter.is_connected is False
