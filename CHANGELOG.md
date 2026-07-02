# Changelog

All notable changes to this project will be documented in this file.

## [1.4.1] - 2026-07-02

### Security / Hardening
- Workspace file delivery is now sandbox-escape-proof: `/workspace/` paths are resolved and verified to stay inside the sandbox; `..` and symlink escapes are rejected.
- Raw RPC is filtered: `dc_rpc_call` logs every invocation at `WARNING`, blocks destructive methods (`delete_*`, `remove_*`), and supports `DELTACHAT_RAW_RPC_ALLOWLIST` / `DELTACHAT_RAW_RPC_BLOCKLIST`.
- Account passwords are cleared from memory immediately after configuration succeeds or fails.
- Inbound access control is fail-closed: chat-info RPC failures now reject the message instead of bypassing policy checks.
- Delta Chat version check failures now reject the connection instead of falling through.
- Voice-call incoming audio buffer is capped at a 60-second utterance ceiling to prevent unbounded growth.
- Cross-loop call-manager state (`_sessions`, `_chat_to_msg`, drop counters) is now protected by a `threading.Lock`.
- Removed ~240 lines of dead code (`_handle_audio_message_UNUSED`).

### Fixed
- `DELTACHAT_MAX_MESSAGE_LENGTH <= 0` no longer causes an infinite split loop; values outside 100–10000 are clamped to the default.

### Tests
- Added `TestWorkspacePathMapping`, `TestSplitMessage::test_zero_or_negative_max_len_uses_default`, and `TestOnboarding::test_configure_account_clears_password_on_configure_failure`.

## [1.4.0] - 2026-07-02

### Added
- Group mention detection: `DELTACHAT_REQUIRE_MENTION=true` makes the bot ignore group messages (and image/voice/file captions) that do not mention its display name (`@Name` or whole-word name).
- URL image sending: `send_image_file()` now accepts `http(s)://` image URLs, downloads them via `httpx`, and sends them as Delta Chat images (25 MiB limit, `image/*` check, no redirects).
- Metadata enrichment: incoming `MessageEvent`s and outgoing `SendResult`s now carry `chat_id`, `message_id`, `from_id`, `is_group`, and `dc_token`.
- New docs: `docs/CONFIGURATION.md` (full env reference), `docs/SECURITY.md` (URL image and permissions notes).
- `httpx` added to `flake.nix` dev shell.

### Tests
- Added `TestMentionDetection`, `TestMentions`, `TestMetadata`, and `TestUrlImageSending`.

## [1.3.0] - 2026-07-02

### Added
- Account onboarding parity with the upstream project:
  - `DELTACHAT_DATA_DIR` is now honoured at runtime (created with `0o700`); falls back to `~/.hermes/deltachat-platform` when unset.
  - `DELTACHAT_DISPLAY_NAME` and `DELTACHAT_AVATAR_PATH` are applied to the Delta Chat account on connect.
  - Automatic account creation: when no account exists, the adapter creates one via chatmail (`DELTACHAT_CHATMAIL_SERVERS`) or manual credentials (`DELTACHAT_EMAIL` + `DELTACHAT_PASSWORD`).
  - Existing accounts are reused and their profile refreshed.
  - SecureJoin invite link generated after IO starts and exposed in `get_status()`.
- New `TestOnboarding` integration tests covering data-dir selection, profile application, manual/chatmail account setup, and invite-link generation.

### Fixed
- Restored `_DC2_AVAILABLE` cache state after the "deltachat2 not installed" test to avoid false negatives in later tests.

## [1.2.0] - 2026-07-02

### Added
- Graceful shutdown on `SIGTERM`/`SIGINT` with signal-handler registration in `connect()` and removal in `disconnect()`.
- `get_status()` health/metrics snapshot (connection state, account address, crash count, internal stats).
- Internal stats counters (`_bump_stat`) wired into inbound gating and outbound sending.
- Event-listener crash recovery: `_event_supervisor()` restarts the listener after a crash and disables the adapter after 3 crashes in 60 seconds.
- Cached bot address (`_self_addr`) for synchronous status reporting.

### Tests
- Added `TestStatusAndStats`, `TestSignalHandling`, and `TestEventSupervisor` integration tests.

## [1.1.0] - 2026-07-02

### Added
- Markdown stripping before sending plain-text messages.
- Smart message splitting at paragraph / line / sentence / word boundaries.
- Configurable `DELTACHAT_MAX_MESSAGE_LENGTH` (default 3600).
- Exponential-backoff retry (`_async_retry`) on outbound send operations.
- Strict config validation for email, data directory, RPC server path, avatar path, and chatmail servers.
- New `DELTACHAT_EMAIL`, `DELTACHAT_PASSWORD`, `DELTACHAT_DATA_DIR`, `DELTACHAT_CHATMAIL_SERVER`, `DELTACHAT_CHATMAIL_SERVERS`, `DELTACHAT_DISPLAY_NAME`, `DELTACHAT_AVATAR_PATH`, and `DELTACHAT_REQUIRE_MENTION` env vars declared in `plugin.yaml`.
- Unit tests for all new helper functions in `tests/test_adapter.py`.

### Fixed
- Updated stale integration tests to use current RPC method mocks (`send_msg`, `get_system_info`, `get_basic_chat_info`) and correct event types.
- Fixed call-handler tests that required a running event loop and a stale `_drop_next_response` assertion.

## [1.0.0] - 2026-06-30

### Added
- Initial release: Delta Chat platform adapter for Hermes Agent with support for text, voice messages, images, files, locations, voice calls, and webxdc mini-apps.
