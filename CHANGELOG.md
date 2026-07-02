# Changelog

All notable changes to this project will be documented in this file.

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
