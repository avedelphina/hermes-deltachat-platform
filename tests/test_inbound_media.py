"""Inbound attachments: Hermes' size cap, videos as videos, and no message
content in INFO logs (ported from upstream 2.1.0)."""

import logging
from unittest.mock import AsyncMock

import pytest

import adapter as adapter_mod
from adapter import DeltaChatAdapter


@pytest.fixture
def blob(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"x" * 2048)
    return path


def _adapter(platform_config, mock_rpc, blob, view_type, mime, text="look at this"):
    a = DeltaChatAdapter(platform_config)
    a.rpc, a.account_id = mock_rpc, 1
    a.handle_message = AsyncMock()
    a._copy_to_hermes_cache = lambda src, kind: src
    msg = {
        "id": 10,
        "text": text,
        "view_type": view_type,
        "file": str(blob),
        "file_mime": mime,
        "file_name": "holiday.mp4",
        "from_id": 11,
    }
    mock_rpc.get_contact = AsyncMock(
        return_value={"address": "a@example.com", "name": "Ann"}
    )
    mock_rpc.get_basic_chat_info = AsyncMock(
        return_value={"chat_type": "Single", "name": "c"}
    )
    mock_rpc.get_config = AsyncMock(return_value="tok")
    return a, msg


def _event(a):
    return a.handle_message.await_args.args[0]


@pytest.mark.asyncio
async def test_oversized_attachment_reaches_hermes_as_a_note(
    platform_config, mock_rpc, blob, monkeypatch
):
    monkeypatch.setattr(adapter_mod, "_inbound_media_limit", lambda: 1024)
    a, msg = _adapter(platform_config, mock_rpc, blob, "Image", "image/png")
    await a._handle_non_text_message(msg, "5", "10")
    event = _event(a)
    assert event.media_urls == [] and event.media_types == []
    assert "over the inbound media size limit" in event.text


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 4096])
async def test_attachment_within_or_without_a_cap_is_passed_on(
    platform_config, mock_rpc, blob, monkeypatch, limit
):
    monkeypatch.setattr(adapter_mod, "_inbound_media_limit", lambda: limit)
    a, msg = _adapter(platform_config, mock_rpc, blob, "Image", "image/png")
    await a._handle_non_text_message(msg, "5", "10")
    event = _event(a)
    assert event.media_urls == [str(blob)] and event.media_types == ["image/png"]


@pytest.mark.asyncio
async def test_video_arrives_as_video(platform_config, mock_rpc, blob, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_inbound_media_limit", lambda: 0)
    monkeypatch.setattr(
        "gateway.platforms.base.cache_document_from_bytes",
        lambda data, name: "/cache/" + name,
        raising=False,
    )
    a, msg = _adapter(platform_config, mock_rpc, blob, "Video", "video/mp4")
    await a._handle_non_text_message(msg, "5", "10")
    event = _event(a)
    assert event.message_type == adapter_mod.MessageType.VIDEO
    assert event.text.startswith("[Video from Ann: holiday.mp4]")


@pytest.mark.asyncio
async def test_missing_file_keeps_media_types_in_step(
    platform_config, mock_rpc, blob, monkeypatch
):
    monkeypatch.setattr(adapter_mod, "_inbound_media_limit", lambda: 0)
    a, msg = _adapter(platform_config, mock_rpc, blob, "Image", "image/png")
    a._resolve_blob_path = lambda name: None
    await a._handle_non_text_message(msg, "5", "10")
    assert _event(a).media_types == []


@pytest.mark.asyncio
async def test_caption_stays_out_of_info_logs(platform_config, mock_rpc, blob, caplog):
    a, msg = _adapter(
        platform_config, mock_rpc, blob, "Image", "image/png", text="my secret plans"
    )
    mock_rpc.get_message = AsyncMock(return_value=msg)
    a._gate_inbound = AsyncMock(return_value=True)
    with caplog.at_level(logging.INFO, logger="hermes_plugins.deltachat"):
        await a._handle_incoming_message({"chat_id": 5, "msg_id": 10})
    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert info and not any("secret" in m for m in info)
