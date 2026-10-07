"""A chat token only works from the chat it belongs to.

The token is shown in its own chat, but the agent can carry one elsewhere
(memory, a cron listing). Hermes binds the calling session's platform and chat
id per task; the RPC tools compare against that.
"""

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import adapter

SPEC = {
    "methods": [
        {
            "name": "get_chat_contacts",
            "params": [{"name": "accountId"}, {"name": "chatId"}],
        }
    ]
}
TOKEN_CHAT = 42


@pytest.fixture
def handlers():
    found = {}
    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: found.__setitem__(
        kw["name"], kw["handler"]
    )
    adapter.register_rpc_tools(ctx)
    return found


@pytest.fixture
def connected(monkeypatch):
    fake = MagicMock()
    fake.account_id = 1
    fake.platform.value = "deltachat-platform"
    fake.rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
    fake._call_manager.start_call = AsyncMock(return_value=77)
    fake._call_manager.request_hangup = AsyncMock(return_value=True)
    fake._call_manager.active_chat_ids.return_value = ["9", "42"]
    monkeypatch.setattr(adapter, "_active_adapter", fake)
    monkeypatch.setattr(adapter, "_spec_cache", SPEC)
    monkeypatch.setattr(
        adapter, "_resolve_chat_token", AsyncMock(return_value=TOKEN_CHAT)
    )
    return fake


@pytest.fixture
def session(monkeypatch):
    """Stand-in for gateway.session_context: bind(platform, chat_id)."""
    bound = {}
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": bound.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)

    def bind(platform, chat_id):
        bound.update(HERMES_SESSION_PLATFORM=platform, HERMES_SESSION_CHAT_ID=chat_id)

    return bind


async def _safe(handlers):
    return json.loads(
        await handlers["dc_safe_rpc_call"](
            {"method": "get_chat_contacts", "chat_token": "tok", "params": []}
        )
    )


class TestSafeRpcCall:
    @pytest.mark.asyncio
    async def test_own_chat_works(self, handlers, connected, session):
        session("deltachat-platform", "42")
        assert await _safe(handlers) == [1, 2]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "platform,chat",
        [
            ("deltachat-platform", "9"),  # another Delta Chat chat
            ("telegram", "42"),  # same number, different platform
        ],
    )
    async def test_token_carried_into_another_chat_is_refused(
        self, handlers, connected, session, platform, chat
    ):
        session(platform, chat)
        result = await _safe(handlers)
        assert "different conversation" in result["error"]
        connected.rpc.get_chat_contacts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_calling_chat_leaves_the_token_as_the_only_check(
        self, handlers, connected, session
    ):
        """Cron binds platform="" and chat_id=""; a job may use a stored token."""
        session("", "")
        assert await _safe(handlers) == [1, 2]

    @pytest.mark.asyncio
    async def test_core_without_session_context_is_unaffected(
        self, handlers, connected, monkeypatch
    ):
        monkeypatch.setitem(sys.modules, "gateway.session_context", None)
        assert await _safe(handlers) == [1, 2]


class TestCalls:
    @pytest.mark.asyncio
    async def test_start_call_into_another_chat_is_refused(
        self, handlers, connected, session
    ):
        session("deltachat-platform", "9")
        result = json.loads(
            await handlers["dc_start_call"]({"chat_token": "tok", "opening": "Hi"})
        )
        assert "different conversation" in result["error"]
        connected._call_manager.start_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_end_call_hangs_up_the_callers_own_call(
        self, handlers, connected, session
    ):
        """It used to end "the first active call", whichever chat asked."""
        session("deltachat-platform", "42")
        await handlers["dc_end_call"]({})
        connected._call_manager.request_hangup.assert_awaited_once_with("42")

    @pytest.mark.asyncio
    async def test_end_call_from_a_chat_without_a_call_ends_nothing(
        self, handlers, connected, session
    ):
        session("deltachat-platform", "5")
        result = json.loads(await handlers["dc_end_call"]({}))
        assert result["error"] == "No active call"
        connected._call_manager.request_hangup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_end_call_with_no_calling_chat_keeps_the_old_behaviour(
        self, handlers, connected, session
    ):
        session("", "")
        await handlers["dc_end_call"]({})
        connected._call_manager.request_hangup.assert_awaited_once_with("9")


class TestSharedAcrossEventLoops:
    """Hermes runs tool handlers on their own event loop in a worker thread,
    so module state they share with the gateway loop must not be loop-bound."""

    def test_token_helpers_work_from_two_event_loops(self):
        import asyncio

        rpc = MagicMock()
        rpc.get_config = AsyncMock(return_value=None)
        rpc.set_config = AsyncMock()

        async def issue():
            return await adapter._get_or_create_chat_token(rpc, 1, 4711)

        try:
            first = asyncio.new_event_loop().run_until_complete(issue())
            other = asyncio.new_event_loop()
            assert other.run_until_complete(issue()) == first
            assert (
                other.run_until_complete(adapter._resolve_chat_token(rpc, 1, first))
                == 4711
            )
        finally:
            adapter._chat_id_to_token.pop(4711, None)
            adapter._chat_token_to_id.pop(first, None)

    def test_no_asyncio_lock_is_shared_at_module_level(self):
        import asyncio

        shared = [n for n, v in vars(adapter).items() if isinstance(v, asyncio.Lock)]
        assert shared == []
