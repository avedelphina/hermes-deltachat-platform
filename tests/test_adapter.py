"""Unit tests for pure helper functions in adapter.py.

These tests do not require a running Delta Chat RPC server or Hermes gateway.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from adapter import (
    DC_MESSAGE_MAX_LEN,
    DeltaChatAdapter,
    _async_retry,
    _cfg,
    _is_valid_email,
    _parse_chatmail_servers,
    _parse_email_list,
    _safe_data_dir,
    _split_message,
    _strip_markdown,
    _validate_avatar_path,
    _validate_rpc_server_path,
)


class TestStripMarkdown:
    def test_empty(self):
        assert _strip_markdown("") == ""

    def test_code_block(self):
        assert _strip_markdown("```python\nprint('hi')\n```") == "print('hi')\n"

    def test_inline_code(self):
        assert _strip_markdown("use `cmd` here") == "use cmd here"

    def test_heading(self):
        assert _strip_markdown("# Hello") == "Hello"
        assert _strip_markdown("## World") == "World"

    def test_link(self):
        assert (
            _strip_markdown("[text](https://example.com)")
            == "text (https://example.com)"
        )

    def test_bold_italic_strikethrough(self):
        assert _strip_markdown("**bold**") == "bold"
        assert _strip_markdown("*italic*") == "italic"
        assert _strip_markdown("__bold__") == "bold"
        assert _strip_markdown("_italic_") == "italic"
        assert _strip_markdown("~~strike~~") == "strike"


class TestSplitMessage:
    def test_short_unchanged(self):
        assert _split_message("hello") == ["hello"]

    def test_empty(self):
        assert _split_message("") == []

    def test_exact_boundary_no_split(self):
        text = "a" * DC_MESSAGE_MAX_LEN
        assert _split_message(text) == [text]

    def test_split_on_paragraph(self):
        text = ("a" * 1800) + "\n\n" + ("b" * 1800)
        parts = _split_message(text)
        assert len(parts) == 2
        assert all(len(p) <= DC_MESSAGE_MAX_LEN for p in parts)

    def test_split_on_line(self):
        text = ("a" * 1800) + "\n" + ("b" * 1800)
        parts = _split_message(text)
        assert len(parts) == 2

    def test_hard_split_fallback(self):
        text = "x" * 8000
        parts = _split_message(text)
        assert len(parts) >= 2
        assert all(len(p) <= DC_MESSAGE_MAX_LEN for p in parts)
        assert "".join(parts) == text

    def test_respects_custom_max_len(self):
        text = "a" * 100
        parts = _split_message(text, max_len=40)
        assert all(len(p) <= 40 for p in parts)
        assert "".join(parts) == text

    def test_zero_or_negative_max_len_uses_default(self):
        text = "a" * (DC_MESSAGE_MAX_LEN + 100)
        for bad_max in (0, -1, -1000):
            parts = _split_message(text, max_len=bad_max)
            assert all(len(p) <= DC_MESSAGE_MAX_LEN for p in parts)
            assert "".join(parts) == text


class TestWorkspacePathMapping:
    def test_rejects_dotdot(self):
        assert (
            DeltaChatAdapter._container_workspace_to_host("/workspace/../etc/passwd")
            is None
        )
        assert (
            DeltaChatAdapter._container_workspace_to_host(
                "/workspace/subdir/../../etc/passwd"
            )
            is None
        )

    def test_rejects_symlink_escape(self, tmp_path, monkeypatch):
        from gateway.config import get_hermes_home

        hermes_home = tmp_path / "home"
        hermes_home.mkdir()
        workspace = hermes_home / "sandboxes" / "docker" / "default" / "workspace"
        workspace.mkdir(parents=True)
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        (workspace / "link").symlink_to(secret)

        monkeypatch.setattr("gateway.config.get_hermes_home", lambda: str(hermes_home))
        assert DeltaChatAdapter._container_workspace_to_host("/workspace/link") is None

    def test_maps_normal_workspace_path(self, tmp_path, monkeypatch):
        from gateway.config import get_hermes_home

        hermes_home = tmp_path / "home"
        workspace = hermes_home / "sandboxes" / "docker" / "default" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "file.txt").write_text("ok")

        monkeypatch.setattr("gateway.config.get_hermes_home", lambda: str(hermes_home))
        result = DeltaChatAdapter._container_workspace_to_host("/workspace/file.txt")
        assert result == str(workspace / "file.txt")


class TestEmailValidation:
    def test_valid(self):
        for e in ["user@example.com", "a+b@x.org", "foo@bar.co.uk"]:
            assert _is_valid_email(e), e

    def test_invalid(self):
        for e in ["notanemail", "@domain.com", "user@", "user @domain.com"]:
            assert not _is_valid_email(e), e

    def test_rejects_display_name_form(self):
        assert not _is_valid_email("User <user@example.com>")

    def test_rejects_too_long(self):
        assert not _is_valid_email("a" * 250 + "@x.com")


class TestParseEmailList:
    def test_basic(self):
        assert _parse_email_list("a@x.com, B@X.COM") == {"a@x.com", "b@x.com"}

    def test_empty(self):
        assert _parse_email_list("") == set()

    def test_single(self):
        assert _parse_email_list("alice@example.com") == {"alice@example.com"}


class TestParseChatmailServers:
    def test_basic(self):
        assert _parse_chatmail_servers("a.com, b.com, a.com") == ["a.com", "b.com"]

    def test_empty(self):
        assert _parse_chatmail_servers("") == []

    def test_whitespace_trimmed(self):
        assert _parse_chatmail_servers(" a.com , b.com ") == ["a.com", "b.com"]

    def test_case_preserved(self):
        assert _parse_chatmail_servers("A.com, a.com") == ["A.com"]


class TestSafeDataDir:
    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            _safe_data_dir("/tmp/foo/../bar")

    def test_creates_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "dc-data")
            p = _safe_data_dir(path, create=True)
            assert p.exists()
            assert p.stat().st_mode & 0o777 == 0o700


class TestValidateRpcServerPath:
    def test_non_strict_returns_path_when_missing(self):
        assert (
            _validate_rpc_server_path("probably-not-on-path", strict=False)
            == "probably-not-on-path"
        )

    def test_strict_missing_raises(self):
        with pytest.raises(ValueError):
            _validate_rpc_server_path("/nonexistent/binary-12345", strict=True)

    def test_strict_resolves_absolute_executable(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/bin/sh\n")
            path = f.name
        os.chmod(path, 0o755)
        try:
            assert _validate_rpc_server_path(path, strict=True) == path
        finally:
            os.unlink(path)


class TestValidateAvatarPath:
    def test_non_strict_accepts_image_suffix(self):
        assert _validate_avatar_path("/tmp/bot.png", strict=False) == "/tmp/bot.png"

    def test_invalid_suffix_raises(self):
        with pytest.raises(ValueError):
            _validate_avatar_path("/tmp/bot.txt", strict=False)

    def test_strict_missing_file_raises(self):
        with pytest.raises(ValueError):
            _validate_avatar_path("/tmp/nonexistent.png", strict=True)


class TestAsyncRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        coro = AsyncMock(return_value="ok")
        result = await _async_retry(coro, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert coro.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        coro = AsyncMock(side_effect=[RuntimeError("boom"), "ok"])
        result = await _async_retry(coro, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert coro.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_exhaustion(self):
        coro = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await _async_retry(coro, max_attempts=2, base_delay=0.01)
        assert coro.call_count == 2


class TestCfg:
    class _FakeConfig:
        def __init__(self, extra):
            self.extra = extra

    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_TEST_KEY", "env_value")
        config = self._FakeConfig({"test_key": "extra_value"})
        assert _cfg(config, "DELTACHAT_TEST_KEY", "test_key", "default") == "env_value"

    def test_extra_fallback(self, monkeypatch):
        monkeypatch.delenv("DELTACHAT_TEST_KEY", raising=False)
        config = self._FakeConfig({"test_key": "extra_value"})
        assert (
            _cfg(config, "DELTACHAT_TEST_KEY", "test_key", "default") == "extra_value"
        )

    def test_default_fallback(self, monkeypatch):
        monkeypatch.delenv("DELTACHAT_TEST_KEY", raising=False)
        config = self._FakeConfig({})
        assert _cfg(config, "DELTACHAT_TEST_KEY", "test_key", "default") == "default"


class TestMentionDetection:
    """Unit tests for the group mention detector."""

    def test_at_mention_matches(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("Hello @Hermes, how are you?") is True

    def test_bare_name_mention_matches(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("Hermes please help") is True

    def test_case_insensitive(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("hey @hermes") is True
        assert adapter._is_mentioned("HERMES do this") is True

    def test_substring_does_not_match(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("Hermesssss") is False
        assert adapter._is_mentioned("@Hermesss") is False

    def test_punctuation_boundary_still_matches(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("@Hermes!") is True
        assert adapter._is_mentioned("(@Hermes)") is True

    def test_empty_text_is_not_mentioned(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("") is False
