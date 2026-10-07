# Troubleshooting

Common issues and solutions for the Delta Chat plugin.

## Plugin Not Loading

**Symptom:** Plugin doesn't appear in `hermes plugins list`

**Check:**
```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

**Common causes:**
- Plugin not in correct directory: `~/.hermes/plugins/deltachat-platform/`
- Missing `plugin.yaml` or `__init__.py`
- Syntax error in plugin files
- Missing dependencies

**Fix:**
```bash
# Verify directory structure
ls -la ~/.hermes/plugins/deltachat-platform/

# Check for syntax errors
python3 -m py_compile ~/.hermes/plugins/deltachat-platform/__init__.py
python3 -m py_compile ~/.hermes/plugins/deltachat-platform/adapter.py

# Install dependencies
pip install deltachat2
```

## Connection Errors

**Symptom:** Gateway fails to connect to Delta Chat

**Check:**
```bash
# View gateway logs
tail -f ~/.hermes/profiles/<name>/logs/gateway.log

# Verify RPC server is accessible
which deltachat-rpc-server

# Test RPC server manually
deltachat-rpc-server --help
```

**Common causes:**
- `deltachat-rpc-server` binary not in PATH
- Binary not installed
- Permission issues
- Firewall blocking RPC communication

**Fix:**
```bash
# Set explicit path in environment
echo 'DELTACHAT_RPC_SERVER=/path/to/deltachat-rpc-server' >> ~/.hermes/.env

# Or for a specific profile
hermes -p my-profile config set env.DELTACHAT_RPC_SERVER /path/to/deltachat-rpc-server

# Install binary
pip install deltachat-rpc-server
```

## No Accounts Found

**Symptom:** "No Delta Chat accounts found" error

**Check:**
```bash
# Verify deltachat-platform directory exists
ls -la ~/.hermes/deltachat-platform/

# Or for specific profile
ls -la ~/.hermes/profiles/<name>/deltachat-platform/

# On a v1.6.x install, the account may still be under the old (temporary)
# name — check that too:
ls -la ~/.hermes/deltachat/
```

**Common causes:**
- No Delta Chat account created yet
- Account created in different config directory
- DC_ACCOUNTS_PATH pointing to wrong location

**Fix:**
```bash
# Create an account using setup.py or Delta Chat desktop/mobile app first
# Then the account will be in the correct directory
```

## Plugin Shows "not enabled" / "... is not a valid Platform"

**Symptom:** `hermes plugins list` shows `deltachat`/`deltachat-platform` as `not enabled`, and/or the gateway log is full of `Skipping invalid routing entry '...': '...' is not a valid Platform`.

**Cause:** v1.6.0 renamed the plugin from `deltachat-platform` to `deltachat`; v1.7.0 reverted that rename. Either transition leaves `config.yaml`'s `plugins.enabled`/`platforms:` keys and Hermes's persisted chat routing state pointing at the plugin's *previous* name after a plain `git pull`.

**Fix:** See [docs/UPGRADING.md](UPGRADING.md) — two `config.yaml` key renames plus a one-time migration script for existing chat sessions, for whichever transition applies to you.

## Plugin Refuses to Start After an Upgrade

**Symptom:** the gateway log shows `Platform 'Delta Chat' config validation error: DELTACHAT_DM_POLICY is 'allowlist' but neither DELTACHAT_DM_ALLOWED_USERS nor DELTACHAT_ALLOWED_USERS names anyone` (or the same for `GROUP`).

**Cause:** since v1.11.0 an `allowlist` policy that names nobody admits nobody. Earlier versions admitted everybody.

**Fix:** list the allowed addresses in `dm_allowed_users` / `group_allowed_users` or `allowed_users`, or choose another policy.

**Symptom:** the gateway log shows `The Delta Chat database does not belong to this Hermes state` and the adapter reports `deltachat_db_mismatch`.

**Cause:** the ID stored in the account differs from the one in `<HERMES_HOME>/.deltachat-db-id`. The accounts directory was deleted, recreated or replaced by another database. Hermes' pairing approvals, sessions, home channel and cron targets refer to contact and chat IDs of the old database, and in the new one those IDs belong to other people.

**Fix:** if you replaced the database by mistake, restore the right one. If you want to start over on the new database, follow the steps in the log message: revoke the `deltachat-platform` pairing approvals, delete its sessions, clear `DELTACHAT_HOME_CHANNEL` and cron targets, then delete the marker file and restart.

## Bot Does Not Answer a Sender

Since v1.11.0 these cases are dropped on purpose:

- **The sender has no Delta Chat key** (plain email). Log, at DEBUG: `Dropping message N from contact M: no key`. The sender must use Delta Chat and scan the invite link.
- **A photo, file or voice message without a caption in a mention-gated group.** Add a caption that mentions the bot, or quote-reply to one of its messages.
- **A group paused by the bot-exchange guard.** Log: `bot_exchange_guard tripped in chat N`. A message from a human resumes it. `DELTACHAT_MAX_BOT_EXCHANGES=0` turns the guard off.
- **An agent tool answers `This chat_token belongs to a different conversation`.** `dc_safe_rpc_call` and `dc_start_call` only accept the token of the chat they are used in. Use `dc_send_message` to write to another chat.

## Version Warning

**Symptom:** "Delta Chat version X.X.X is newer than expected" warning

**What it means:**
- Your Delta Chat version is newer than what the plugin was tested with
- Most features should still work
- Some newer features may not be available through the plugin

**Check your version:**
```bash
deltachat-rpc-server --version
```

**Solutions:**
1. **Update the plugin:** Check if a newer version of this plugin exists
2. **Update MIN_DC_VERSION:** Edit `adapter.py` to match your version:
   ```python
   MIN_DC_VERSION = "2.52.0"  # Change to your version
   ```
3. **Ignore it:** The warning is informational; connection will still work

## Message Sending Fails

**Symptom:** Messages don't send, `send()` returns error

**Check:**
```bash
# Enable debug logging
HERMES_LOG_LEVEL=DEBUG hermes gateway start

