# Delta Chat Platform Plugin for Hermes Agent

A Hermes platform plugin that integrates Delta Chat as a messaging channel.
Uses **deltachat2** for direct JSON-RPC access (not abstracted away).

## Quick Start

```bash
# Install deltachat-rpc-server binary
pip install deltachat-rpc-server

# Clone plugin to Hermes
git clone https://github.com/Simon-Laux/hermes-deltachat-platform ~/.hermes/plugins/deltachat-platform

# Enable plugin
hermes plugins enable deltachat-platform

# Start gateway
hermes gateway start
```

## Installation

### 1. Install deltachat-rpc-server

**Option A: pip (recommended for most systems)**
```bash
pip install deltachat-rpc-server
```

**Option B: From source**
```bash
git clone https://github.com/chatmail/core
cd core
cargo build -p deltachat-rpc-server --release
# Binary: target/release/deltachat-rpc-server
```

**Option C: NixOS**
```bash
nix profile install nixpkgs#deltachat-rpc-server
echo 'DELTACHAT_RPC_SERVER=/home/work/.nix-profile/bin/deltachat-rpc-server' >> ~/.hermes/.env
```

### 2. (Optional) Configure RPC server path

If binary is not in PATH:
```bash
# For default profile
echo 'DELTACHAT_RPC_SERVER=/path/to/deltachat-rpc-server' >> ~/.hermes/.env

# For named profile
hermes -p my-profile config set env.DELTACHAT_RPC_SERVER /path/to/deltachat-rpc-server
```

### 3. Enable Plugin

```bash
hermes plugins enable deltachat-platform
```

### 4. Create Account

Run the setup script. It will **auto-detect your Hermes profiles** and let you select one:

```bash
python ~/.hermes/plugins/deltachat-platform/setup.py
```

The script will:
1. List all available Hermes profiles
2. Let you select which profile to configure
3. Create the account and display its **Delta Chat address** (e.g., `mybot@nine.testrun.org`)
4. Save the account in that profile's `deltachat-platform/` directory

### 5. Start the gateway
```bash
# Start gateway (automatically connects to first DC account)
hermes gateway start
```

## Usage

### Multiple Agents

Each Hermes profile = one Delta Chat account:

```bash
# Create profiles
hermes profile create work
hermes profile create personal

# Each gets its own DC config at:
# ~/.hermes/profiles/work/deltachat-platform/
# ~/.hermes/profiles/personal/deltachat-platform/

# Start gateways
work gateway start
personal gateway start
```

## Features

### Phase 1: Messaging Adapter
- Bidirectional text messaging via direct JSON-RPC
- Multi-agent support via Hermes profiles
- Automatic account selection (first available)
- Chat metadata (name, type, user info)
- Version guard: blocks older than 2.51.0, warns on newer

### Phase 2: Webxdc Support
- Send .xdc files via `send_file()` RPC call
- Bundled `webxdc-converter` skill
- Access via `skill_view("plugin:deltachat-platform:webxdc-converter")`

### Raw RPC Access (Experimental, opt-in)

Three tools are always available once the plugin is loaded:

| Tool | Description |
|------|-------------|
| `dc_rpc_spec` | Full OpenRPC spec from the running server — all methods, params, types |
| `dc_chat_rpc_spec` | Spec filtered to chat-scoped methods only, destructive ops removed |
| `dc_safe_rpc_call` | Call a chat-scoped method safely: `accountId` and `chatId` are injected from an opaque per-chat token the LLM receives in each message (`[dc:chat=<token>]`), so it cannot address a different chat. Destructive methods are blocked. |

The chat token is stored in Delta Chat UI config (`ui.hermes.chat_token.<chat_id>`) so it survives restarts — the same chat always gets the same token.

Set `DELTACHAT_ENABLE_RAW_RPC=1` to also unlock:

| Tool | Description |
|------|-------------|
| `dc_rpc_call` | Call **any** RPC method by name and params — unrestricted |

```bash
echo 'DELTACHAT_ENABLE_RAW_RPC=1' >> ~/.hermes/.env
```

> **Warning:** `dc_rpc_call` has unrestricted access to the Delta Chat core, including destructive operations (delete accounts, wipe messages, change config). Only enable in trusted deployments.

The combination lets Hermes use the full Delta Chat API without adapter code: it can read the spec to discover new features and invoke them directly.

### Phase 3: Voice Messages (Planned)
- Audio attachment detection
- Automatic transcription via Hermes STT
- Forward transcription as text to AI

### Phase 4: Voice Calls (Planned)
- Incoming call detection via `IncomingCall` event
- WebRTC bridge via aiortc
- Uses `iceServers()` and `acceptIncomingCall()` RPC methods
- Real-time audio processing

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DELTACHAT_RPC_SERVER` | No | `deltachat-rpc-server` | Path to RPC binary |
| `DELTACHAT_HOME_CHANNEL` | No | - | Default chat for cron delivery |
| `DELTACHAT_ENABLE_RAW_RPC` | No | - | Enable raw RPC tools (see below) |

## Documentation

- [Version Compatibility](docs/version-compatibility.md) - Version requirements
- [File Structure](docs/file-structure.md) - Directory layout
- [Troubleshooting](docs/troubleshooting.md) - Common issues

## Development

### Vendored Dependencies

The `deltachat2` Python package is vendored in the `vendor/` directory for simplified
installation. This avoids requiring users to manually install the package.

**Note:** If you need to update the vendored `deltachat2` package:
1. Fetch the latest version from [adbenitez/deltachat2](https://github.com/adbenitez/deltachat2)
2. Copy the `deltachat2/` directory contents to `vendor/deltachat2/`
3. Test thoroughly as API changes may affect compatibility
4. Update the minimum version in `adapter.py` if needed

## License

Mozilla Public License 2.0 (MPL-2.0)

---

## References

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [Hermes Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
- [Delta Chat](https://delta.chat/)
- [deltachat2 PyPI](https://pypi.org/project/deltachat2/)
- [deltachat-rpc-server](https://github.com/chatmail/core/tree/main/deltachat-rpc-server)
- [Delta Chat JSON-RPC API](https://github.com/chatmail/core/blob/main/deltachat-jsonrpc/src/api.rs)
- [Delta Chat Core](https://github.com/chatmail/core)
- [Webxdc](https://webxdc.org/)
- [aiortc](https://aiortc.readthedocs.io/)
