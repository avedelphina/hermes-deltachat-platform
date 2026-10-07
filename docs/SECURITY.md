# Security Notes

## URL image sending

When the AI sends an image by URL, the adapter downloads it before forwarding it to Delta Chat. The following restrictions apply:

- **Scheme**: only `http://` and `https://` URLs are accepted.
- **Target**: URLs that resolve to loopback, private or link-local addresses are refused. Hermes' own URL policy decides when available, so its `security.allow_private_urls` setting is honoured.
- **Redirects**: `httpx` is configured with `follow_redirects=False` to avoid open-redirect issues.
- **Size limit**: downloads are bounded to **25 MiB** by both the `Content-Length` header and the streamed response size.
- **Content-Type**: the response must declare `image/*`; non-image responses are rejected.
- **Temporary files**: downloaded images are written to a temporary file, sent, and then deleted.

If `httpx` is not installed in the runtime environment, URL image sending returns an error.

## Data directory permissions

The Delta Chat data directory (`DELTACHAT_DATA_DIR`) is created with `0o700` permissions. The adapter logs a warning if the directory is group- or world-readable.

## Raw RPC access

The `dc_safe_rpc_call` tool is restricted to methods that accept a `chatId`. It refuses destructive methods (`delete_*`, `remove_*`, `leave_group`, `set_chat_ephemeral_timer`), methods that reach outside the chat or hand out credentials (`forward_messages`, `add_contact_to_chat`, the SecureJoin QR methods), and methods that hide traffic (`block_chat`, mute, visibility). File paths in its parameters go through Hermes' delivery policy; the Delta Chat data directory of every Hermes profile and `logs/` are always refused.

Set `DELTACHAT_ENABLE_RAW_RPC=1` to also expose `dc_rpc_call`, which reaches the whole account — only enable this in trusted deployments. It refuses the same methods, but does **not** validate file paths. Limit it with `DELTACHAT_RAW_RPC_ALLOWLIST`. Every call is logged at WARNING as ACCEPTED or REFUSED.

## Database identity

Hermes keys pairing approvals, sessions, `DELTACHAT_HOME_CHANNEL` and cron targets on Delta Chat contact and chat IDs. Those IDs only mean something inside one database: a recreated database hands an approved contact's ID to someone else. The adapter stores a random ID in the account (`ui.hermes.db_id`) and in `<HERMES_HOME>/.deltachat-db-id`, and refuses to start when they differ. The error message lists the steps to start over. Restoring an older backup of the same database is not detected.

## Voice calls

Incoming calls are declined unless the caller passes the same sender rules as a direct message (`allowed_users`, `dm_policy`) and Hermes' own authorization.

## Senders without a key

Identity in Delta Chat is the key. A message from a contact without one is plain unencrypted mail, whose From address can be forged, so it is dropped silently and unread. This also means the bot does not answer people who write to its address from an ordinary email client.

## Chat tokens

Each message carries an opaque `[dc:chat=<token>]` tag that scopes the RPC tools to that chat. `dc_safe_rpc_call` and `dc_start_call` only accept a token from the conversation it belongs to; `dc_send_message` accepts any, since sending to another chat is its purpose.

## Contact verification

The default DM policy (`pairing`) only responds to verified contacts. Use the SecureJoin invite link (written to `invite.txt`, mode 0600, in the data directory on every connect, and shown in `get_status()`) to establish a verified session.
