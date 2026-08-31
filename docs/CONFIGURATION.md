# Configuration

All Delta Chat platform settings are read from environment variables (or from `config.extra` if you manage plugins programmatically).

## Required

| Variable | Description |
|----------|-------------|
| `DELTACHAT_RPC_SERVER` | Path to the `deltachat-rpc-server` binary. Defaults to looking on `PATH` for `deltachat-rpc-server`. |

## Account / onboarding

| Variable | Default | Description |
|----------|---------|-------------|
| `DELTACHAT_EMAIL` | `auto` | Bot email address, or `auto` to create a free chatmail account automatically. |
| `DELTACHAT_PASSWORD` | — | Email password. Required when `DELTACHAT_EMAIL` is set to a real address. |
| `DELTACHAT_DATA_DIR` | `~/.hermes/deltachat-platform` | Directory for Delta Chat account data. Created with `0o700` permissions. |
| `DELTACHAT_CHATMAIL_SERVER` | `nine.testrun.org` | Chatmail server used for auto accounts. |
| `DELTACHAT_CHATMAIL_SERVERS` | — | Comma-separated list of chatmail servers to try in order. Overrides `DELTACHAT_CHATMAIL_SERVER`. |
| `DELTACHAT_DISPLAY_NAME` | `Hermes` | Display name shown to contacts. |
| `DELTACHAT_AVATAR_PATH` | — | Path to a bot avatar image (`.png`, `.jpg`, `.jpeg`, `.gif`, or `.webp`). |

## Access control

| Variable | Default | Description |
|----------|---------|-------------|
| `DELTACHAT_ALLOW_ALL_USERS` | `false` | Set to `true` to allow anyone to use the bot. Overrides all per-sender checks. |
| `DELTACHAT_ALLOWED_USERS` | — | Comma-separated email addresses allowed to use the bot. |
| `DELTACHAT_DM_POLICY` | `pairing` | Direct-message policy: `open`, `allowlist`, `pairing` (verified contacts only), or `disabled`. |
| `DELTACHAT_DM_ALLOWED_USERS` | — | Comma-separated emails allowed for `allowlist` DM policy. |
| `DELTACHAT_GROUP_POLICY` | `open` | Group-chat policy: `open`, `allowlist`, or `disabled`. |
| `DELTACHAT_GROUP_ALLOWED_USERS` | — | Comma-separated emails allowed for `allowlist` group policy. |
| `DELTACHAT_REQUIRE_MENTION` | `false` | In group chats, only respond to messages that mention the bot (`@DisplayName` or whole-word name). Applies to text and media captions. |
| `DELTACHAT_SEND_REJECTION_REPLIES` | `true` | Send an explanation when a message is rejected by policy. |

## Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `DELTACHAT_MAX_MESSAGE_LENGTH` | `3600` | Character limit for automatic message splitting (min 100, max 10000). |
| `DELTACHAT_MAX_MESSAGE_LINES` | `20` | Line limit per outbound message (min 1, max 200). Replies over the limit are stripped of markdown and split at paragraph/line boundaries into ordered plain-text messages. |
| `DELTACHAT_REQUIRE_MENTION` | `false` | Require `@DisplayName` mention in groups (see above). |
| `DELTACHAT_RATE_LIMIT_MAX` | `30` | Max inbound messages per sender per window. |
| `DELTACHAT_RATE_LIMIT_WINDOW` | `60` | Rate-limit window in seconds. |
| `DELTACHAT_HOME_CHANNEL` | — | Chat ID for cron/proactive delivery (or use `/sethome` in chat). |

## Advanced / developer

| Variable | Default | Description |
|----------|---------|-------------|
| `DELTACHAT_ENABLE_RAW_RPC` | — | Set to `1`/`true` to unlock unrestricted `dc_rpc_call`. |
| `DELTACHAT_DEBUG` | — | Set to `1`/`true` to enable debug logs from `deltachat2`. |

## Environment examples

```bash
# Use a real email address
DELTACHAT_EMAIL=bot@example.com
DELTACHAT_PASSWORD='very-secret'

# Or let the plugin create a chatmail account automatically
DELTACHAT_EMAIL=auto
DELTACHAT_CHATMAIL_SERVERS=nine.testrun.org,mail.example.com

# Restrict to a group that must @-mention the bot
DELTACHAT_REQUIRE_MENTION=true
DELTACHAT_DISPLAY_NAME="My Assistant"

# Shorter, chattier Delta Chat messages (default is already 20 lines)
DELTACHAT_MAX_MESSAGE_LINES=15
```

## Outbound message formatting

Delta Chat has no markdown rendering. Every outbound text reply runs through a
deterministic plain-text pass immediately before sending:

1. **Markdown is removed** — pipe tables collapse to `Header: value` lines,
   headings lose `#`, `*`/`_` emphasis is unwrapped, `[label](url)` becomes
   `label (url)`, fenced-code delimiters are dropped (code body and its
   indentation kept), and `*`/`+`/`•` bullets become `- `. URLs and ordinary
   punctuation are left alone.
2. **Long replies are split** — a reply over `DELTACHAT_MAX_MESSAGE_LINES`
   (default 20) or `DELTACHAT_MAX_MESSAGE_LENGTH` characters is broken at
   paragraph/line boundaries into several ordered messages, each within both
   limits. Only a single line longer than the character limit falls back to a
   hard word-boundary split (logged at `WARNING`). Nothing is truncated; only
   the first message carries the quote-reply.

This applies **only** to the Delta Chat outbound text path — attachments, voice
messages, generated documents, the stored conversation, and every non-Delta
Chat surface are unchanged.

### Rollout

The defaults are conservative and safe for every Hermes profile; no config
change is required to adopt this. To tune per profile, set
`DELTACHAT_MAX_MESSAGE_LINES` (or `max_message_lines:` in the `extra:` block of
`config.yaml`) and restart the gateway. Lower it for a more staccato feel,
raise it (up to 200) to keep more content in one message. Validate against a
Delta Chat test destination by sending a reply containing headings, a list, a
link, and >20 lines, and confirm it arrives as ordered plain-text messages
with no `#`/`*` markers.
