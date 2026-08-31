# Changelog

All notable changes to this project will be documented in this file.

## [1.7.3] - 2026-08-31

### Fixed
- A dead `deltachat-rpc-server` no longer hangs its callers forever. `vendor/deltachat2/transport.py`'s `_Result.wait()` was a bare untimed `threading.Event.wait()`: when the RPC subprocess exited, the reader loop resolved every *in-flight* call with a disconnect error, but the writer loop then died silently on `BrokenPipeError`, so every *subsequent* `call()` queued a request that was never written and blocked its thread permanently — with `is_connected` still reporting `True`. `call()` now fast-fails when `process.poll()` shows the subprocess is gone, polls in 1s slices while it waits (a legitimate slow call keeps waiting as long as the server is alive), and raises `JsonRpcError` the moment the subprocess exits mid-call. The pending-caller wake-up (`_fail_all_pending()`) now fires from the writer loop's `finally` as well as the reader loop's. Reported by upstream as [issue #16](https://github.com/Simon-Laux/hermes-deltachat-platform/issues/16).
- `_cleanup()` now marks the adapter disconnected. It is the failure path out of `connect()` as well as part of `disconnect()`, so a failed connect previously left the prior runtime status in place.
- `disconnect()` wraps `CallManager.teardown()` in a guard. A raising teardown used to skip `_cleanup()` entirely, leaking the RPC subprocess and the accounts-dir lock — which then blocks any replacement adapter from connecting.
- The event supervisor task gets a done-callback that retrieves its exception, so an escaped crash is logged when it happens rather than surfacing as "Task exception was never retrieved" whenever the GC gets to it.

### Docs
- `README.md`'s "Development" section now documents that `vendor/deltachat2/` has diverged from upstream `adbenitez/deltachat2` (the dead-server handling above, `to_attrdict()` on results, `close()` guards, and the `rpc_server=` vs upstream `rpc_executable=` constructor kwarg), and tells re-vendorers to diff rather than copy.

### Tests
- Added `tests/test_transport_death.py`: already-dead fast-fail, server dies mid-`wait()` (raises within 5s instead of hanging, abandoned request cleared), and writer-loop failure waking pending callers.

## [1.7.2] - 2026-08-31

### Fixed
- A quote-reply to one of the bot's own messages is again treated as an implicit mention in a mention-gated group (`DELTACHAT_REQUIRE_MENTION`, or a chat listed in `DELTACHAT_REQUIRE_MENTION_CHANNELS`). The reply-to-self check compared `quote.author_display_name` against the configured `display_name`, but Delta Chat core reports that field as the localized "Me" stock string (`"Me"`/`"Ich"`/`"Já"`…) for a self-authored quote, never the displayname, so the match always failed and the reply was silently dropped. Now `_quote_is_self_authored()` fetches the quoted message and checks `from_id == DC_CONTACT_ID_SELF` (locale-independent); the old name match is kept only as a fallback for when the quoted message is not available locally. The `[replying to …]` context spliced into the text now uses the bot's real display name instead of "Me". Explicit `@mention`, authorization, allowlist/open policy, and the bot-loop / bot-exchange guards are unaffected.

### Tests
- Added `test_reply_to_own_message_is_implicit_mention`, `test_reply_to_other_member_still_gated`, and `test_reply_to_own_message_explicit_mention_still_works` to `TestMentions` (integration), covering the real `MessageQuote.WithMessage` shape.

## [1.5.11] - 2026-07-05

### Fixed
- Mention matching now requires an explicit `@` prefix. A bare display name in prose (e.g. "napiš Alici") is about the bot, not addressed to it.
- The mention gate now runs on the reply body only, before quoted text is spliced in, so quoting an old message that mentioned the bot does not count as a fresh mention.
- Updated mention tests to expect `@`-prefixed mentions.

## [1.5.10] - 2026-07-05

### Fixed
- The consecutive-reply and bot-exchange loop guards are now scoped to group chats only. In a DM the counterparty never changes, so the guards would trip permanently and lock out the only other participant.

