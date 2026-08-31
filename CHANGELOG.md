# Changelog

All notable changes to this project will be documented in this file.

## [1.7.5] - 2026-08-31

### Changed
- A dead event listener is now reported to the Hermes gateway instead of being restarted in-process. Previously `_event_supervisor` restarted the listener up to 3 times in 60s and then called `disconnect()` — but the gateway was never told, so the platform was never queued for reconnect, and the inner `except Exception` in `_event_listener` swallowed every error so the give-up path was effectively unreachable anyway. A dead `deltachat-rpc-server` therefore produced a permanent 1-per-second error log with `is_connected` still `True` and no recovery. `_event_supervisor` is removed. `_event_listener` now runs the poll loop in a `try/finally`; if the loop ever exits while `self.is_connected` is still true — the RPC subprocess exited, or the task was cancelled by something other than `disconnect()` — it calls `_set_fatal_error(..., retryable=True)` + `_notify_fatal_error()`, the same `BasePlatformAdapter` contract IRC/Telegram and the other bundled adapters use. Hermes's `_handle_adapter_fatal_error` then drops this adapter and `_platform_reconnect_watcher` rebuilds a fresh one (respawning the RPC server) with 30s→300s backoff. The adapter deliberately does not restart the listener itself — an adapter-side supervisor races that watcher and keeps the RPC subprocess and accounts-dir lock alive, blocking the replacement.
- `_handle_listener_error()` distinguishes a transient RPC error (logged, retried after 1s, counted in `_crash_times` / the new `event_listener_errors` stat) from a dead subprocess: `_rpc_server_exit_code()` probes `transport.process.poll()`, and any exit code — `0` included — stops the loop and escalates as fatal code `rpc_server_died`. Builds on the v1.7.3 transport fix, which is what makes `get_next_event()` reliably *raise* (rather than hang) once the server is gone.

### Fixed
- `disconnect()` now runs `_cleanup()` from a `finally`, so a raising `CallManager.teardown()` can no longer skip it and leak the RPC subprocess + accounts-dir lock. `_on_listener_done` (was `_on_event_task_done`) logs an escaped listener crash with `exc_info`.

### Tests
- `tests/conftest.py`: `MockBasePlatformAdapter` gains the fatal-error contract (`is_connected`, `has_fatal_error`, `fatal_error_code`/`message`/`retryable`, `set_fatal_error_handler`, `_set_fatal_error`, `_notify_fatal_error`) and now mirrors the real base's `_running`-is-`is_connected` and its refusal to downgrade a recorded fatal error.
- `tests/test_adapter_integration.py`: `TestEventSupervisor` replaced with `TestListenerDeathEscalation` (transient-error retry vs dead-server escalation, cancellation-while-connected, gateway notify, deliberate-disconnect is not a crash, no self-restart) and `TestListenerDoneCallback`.
- Added `tests/test_rpc_server_death.py`: the `_rpc_server_exit_code()` probe, transient-vs-dead handling in `_handle_listener_error()`, and `disconnect()`/`_cleanup()` resilience.

## [1.7.4] - 2026-08-31

### Changed
- Delta Chat outbound text messages are now kept short and plain by default.
  The old behavior converted any reply over 40 lines into a styled HTML part
  (`_format_html_message`, now removed); Delta Chat's HTML view is a poor fit
  for conversational replies and the markdown markers still leaked into the
  plain-text fallback. Every outbound text reply now runs through a
  deterministic pipeline — markdown-strip → line/character split → send:
  - `_strip_markdown` additionally normalizes `*`/`+`/`•` bullet markers to
    `- `, handles closed ATX headings (`## x ##`), and drops fenced-code info
    strings while keeping the code body and its indentation. Headings,
    emphasis, links (`label (url)`), URLs, and ordinary punctuation are
    handled as before.
  - `_split_message` takes a new `max_lines` argument and splits a reply that
    exceeds either the line limit or the character limit at paragraph/line
    boundaries into ordered messages, each within both limits. Only a single
    line longer than the character limit falls back to a hard word-boundary
    split, which logs at `WARNING`. Nothing is truncated; only the first
    message carries the quote-reply.
  - New `DELTACHAT_MAX_MESSAGE_LINES` (config `max_message_lines`), default
    `20`, range 1–200. `DELTACHAT_MAX_MESSAGE_LENGTH` is unchanged.
  Attachments, voice messages, generated documents, replies, file delivery,
  the stored conversation, and every non-Delta Chat surface are unaffected.