# Check for specific errors in logs
grep -i error ~/.hermes/profiles/<name>/logs/gateway.log
```

**Common causes:**
- Invalid chat_id (must be integer string)
- Account not connected
- Network connectivity issues

**Fix:**
```bash
# Verify chat_id is valid
# Use get_chat_info() to check if chat exists
```

## File Sending Fails

**Symptom:** `.xdc` or other files don't send

**Check:**
```bash
# Verify file exists and is readable
ls -la /path/to/your/file.xdc

# Check file permissions
file /path/to/your/file.xdc
```

**Common causes:**
- File path is incorrect
- File permissions prevent reading
- File type not supported by Delta Chat

**Fix:**
```bash
# Use absolute paths for files
# Ensure file exists before sending
```

## Voice Call Issues

See [voice-calls.md](voice-calls.md) for setup. Voice calls need `aiortc`.

**Symptom:** the bot declines an incoming call. Log: `Declining call N from unauthorized contact M`.

**Cause:** since v1.11.0 a call is only answered when the caller may also message the bot: `allowed_users`, `dm_policy` and Hermes' own authorization all apply.

**Symptom:** the log shows `DELTACHAT_CALL_MODEL=... is set but the gateway runner is not reachable`.

**Cause:** the per-call model could not be installed, so the call runs on the default model.

**Symptom:** the bot says goodbye but does not hang up.

**Cause:** a custom `DELTACHAT_CALL_PROMPT` without the hang-up instruction. Tell the model to end its goodbye with `[[hangup]]`.

## Performance Issues

**Symptom:** Slow message delivery, high CPU usage

**Check:**
```bash
# Monitor RPC server process
top -p $(pgrep -f deltachat-rpc-server)

# Check event loop latency
# Enable debug logging for timing info
```

**Common causes:**
- Many active chats
- Large message history
- Slow network connection

**Solutions:**
- Limit number of active chats
- Archive old messages
- Use faster network connection

## Cleanup

**To completely remove the plugin:**
```bash
# Remove plugin files
rm -rf ~/.hermes/plugins/deltachat-platform/

# Remove profile-specific config
rm -rf ~/.hermes/profiles/*/deltachat-platform/
rm -rf ~/.hermes/deltachat-platform/

# On a v1.6.x install, also check the (temporary) renamed directories:
rm -rf ~/.hermes/plugins/deltachat/
rm -rf ~/.hermes/profiles/*/deltachat/
rm -rf ~/.hermes/deltachat/

# Remove from enabled plugins
hermes plugins disable deltachat-platform
```
