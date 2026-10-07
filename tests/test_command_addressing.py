"""Telegram-style "/cmd@<name>" command addressing (ported from upstream 2.0.0)."""

from unittest.mock import AsyncMock

import pytest

from adapter import DeltaChatAdapter, _COMMAND_ADDR_RE


def _for_us(adapter, text):
    return adapter._command_for_us(text, _COMMAND_ADDR_RE.match(text).end())


class TestCommandForUs:
    @pytest.fixture
    def adapter(self, platform_config):
        platform_config.extra = {
            "display_name": "Hermes",
            "mention_aliases": "Hermes Bot,herm",
        }
        return DeltaChatAdapter(platform_config)

    def test_own_name_is_stripped(self, adapter):
        assert _for_us(adapter, "/reset@Hermes") == "/reset"
        assert _for_us(adapter, "/model@hermes gpt-5") == "/model gpt-5"

    def test_longest_name_wins(self, adapter):
        assert _for_us(adapter, "/reset@Hermes Bot now") == "/reset now"

    def test_other_bot_is_not_us(self, adapter):
        assert _for_us(adapter, "/reset@Alice") is None
        assert _for_us(adapter, "/reset@Hermesina") is None  # no prefix match


class TestAddressedCommandRouting:
    def _adapter(self, platform_config, mock_rpc, text, chat_type):
        platform_config.extra = {"display_name": "Hermes", "group_policy": "open"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        adapter.handle_message = AsyncMock()
        adapter._gate_inbound = AsyncMock(return_value=True)
        adapter._get_group_roster = AsyncMock(return_value=[])
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": text,
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": chat_type, "name": "Chat"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        return adapter

    async def _run(self, adapter):
        await adapter._handle_incoming_message(
            {"kind": "IncomingMsg", "chat_id": 1, "msg_id": 10}
        )

    @pytest.mark.asyncio
    async def test_command_for_us_reaches_hermes_as_plain_command(
        self, platform_config, mock_rpc
    ):
        adapter = self._adapter(platform_config, mock_rpc, "/reset@Hermes", "Group")
        await self._run(adapter)
        assert adapter.handle_message.await_args.args[0].text == "/reset"

    @pytest.mark.asyncio
    async def test_command_for_another_bot_is_dropped_in_a_group(
        self, platform_config, mock_rpc
    ):
        adapter = self._adapter(platform_config, mock_rpc, "/reset@Alice", "Group")
        await self._run(adapter)
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bare_command_still_reaches_every_bot(
        self, platform_config, mock_rpc
    ):
        adapter = self._adapter(platform_config, mock_rpc, "/reset", "Group")
        await self._run(adapter)
        assert adapter.handle_message.await_args.args[0].text == "/reset"

    @pytest.mark.asyncio
    async def test_dm_passes_a_foreign_address_through(self, platform_config, mock_rpc):
        """In a DM there is nobody else it could be for."""
        adapter = self._adapter(platform_config, mock_rpc, "/reset@Alice", "Single")
        await self._run(adapter)
        assert adapter.handle_message.await_args.args[0].text == "/reset@Alice"