### Docs
- `docs/CONFIGURATION.md` gains an "Outbound message formatting" section with
  the pipeline, the new variable, and profile-by-profile rollout/validation
  notes. `Agents.md` and `plugin.yaml` document the new variable.

### Tests
- `tests/test_adapter.py`: extended `TestStripMarkdown` (bullets, numbered
  lists, closed headings, fenced-code body, URL/punctuation/Unicode
  preservation) and `TestSplitMessage` (line-count split, exact-boundary,
  one-over boundary, long paragraph, lists, Unicode combining marks,
  multi-part ordering, default line limit).
- `tests/test_adapter_integration.py`: replaced `TestHTMLFormatting` with
  `TestPlainTextDelivery`, covering markdown-free delivery, ordered
  multi-message split, single-message short replies, and first-chunk-only
  quote-reply through the real `send()` path.

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
- A quote-reply to one of the bot's own messages is again treated as an implicit mention in a mention-gated group (`DELTACHAT_REQUIRE_MENTION`, or a chat listed in `DELTACHAT_REQUIRE_MENTION_CHANNELS`). The reply-to-self check compared `quote.author_display_name` against the configured `display_name`, but Delta Chat core reports that field as the localized "Me" stock string (`"Me"`/`"Ich"`/`"Já"`…) for a self-authored quote, never the displayname, so the match always failed and the reply was silently dropped. Now `_quote_is_self_authored()` fetches the quoted message and checks `from_id == DC_CONTACT_ID_SELF` (locale-independent); the old name match is kept only as a fallback for when the quoted message is not available locally. The `[replying to …]` context spliced into the text now uses the bot's real display name instead of "Me". Latent since the check was added in v1.5.11 — only reachable once a group is mention-gated, which `DELTACHAT_REQUIRE_MENTION_CHANNELS` (v1.6.0) made possible without gating every group. Explicit `@mention`, authorization, allowlist/open policy, and the bot-loop / bot-exchange guards are unaffected.

### Tests
- Added `test_reply_to_own_message_is_implicit_mention`, `test_reply_to_other_member_still_gated`, and `test_reply_to_own_message_explicit_mention_still_works` to `TestMentions` (integration), covering the real `MessageQuote.WithMessage` shape and the `DELTACHAT_REQUIRE_MENTION_CHANNELS` config.

## [1.7.1] - 2026-08-27