### Tests
- Added `TestLoopGuardChatScope` with DM-ignored and group-still-tripped cases.

## [1.5.9] - 2026-07-05

### Added
- `_is_mentioned` now tolerates short case-ending variation so declined forms of a bot's name still count as a mention (e.g. Czech: "napiš Alici" mentions `display_name: Alice`; "Anikko" still mentions "Anikke"). Implemented generically via `_build_mention_pattern` (stem + up to 2 trailing word characters), not a Czech-specific grammar table. Names with a stem shorter than 3 characters fall back to an exact match to avoid matching unrelated words on a short shared prefix.
- `DELTACHAT_MENTION_ALIASES` (comma-separated): extra names/forms that also count as a mention, for cases the automatic stemming doesn't cover.

### Tests
- Added `TestCzechDeclensionMentions`: Alice's dative/instrumental forms, Anikke's (incorrect) vocative, Holly's unchanged dative, a short-name exact-match fallback, `DELTACHAT_MENTION_ALIASES`, and a same-prefix false-positive guard ("Hollywood" vs "Holly").

## [1.5.8] - 2026-07-05

### Fixed
- `MSG_FAILED` events now log `chat_id` and fetch `get_message().error` for the real failure reason. Previously only the bare `msg_id` was logged, giving no way to tell which chat a failed send belonged to or why Delta Chat core marked it `DC_STATE_OUT_FAILED`.

### Tests
- Added coverage for `MSG_FAILED` handling: chat_id/error surfaced correctly, and the handler still logs what it knows if `get_message` itself fails.

## [1.5.7] - 2026-07-05

### Added
- `dc_send_message` gains an `address` parameter: cold-opens a 1:1 chat with a Delta Chat email instead of requiring an existing `chat_token`. Resolution is `lookup_contact_id_by_addr` → `create_contact` (only if unknown) → `create_chat_by_contact_id`. Guarded by `_is_address_in_known_rosters()` — only addresses already seen via `get_chat_contacts` in a group this bot participates in are reachable; an unknown/arbitrary address is rejected. `chat_token` takes precedence when both are given.

### Tests
- Added `TestDcSendMessageAddress` (invalid email, address outside every roster, known-address send, create_contact fallback when unknown, chat_token precedence) and `TestIsAddressInKnownRosters`.

## [1.5.6] - 2026-07-05

### Fixed
- `register_platform()` now declares `allowed_users_env="DELTACHAT_ALLOWED_USERS"` and `allow_all_env="DELTACHAT_ALLOW_ALL_USERS"`. Without this, Hermes-core's own authorization gate (`gateway.authz_mixin._is_user_authorized`) had no way to know these env vars exist, and never trusted `dm_policy`/`group_policy: open` as authorization (by design), so it silently dropped every sender — including the account owner — regardless of this adapter's own access-control config.
- The group mention gate (`_check_mention`) no longer sends a "please mention me" reply on unmentioned messages — it now ignores them silently. In a multi-bot group every bot enforces this independently, so the old notice fired once per bot per unmentioned message.

### Tests
- Added `TestRegisterPlatformAuthEnv` asserting the auth env var names are declared.
- Added `TestUnmentionedGroupMessageIsSilent`; updated the existing `TestMentions::test_require_mention_blocks_unmentioned_group_message` to assert silence instead of a reply.

## [1.5.5] - 2026-07-05

### Added
- Group roster awareness: `_get_group_roster` fetches a group's member list via `get_chat_contacts` (excluding self), cached per `chat_id` for 5 minutes to avoid an RPC round-trip per message. `_message_metadata` now includes `participants` (`[{"name", "address"}, ...]`) for group messages, so the agent knows all group members, not just whoever has spoken.

### Tests
- Added `TestGroupRoster` covering fetch-and-exclude-self, caching, TTL expiry, and RPC-failure fallback to stale cache / empty list.
- Added `TestMessageMetadataRoster` covering participants inclusion for groups and omission for DMs / no-roster calls.

## [1.5.4] - 2026-07-05

