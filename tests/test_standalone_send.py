"""Standalone delivery (hermes send / headless cron) without a running gateway."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import adapter as adapter_mod


def _rpc(state=26, accounts=({"id": 1},)):
    rpc = AsyncMock()
    rpc.get_all_accounts.return_value = list(accounts)
    rpc.send_msg.return_value = 7
    rpc.get_message.return_value = {"state": state}
    return rpc


@pytest.fixture
def env(platform_config):
    """Patch transport + rpc; yields (rpc, transport_cls, run)."""
    rpc = _rpc()
    transport_cls = MagicMock()
    with patch("adapter._check_dc2_available", return_value=True), patch(
        "deltachat2.transport.IOTransport", transport_cls
    ), patch("adapter._AsyncRpc", return_value=rpc), patch(
        "adapter.asyncio.sleep", AsyncMock()
    ):

        async def run(*args, **kwargs):
            return await adapter_mod._standalone_send(platform_config, *args, **kwargs)

        yield rpc, transport_cls, run


def test_registered_as_standalone_sender():
    ctx = MagicMock()
    adapter_mod.register_platform(ctx)
    _, kwargs = ctx.register_platform.call_args
    assert kwargs["standalone_sender_fn"] is adapter_mod._standalone_send


@pytest.mark.asyncio
async def test_text_success_waits_for_delivery_and_closes(env):
    rpc, transport_cls, run = env
    res = await run("5", "hello")
    assert res == {
        "success": True,
        "platform": "deltachat-platform",
        "chat_id": "5",
        "message_id": "7",
    }
    rpc.start_io.assert_awaited_once()
    rpc.get_message.assert_awaited()
    transport_cls.return_value.close.assert_called_once()


@pytest.mark.asyncio
async def test_rpc_failure_is_sanitised_and_cleans_up(env):
    rpc, transport_cls, run = env
    rpc.send_msg.side_effect = RuntimeError("secret-password-123")
    with patch(
        "adapter._async_retry",
        AsyncMock(side_effect=RuntimeError("secret-password-123")),
    ):
        res = await run("5", "hello")
    assert res["success"] is False
    assert "secret" not in res["error"]
    transport_cls.return_value.close.assert_called_once()


@pytest.mark.asyncio
async def test_no_account(env):
    rpc, transport_cls, run = env
    rpc.get_all_accounts.return_value = []
    res = await run("5", "hi")
    assert res["success"] is False and "setup.py" in res["error"]
    transport_cls.return_value.close.assert_called_once()


@pytest.mark.asyncio
async def test_locked_db_is_retryable(env):
    rpc, transport_cls, run = env
    rpc.get_all_accounts.side_effect = RuntimeError("RPC server disconnected")
    transport_cls.return_value.process.poll.return_value = 1  # server exited on lock
    res = await run("5", "hi")
    assert res["success"] is False and res["retryable"] is True
    rpc.send_msg.assert_not_awaited()
    transport_cls.return_value.close.assert_called_once()


@pytest.mark.asyncio
async def test_bad_chat_id_and_nothing_to_send(env):
    _, transport_cls, run = env
    assert (await run("abc", "hi"))["success"] is False
    transport_cls.assert_not_called()
    res = await run("5", "  ")
    assert res["success"] is False and res["error"] == "nothing to send"


@pytest.mark.asyncio
async def test_missing_media_file_fails_before_send(env):
    rpc, transport_cls, run = env
    res = await run("5", "hi", media_files=["/nonexistent/x.pdf"])
    assert res["success"] is False and "x.pdf" in res["error"]
    transport_cls.assert_not_called()


@pytest.mark.asyncio
async def test_media_and_force_document(env, tmp_path):
    rpc, _, run = env
    f = tmp_path / "pic.png"
    f.write_bytes(b"x")
    res = await run("5", "", media_files=[(str(f), False)], force_document=True)
    assert res["success"] is True
    msg_data = rpc.send_msg.await_args.args[2]
    assert msg_data.file == str(f)
    assert msg_data.viewtype.value == "File"


@pytest.mark.asyncio
async def test_failed_delivery_state_reported(env):
    rpc, _, run = env
    rpc.get_message.return_value = {"state": 24}
    res = await run("5", "hi")
    assert res["success"] is False and "deliver" in res["error"]