### Fixed
- `filter_media_delivery_paths`/`filter_local_delivery_paths` now forward `session_key` to `BasePlatformAdapter` only when the installed core's implementation actually declares that parameter (checked via `inspect.signature`, `_base_supports_session_key()`). v1.6.2 forwarded it unconditionally, which fixed newer cores but crashes on Hermes 0.15.1's single-positional-argument base with the exact same `TypeError` this was meant to fix — reported by upstream on [PR #6](https://github.com/Simon-Laux/hermes-deltachat-platform/pull/6).

### Added
- `enforces_own_access_policy` property (`True`), the documented `BasePlatformAdapter` contract Hermes core reads via `getattr(adapter, "enforces_own_access_policy", False)`. Lets core honor `dm_allowed_users`/`group_allowed_users` configured in `config.yaml`'s `extra:` block (as opposed to the `DELTACHAT_ALLOWED_USERS` env var) when the adapter's effective policy is exactly `"allowlist"` and no env allowlist is set at all — core previously had no way to know this adapter already gated such senders in that specific configuration and denied them regardless. Does not affect `dm_policy`/`group_policy: open` or `pairing` (core only trusts an actual `"allowlist"` policy) or any deployment using the `DELTACHAT_ALLOWED_USERS`/`DELTACHAT_ALLOW_ALL_USERS` env vars, which were already handled by the existing `allowed_users_env`/`allow_all_env` declaration.

### Tests
- Added `TestBaseSupportsSessionKey` and `TestEnforcesOwnAccessPolicy` (unit).
- Added `test_omits_session_key_when_base_does_not_support_it` covering the old-core (single positional argument) case end to end.

## [1.7.0] - 2026-08-26

### Changed
- **Breaking (reverts v1.6.0):** Plugin renamed back from `deltachat` to `deltachat-platform`. `-platform` turned out to be Hermes's own naming convention for messaging platform plugins, not something to drop — v1.6.0's rename was a mistake. `plugin.yaml`'s `name`, the registered `Platform` id, the `hermes plugins enable`/`disable` argument, and `skill_view('plugin:deltachat-platform:webxdc-converter')` are all back to the pre-v1.6.0 values. If your Hermes `config.yaml` has a `platforms: deltachat:` block from a v1.6.x install, rename that key back to `platforms: deltachat-platform:`. `DELTACHAT_*` env vars are unaffected — they were never part of the rename.
- Default Delta Chat account-data directory restored to `~/.hermes/deltachat-platform/` (`_default_dc_data_dir()` in `adapter.py`, mirrored in `setup.py`). Falls back to a v1.6.x install's `~/.hermes/deltachat/` automatically when it already holds an account and the restored default doesn't, so v1.6.x installs need no manual data migration — just update `config.yaml` (see below) and reconnect.
- `scripts/migrate_deltachat_platform_rename.py`'s default direction now restores `deltachat-platform` (matching this release); pass `--reverse` for the old v1.5.x→v1.6.x direction.
- `docs/UPGRADING.md` rewritten around this reversal: anyone coming from v1.5.x or earlier straight to v1.7.0+ has nothing to do; anyone on a v1.6.0–v1.6.4 install follows the same three steps as before (optional directory rename, two `config.yaml` keys, routing/session migration script) with the names swapped.

## [1.6.4] - 2026-08-26

### Fixed
- `_apply_profile` (runs on every `connect()`, including reconnects) now checks the account's current `displayname`/`bot` config via `get_config` before calling `set_config`, and skips the call when the value is already correct. Unconditionally re-setting an unchanged `displayname` on every reconnect causes DC core to gossip an updated Autocrypt header to 1:1 chat partners, which their Delta Chat clients surface as a "verification changed" system message in the chat on every gateway restart — even though nothing about the bot's identity or verification actually changed. `selfavatar` is left unconditional (DC stores its own copy at an internal blob path, so a simple value comparison against the source path would never match).

### Tests
- Added `test_apply_profile_skips_unchanged_displayname_and_bot`.
- Updated the three existing `_apply_profile`/`_configure_account` tests to mock `get_config` so the (now-conditional) `set_config` calls still fire as expected.

## [1.6.3] - 2026-08-25

### Added
- `docs/UPGRADING.md`: documents the update path from v1.5.x to v1.6.0+, previously undocumented — the three things a plain `git pull` doesn't handle after the `deltachat-platform` → `deltachat` rename: the (optional) install directory rename, the two `config.yaml` keys (`plugins.enabled`, `platforms.<name>`) that must be renamed by hand or the plugin silently shows as "not enabled", and existing chat routing/session state that's orphaned unless migrated.
- `scripts/migrate_deltachat_platform_rename.py`: a checked-in, dry-run-by-default migration tool for the routing/session part of the above — rewrites `session_key`/`platform`/`origin.platform` in a profile's `state.db` (`gateway_routing` table) and its `sessions.json` mirror from the old platform id to the new one. Backs up both files before writing (`*.bak-rename-<timestamp>`), never touches `session_id` or message content, and is safe to re-run (idempotent). This generalizes the ad-hoc script used to migrate the two profiles that hit the issue live.
- `docs/troubleshooting.md`: new "Plugin Shows 'not enabled' / 'not a valid Platform'" section pointing at the upgrade guide.

## [1.6.2] - 2026-08-25

### Fixed
- `filter_media_delivery_paths`/`filter_local_delivery_paths` now accept the `session_key: str = ""` keyword argument Hermes core passes when calling them on the adapter instance (`self.filter_media_delivery_paths(media_files, session_key=session_key)`), and forward it to `BasePlatformAdapter`'s implementation. Without it every reply containing a MEDIA directive or local file path crashed with `TypeError: ... got an unexpected keyword argument 'session_key'`, surfaced to the user as "Sorry, I encountered an error (TypeError)." Every other overridden `BasePlatformAdapter` method was audited against the installed Hermes core and either matches exactly or already absorbs new keyword arguments via `**kwargs`.

### Tests
- Added `test_accepts_session_key_kwarg` to `TestFilterLocalDeliveryPaths` covering the exact call shape Hermes core uses.
- Updated `MockBasePlatformAdapter.filter_media_delivery_paths`/`filter_local_delivery_paths` in `tests/conftest.py` to accept `session_key` too, matching the real base class.

## [1.6.1] - 2026-08-25

### Changed
- The "Delta Chat version is newer than the minimum required version" message now logs at `INFO` instead of `WARNING`. `MIN_DC_VERSION` is a floor, not a pin, so running a newer core is the common case and was producing a WARNING-level line on every single startup for no actionable reason. The "too old" rejection case is unchanged (still logged at `ERROR`, connection still refused).

### Tests
- `test_version_newer_warns` → `test_version_newer_logs_info_and_allows`: asserts the message is captured at `INFO` and that no `WARNING`-level record is emitted for this case.

## [1.6.0] - 2026-08-25

### Added
- `dc_send_message` gains a `file_path` parameter: pushes a proactive attachment (e.g. an agent-generated `.md` report) instead of/alongside `text`. Routed through `filter_local_delivery_paths()` — the same pipeline the reply-flow MEDIA directive uses — so a `/workspace/` path (Docker sandbox) goes through the cache-copy guard, and any other absolute path (non-Docker deployments) flows to Hermes's own denylist-aware host-path validator. Sent via `send_document` with `text` (if given) as the caption. At least one of `text`/`file_path` is now required (previously `text` alone).
- `DELTACHAT_REQUIRE_MENTION_CHANNELS` (comma-separated group chat IDs): the inverse of `DELTACHAT_FREE_RESPONSE_CHANNELS`. When `DELTACHAT_REQUIRE_MENTION=false` (the default), every group responds freely except the chat IDs listed here, which stay mention-gated. Lets most groups stay conversational while a specific noisy support/ops group requires `@DisplayName`.
- Non-Docker file delivery, adapted from an unmerged fix upstream (`Simon-Laux/hermes-deltachat-platform#3`, branch `fix/workspace-path-resolution-v2`): `extract_local_files`'s bare-`.xdc` regex now matches any absolute or `~/`-relative path, not just `/workspace/` (mirroring `extract_media`, which already did). The platform hint and the `webxdc-converter` skill now tell the agent to write output to its current working directory and reference it by absolute path, noting that the working directory is `/workspace/` specifically in the Docker sandbox. `_container_workspace_to_host`'s traversal-containment check (`.resolve()` + `is_relative_to()`) was already present here independent of that upstream branch.

### Changed
- **Breaking:** Plugin renamed from `deltachat-platform` to `deltachat` — `plugin.yaml`'s `name`, the registered `Platform` id, and the `hermes plugins enable`/`disable` argument all change. If your Hermes `config.yaml` has a `platforms: deltachat-platform:` block, rename that key to `platforms: deltachat:`. `DELTACHAT_*` env vars are unaffected. The recommended install directory is now `~/.hermes/plugins/deltachat/`; the `webxdc-converter` skill reference is now `plugin:deltachat:webxdc-converter`.
- Default Delta Chat account-data directory renamed from `~/.hermes/deltachat-platform/` to `~/.hermes/deltachat/` (`_default_dc_data_dir()` in `adapter.py`, mirrored in `setup.py`). Falls back to the old directory automatically when it already holds an account and the new one doesn't, so existing installs need no manual migration — just enable the plugin under its new name and reconnect.

### Fixed
- `dc_send_message`'s `file_path` no longer hard-requires `/workspace/` — it now accepts any path a non-Docker deployment's agent can reach, validated the same way the reply-flow MEDIA pipeline validates one, instead of a plugin-local `/workspace/`-only check.

### Tests
- Added `TestDcSendMessageFilePath` (file-path validation, sandbox-copy failure, success-with-caption, non-Docker path passthrough).
- Added `TestRequireMentionChannels` (unit) and 3 new `TestMentions` cases (integration) covering the opt-in mention mode alongside the existing legacy `require_mention=true` behavior.
- Added `tests/test_workspace_paths.py`: `_container_workspace_to_host` mapping + traversal containment, generalized `.xdc` extractors, and `filter_local_delivery_paths` remap-vs-passthrough split. Added matching mocks (`extract_media`, `extract_local_files`, `filter_media_delivery_paths`, `filter_local_delivery_paths`) to `MockBasePlatformAdapter` in `tests/conftest.py` — this pipeline had no test coverage before.
- Added `TestDefaultDcDataDir` and an integration test covering the deltachat-platform -> deltachat data-dir fallback.

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