### Fixed
- `free_response_channels` / `DELTACHAT_FREE_RESPONSE_CHANNELS` was bridged from YAML into `extra` but never actually read anywhere — `_check_mention` now exempts listed group chat IDs from `DELTACHAT_REQUIRE_MENTION`, so a shared multi-bot group can be configured to always respond without needing an `@mention`.

### Tests
- Added `TestFreeResponseChannels` covering the mention gate blocking an unlisted group, a listed group skipping the gate, other chats staying unaffected, and multiple configured channel IDs.

## [1.5.3] - 2026-07-04

### Added
- Bot-exchange guard: `DELTACHAT_MAX_BOT_EXCHANGES` (default 12) caps total messages in a chat from senders not in `DELTACHAT_HUMAN_USERS`, requiring a human check-in to resume. Catches 3+ bots round-robining a shared group — a case the existing `DELTACHAT_MAX_CONSECUTIVE_REPLIES` guard misses, since with more than 2 bots the sender keeps changing so no same-sender streak ever trips. Inactive unless `DELTACHAT_HUMAN_USERS` is set.
- `_apply_yaml_config` now also bridges `human_users` and `max_bot_exchanges`.

### Tests
- Added `TestBotExchangeGuard` covering the disabled-by-default state, tripping across alternating senders, reset on a human message, and single-warning-per-trip behavior.

## [1.5.2] - 2026-07-02

### Fixed
- `_apply_yaml_config` now preserves values already under `platform_cfg["extra"]` instead of silently dropping them.
- `_apply_yaml_config` now bridges access-control keys from YAML (`allowed_users`, `allow_all_users`, `dm_allowed_users`, `group_allowed_users`, `dm_policy`, `group_policy`).
- Code formatting (`black`) applied to `setup.py` and `tests/test_call_webrtc_loopback.py`.

### Tests
- Added `TestApplyYamlConfig` covering extra preservation, access-control bridging, and YAML-key precedence.

## [1.5.1] - 2026-07-02

### Added
- YAML config bridge: `_apply_yaml_config` maps platform YAML keys (`display_name`, `avatar_path`, `email`, `chatmail_server`, `chatmail_servers`, `data_dir`, `home_channel`, `require_mention`, `free_response_channels`, `auto_delete_interval`, `max_message_length`) into the adapter's `extra` config.

### Fixed
- `_env_enablement` no longer hardcodes a default `display_name`, letting the adapter constructor apply its own default when the env var is absent.

## [1.5.0] - 2026-07-02

### Added
- Proactive messaging tool: `dc_send_message` lets the agent push text to a chat without an inbound message (uses `[dc:chat=<token>]` or falls back to `DELTACHAT_HOME_CHANNEL`).
- Bot-loop guard: `DELTACHAT_MAX_CONSECUTIVE_REPLIES` (default 20, `<=0` disables) stops the adapter from processing further messages from the same sender after that many consecutive messages with no one else joining in.
- Quote-reply handling: replying to one of the bot's own messages is treated as an implicit mention, and the quoted text is surfaced in the incoming message context.

### Fixed
- `DeltaChatAdapter.connect()` now accepts the `is_reconnect` keyword for Hermes 0.18 compatibility.

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
- Group mention detection: `DELTACHAT_REQUIRE_MENTION=true` makes the bot ignore group messages (and image/voice/file captions) that do not mention its display name (`@Name` or whole-word name). Reads `DELTACHAT_DISPLAY_NAME` (default `Hermes`) for the match.
- URL image sending: `send_image_file()` now accepts `http(s)://` image URLs, downloads them via `httpx`, and sends them as Delta Chat images (25 MiB limit, `image/*` check, no redirects).
- Metadata enrichment: incoming `MessageEvent`s and outgoing `SendResult`s now carry `chat_id`, `message_id`, `from_id`, `is_group`, and `dc_token`.
- New docs: `docs/CONFIGURATION.md` (full env reference), `docs/SECURITY.md` (URL image and permissions notes).
- `httpx` added to `flake.nix` dev shell.

### Tests
- Added `TestMentionDetection`, `TestMentions`, `TestMetadata`, and `TestUrlImageSending`.

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
