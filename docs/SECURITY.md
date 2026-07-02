# Security Notes

## URL image sending

When the AI sends an image by URL, the adapter downloads it before forwarding it to Delta Chat. The following restrictions apply:

- **Scheme**: only `http://` and `https://` URLs are accepted.
- **Redirects**: `httpx` is configured with `follow_redirects=False` to avoid open-redirect issues.
- **Size limit**: downloads are bounded to **25 MiB** by both the `Content-Length` header and the streamed response size.
- **Content-Type**: the response must declare `image/*`; non-image responses are rejected.
- **Temporary files**: downloaded images are written to a temporary file, sent, and then deleted.

If `httpx` is not installed in the runtime environment, URL image sending returns an error.

## Data directory permissions

The Delta Chat data directory (`DELTACHAT_DATA_DIR`) is created with `0o700` permissions. The adapter logs a warning if the directory is group- or world-readable.

## Raw RPC access

The `dc_safe_rpc_call` tool is restricted to methods that accept a `chatId` and destructive operations are blocked. Set `DELTACHAT_ENABLE_RAW_RPC=1` to also expose `dc_rpc_call`, which allows unrestricted access to the account — only enable this in trusted deployments.

## Contact verification

The default DM policy (`pairing`) only responds to verified contacts. Use the SecureJoin invite link printed at startup (or shown in `get_status()`) to establish a verified session.
