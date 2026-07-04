"""Delta Chat platform adapter for Hermes Gateway.

Integrates Delta Chat as a messaging platform using deltachat2 (direct JSON-RPC).
"""

import email.utils
import functools
import html
import json
import os
import re
import secrets
import shutil
import signal
import sys
import asyncio
import logging
import mimetypes
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Optional, Dict, Any

# Add vendor directory to sys.path so vendored deltachat2 can be imported
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
_vendor_dir = os.path.join(_plugin_dir, "vendor")
if os.path.exists(_vendor_dir) and _vendor_dir not in sys.path:
    sys.path.insert(0, _vendor_dir)

from gateway.platforms.base import (  # noqa: E402
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform, PlatformConfig  # noqa: E402

# Must use "hermes_plugins.*" prefix so records appear in gateway.log.
# __name__ resolves to "adapter" (standalone module), which only goes to agent.log.
logger = logging.getLogger("hermes_plugins.deltachat")

# Enable debug logging for RPC if requested
if os.getenv("DELTACHAT_DEBUG"):
    logging.getLogger("deltachat2").setLevel(logging.DEBUG)
    logging.getLogger("deltachat2.IOTransport").setLevel(logging.DEBUG)

# Minimum required Delta Chat core version
# Plugin will NOT connect with older versions
MIN_DC_VERSION = "2.51.0"

# DC truncates at ~3800; split conservatively
DC_MESSAGE_MAX_LEN = 3600

# Maximum image download size for send_image_file() URLs (25 MiB)
_MAX_IMAGE_SIZE = 25 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _cfg(config, env: str, key: str, default: str = "") -> str:
    """Read platform config: env var takes precedence over config.extra."""
    extra = getattr(config, "extra", {}) or {}
    val = os.getenv(env)
    if val:
        return val
    val = extra.get(key, default)
    if isinstance(val, bool):
        val = "true" if val else "false"
    return val if val is not None else default


def _strip_markdown(text: str) -> str:
    """Delta Chat renders plain text only; strip common markdown syntax."""
    if not text:
        return text
    text = re.sub(r"```(?:\w*\n)?(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+(.*)$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"(\*\*\*|___)(.+?)\1", r"\2", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<!\w)(\*|_)(.+?)\1(?!\w)", r"\2", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    return text


def _split_message(text: str, max_len: int = DC_MESSAGE_MAX_LEN) -> list[str]:
    """Split long text at paragraph/line/sentence/word boundaries."""
    if not text:
        return []
    if max_len < 1:
        max_len = DC_MESSAGE_MAX_LEN
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = -1
        # Try in order: paragraph break, line break, sentence end, word boundary.
        # Require split point past 25% of max_len so first chunk isn't tiny.
        for rfind_str, extra in [("\n\n", 0), ("\n", 0), (". ", 1), (" ", 0)]:
            idx = remaining.rfind(rfind_str, 0, max_len)
            if idx > max_len * 0.25:
                split_at = idx + extra
                break
        if split_at <= 0:
            split_at = max_len
            # Avoid splitting in the middle of a combining character.
            while split_at > 1 and unicodedata.combining(remaining[split_at]):
                split_at -= 1
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
        if not remaining:
            break
    if remaining:
        parts.append(remaining)
    return parts


def _is_valid_email(s: str) -> bool:
    """Return True if *s* looks like a plain email address."""
    if not s or len(s) > 254:
        return False
    if not _EMAIL_RE.match(s):
        return False
    real_name, addr = email.utils.parseaddr(s)
    return real_name == "" and addr.lower() == s.lower()


def _safe_data_dir(path: str, create: bool = False) -> Path:
    """Resolve and optionally create the Delta Chat data directory."""
    p = Path(path).expanduser()
    if ".." in p.parts:
        raise ValueError(f"data_dir may not contain '..': {path!r}")
    if create:
        p = p.resolve()
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o700)
        mode = p.stat().st_mode
        if mode & 0o077:
            logger.warning(
                "DeltaChat: data_dir %s has permissive mode %o; expected 0o700",
                p,
                mode & 0o777,
            )
    return p


def _validate_rpc_server_path(path: str, strict: bool = True) -> str:
    """Resolve the RPC server binary path. Raise ValueError if invalid."""
    if not path:
        raise ValueError("RPC server path must not be empty")
    resolved = shutil.which(path)
    if resolved:
        return resolved
    p = Path(path)
    if p.is_absolute() and p.is_file() and os.access(p, os.X_OK):
        return str(p)
    if not strict:
        return path
    raise ValueError(f"RPC server not found or not executable: {path!r}")


def _validate_avatar_path(path: Optional[str], strict: bool = True) -> Optional[str]:
    """Validate avatar image path. Raise ValueError if invalid."""
    if not path:
        return None
    suffix = Path(path).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise ValueError(f"DELTACHAT_AVATAR_PATH must be an image file: {path!r}")
    if strict:
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"DELTACHAT_AVATAR_PATH does not exist: {path!r}")
        return str(p)
    return path


# Lazy import to avoid dependency issues if deltachat2 not installed
_DC2_AVAILABLE = None


def _check_dc2_available():
    """Check if deltachat2 is available."""
    global _DC2_AVAILABLE
    if _DC2_AVAILABLE is None:
        try:
            import deltachat2

            _DC2_AVAILABLE = True
            return True
        except ImportError:
            _DC2_AVAILABLE = False
    return _DC2_AVAILABLE


def _parse_version(version_str: str) -> tuple:
    """Parse version string into tuple of ints for comparison.

    Args:
        version_str: Version string like "2.51.0" or "2.51.0-dev"

    Returns:
        Tuple of (major, minor, patch) integers
    """
    try:
        # Remove any suffixes like -dev, -rc1, etc. and leading 'v'
        base_version = version_str.lstrip("v").split("-")[0]
        parts = base_version.split(".")
        # Pad with zeros if needed
        while len(parts) < 3:
            parts.append("0")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


async def _check_dc_version(rpc) -> bool:
    """Check Delta Chat core version and enforce minimum.

    Args:
        rpc: DeltaChat2 RPC client

    Returns:
        True if version is compatible, False if too old or unknown
    """
    try:
        # Get system info which includes version
        system_info = await rpc.get_system_info()
        dc_version_str = system_info.get("deltachat_core_version", "0.0.0")
        dc_version = _parse_version(dc_version_str)
        min_version = _parse_version(MIN_DC_VERSION)

        if dc_version < min_version:
            logger.error(
                "Delta Chat version %s is too old. "
                "This plugin requires %s or higher. "
                "Please update your Delta Chat installation.",
                dc_version_str,
                MIN_DC_VERSION,
            )
            return False
        elif dc_version > min_version:
            logger.warning(
                "Delta Chat version %s is newer than the minimum "
                "required version %s. Continuing but untested.",
                dc_version_str,
                MIN_DC_VERSION,
            )
        else:
            logger.info(
                "Delta Chat version %s meets the minimum requirement %s.",
                dc_version_str,
                MIN_DC_VERSION,
            )
        return True
    except Exception as e:
        logger.error("Could not check Delta Chat version: %s", e)
        return False


# ---------------------------------------------------------------------------
# Access control: rate limiting, dedup, DM/group policy
# ---------------------------------------------------------------------------


def _parse_email_list(raw: str) -> set:
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _parse_chatmail_servers(raw: str) -> list[str]:
    """Return a list of non-empty, trimmed chatmail server hostnames."""
    servers = [s.strip() for s in raw.split(",") if s.strip()]
    # Remove duplicate hostnames while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for s in servers:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


class _RateLimiter:
    """Simple sliding-window rate limiter keyed by arbitrary strings."""

    def __init__(self, max_calls: int = 30, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        self._buckets: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = deque([now], maxlen=self.max_calls)
                return True
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                return False
            bucket.append(now)
            return True


class _MessageCache:
    """Bounded LRU set for duplicate message detection."""

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._deque: deque = deque(maxlen=max_size)
        self._set: set = set()

    def add(self, msg_id: str) -> bool:
        """Add *msg_id*. Return True if it was new, False if already seen."""
        if msg_id in self._set:
            return False
        if len(self._deque) >= self.max_size:
            oldest = self._deque.popleft()
            self._set.discard(oldest)
        self._deque.append(msg_id)
        self._set.add(msg_id)
        return True


async def _async_retry(
    coro_fn,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple = (Exception,),
):
    """Retry an async callable with exponential backoff."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except exceptions as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                "Operation failed (attempt %d/%d): %s; retrying in %.1fs",
                attempt + 1,
                max_attempts,
                e,
                delay,
            )
            await asyncio.sleep(delay)
    raise last_exc


class _AsyncRpc:
    """Wraps synchronous deltachat2.Rpc so every call runs in a thread executor.

    deltachat2.Rpc.transport.call() blocks on a threading.Event until the
    RPC server responds.  Calling it directly from an async function would
    freeze the asyncio event loop.  This wrapper makes every attribute access
    return an async function that runs the underlying sync call in the default
    ThreadPoolExecutor, keeping the event loop free.
    """

    def __init__(self, rpc) -> None:
        object.__setattr__(self, "_rpc", rpc)

    def __getattr__(self, name: str):
        method = getattr(object.__getattribute__(self, "_rpc"), name)

        async def _async_call(*args):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, method, *args)

        return _async_call


# Tracks the currently connected adapter instance; used by RPC tools.
_active_adapter = None

# Per-session opaque token ↔ real chat_id mapping.
# Tokens are generated once per unique chat_id using secrets.token_hex so they
# are unguessable and stable within a process lifetime.  They are injected into
# every incoming message text as "[dc:chat=<token>]" so the LLM always has the
# right token in its context without ever seeing the raw numeric id.
_chat_id_to_token: Dict[int, str] = {}
_chat_token_to_id: Dict[str, int] = {}

# Methods that mutate or destroy chat data — blocked from dc_safe_rpc_call.
_DESTRUCTIVE_METHODS = frozenset(
    {
        "delete_chat",
        "delete_messages",
        "delete_messages_for_all",
        "remove_contact_from_chat",
        "remove_draft",
        "leave_group",
    }
)


def _parse_method_list(value: Optional[str]) -> frozenset:
    """Parse a comma-separated list of RPC method names into a set."""
    if not value:
        return frozenset()
    return frozenset(m.strip() for m in value.split(",") if m.strip())


# Optional explicit allowlist/blocklist for the unrestricted dc_rpc_call tool.
# DESTRUCTIVE_METHODS and delete_/remove_ prefixes are always blocked.
_RAW_RPC_ALLOWLIST = _parse_method_list(os.getenv("DELTACHAT_RAW_RPC_ALLOWLIST"))
_RAW_RPC_BLOCKLIST = _parse_method_list(os.getenv("DELTACHAT_RAW_RPC_BLOCKLIST"))

# Cached OpenRPC spec (fetched lazily on first use).
_spec_cache: Optional[dict] = None
_token_lock = asyncio.Lock()
_spec_lock = asyncio.Lock()


async def _get_or_create_chat_token(rpc, account_id: int, chat_id: int) -> str:
    """Return a stable opaque token for *chat_id*.

    Checks memory cache first, then DC UI config (persists across restarts),
    creating and storing a new token if none exists yet.
    """
    async with _token_lock:
        if chat_id in _chat_id_to_token:
            return _chat_id_to_token[chat_id]

    dc_key = f"ui.hermes.chat_token.{chat_id}"
    try:
        existing = await rpc.get_config(account_id, dc_key)
    except Exception:
        existing = None

    if existing:
        token = existing
    else:
        token = secrets.token_hex(8)
        try:
            await rpc.set_config(account_id, dc_key, token)
            await rpc.set_config(
                account_id, f"ui.hermes.token_chat.{token}", str(chat_id)
            )
        except Exception as e:
            logger.warning("Could not persist chat token to DC config: %s", e)

    async with _token_lock:
        _chat_id_to_token[chat_id] = token
        _chat_token_to_id[token] = chat_id
    return token


async def _resolve_chat_token(rpc, account_id: int, token: str) -> Optional[int]:
    """Resolve an opaque token back to the real chat_id.

    Checks memory cache first, then DC UI config as a fallback for
    tokens issued in a previous session.
    """
    async with _token_lock:
        if token in _chat_token_to_id:
            return _chat_token_to_id[token]

    dc_key = f"ui.hermes.token_chat.{token}"
    try:
        chat_id_str = await rpc.get_config(account_id, dc_key)
    except Exception:
        chat_id_str = None

    if chat_id_str:
        chat_id = int(chat_id_str)
        async with _token_lock:
            _chat_token_to_id[token] = chat_id
            _chat_id_to_token[chat_id] = token
        return chat_id

    return None


async def _fetch_spec() -> dict:
    """Fetch and cache the OpenRPC spec from deltachat-rpc-server --openrpc."""
    global _spec_cache
    if _spec_cache is not None:
        return _spec_cache
    async with _spec_lock:
        if _spec_cache is not None:
            return _spec_cache
        rpc_server = (
            _active_adapter._get_rpc_server_path()
            if _active_adapter is not None
            else os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server")
        )
        proc = await asyncio.create_subprocess_exec(
            rpc_server,
            "--openrpc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"deltachat-rpc-server --openrpc failed: {stderr.decode().strip()}"
            )
        _spec_cache = json.loads(stdout.decode())
        return _spec_cache


class DeltaChatAdapter(BasePlatformAdapter):
    """Delta Chat platform adapter for Hermes Gateway.

    Uses deltachat2 for direct JSON-RPC access (not abstracted away).
    Each Hermes profile runs its own instance with its own DC_ACCOUNTS_PATH.
    """

    def __init__(self, config: PlatformConfig):
        """Initialize the adapter.

        Args:
            config: Hermes PlatformConfig for this profile
        """
        super().__init__(config, Platform("deltachat-platform"))
        self.rpc = None
        self._transport = None
        self.account_id: Optional[int] = None
        self._event_loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._dc_config_dir: Optional[str] = None
        self._call_manager = None

        extra = getattr(config, "extra", {}) or {}

        def g(env, key, default=""):
            val = os.getenv(env)
            if val:
                return val
            val = extra.get(key, default)
            if isinstance(val, bool):
                val = "true" if val else "false"
            return val

        allow_all = g(
            "DELTACHAT_ALLOW_ALL_USERS", "allow_all_users", "false"
        ).lower() in (
            "1",
            "true",
            "yes",
        )
        raw_allowed = g("DELTACHAT_ALLOWED_USERS", "allowed_users")
        self._allowed_users = (
            set()
            if allow_all
            else (_parse_email_list(raw_allowed) if raw_allowed else set())
        )

        self._dm_policy = g("DELTACHAT_DM_POLICY", "dm_policy", "pairing")
        raw_dm_allow = g("DELTACHAT_DM_ALLOWED_USERS", "dm_allowed_users")
        self._dm_allow_from = _parse_email_list(raw_dm_allow) if raw_dm_allow else set()

        self._group_policy = g("DELTACHAT_GROUP_POLICY", "group_policy", "open")
        raw_group_allow = g("DELTACHAT_GROUP_ALLOWED_USERS", "group_allowed_users")
        self._group_allow_from = (
            _parse_email_list(raw_group_allow) if raw_group_allow else set()
        )

        self._send_rejection_replies = g(
            "DELTACHAT_SEND_REJECTION_REPLIES", "send_rejection_replies", "true"
        ).lower() in ("1", "true", "yes")

        self._seen_ids = _MessageCache(max_size=1000)
        self._rate_limiter = _RateLimiter(
            max_calls=int(g("DELTACHAT_RATE_LIMIT_MAX", "rate_limit_max", "30")),
            window_seconds=float(
                g("DELTACHAT_RATE_LIMIT_WINDOW", "rate_limit_window", "60")
            ),
        )

        try:
            max_message_len = int(
                g(
                    "DELTACHAT_MAX_MESSAGE_LENGTH",
                    "max_message_length",
                    str(DC_MESSAGE_MAX_LEN),
                )
            )
        except ValueError:
            max_message_len = DC_MESSAGE_MAX_LEN
        if max_message_len < 100 or max_message_len > 10000:
            logger.warning(
                "DELTACHAT_MAX_MESSAGE_LENGTH %s out of bounds (100-10000), "
                "using default %s",
                max_message_len,
                DC_MESSAGE_MAX_LEN,
            )
            max_message_len = DC_MESSAGE_MAX_LEN
        self._max_message_len = max_message_len

        self._require_mention = g(
            "DELTACHAT_REQUIRE_MENTION", "require_mention", "false"
        ).lower() in ("1", "true", "yes")

        # Guards against bot-to-bot auto-reply loops (e.g. multiple agents in
        # one group replying to each other forever). <=0 disables the guard.
        self._max_consecutive_replies = int(
            g(
                "DELTACHAT_MAX_CONSECUTIVE_REPLIES",
                "max_consecutive_replies",
                "20",
            )
        )
        self._reply_streak: dict[str, tuple[str, int, bool]] = {}

        # Onboarding / profile settings
        self._email = g("DELTACHAT_EMAIL", "email", "auto").strip() or "auto"
        self._password = g("DELTACHAT_PASSWORD", "password") or None
        self._display_name = g("DELTACHAT_DISPLAY_NAME", "display_name", "Hermes")
        self._avatar_path = _validate_avatar_path(
            g("DELTACHAT_AVATAR_PATH", "avatar_path") or None, strict=False
        )
        self._data_dir = g("DELTACHAT_DATA_DIR", "data_dir") or None

        chatmail_servers = os.getenv("DELTACHAT_CHATMAIL_SERVERS") or extra.get(
            "chatmail_servers"
        )
        if not chatmail_servers:
            chatmail_servers = os.getenv(
                "DELTACHAT_CHATMAIL_SERVER", "nine.testrun.org"
            )
        self._chatmail_servers = _parse_chatmail_servers(chatmail_servers)

        # Runtime state for observability and crash recovery
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._crash_times: list[float] = []
        self._stats: dict[str, int] = {}
        self._lock = threading.RLock()
        self._self_addr: Optional[str] = None
        self._invite_link: Optional[str] = None

    def _bump_stat(self, key: str, count: int = 1) -> None:
        """Increment an internal counter under the adapter lock."""
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + count

    def _message_metadata(
        self,
        chat_id: Any,
        msg_id: Any,
        from_id: Any,
        is_group: bool,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build metadata dict for an incoming MessageEvent."""
        meta: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": str(msg_id),
            "is_group": is_group,
        }
        if from_id is not None:
            meta["from_id"] = str(from_id)
        if token:
            meta["dc_token"] = token
        return meta

    def _send_result(
        self,
        chat_id: str,
        msg_id: Optional[int],
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Build a SendResult, including metadata when supported."""
        if error is not None:
            try:
                return SendResult(success=False, error=error)
            except TypeError:
                return SendResult(success=False)

        meta: Dict[str, Any] = dict(metadata or {})
        meta.setdefault("chat_id", str(chat_id))
        if msg_id is not None:
            meta.setdefault("message_id", str(msg_id))
        try:
            token = _chat_id_to_token.get(int(chat_id))
            if token:
                meta.setdefault("dc_token", token)
        except (ValueError, TypeError):
            pass

        try:
            return SendResult(
                success=True, message_id=str(msg_id) if msg_id else None, metadata=meta
            )
        except TypeError:
            return SendResult(success=True, message_id=str(msg_id) if msg_id else None)

    def _check_dm(self, sender_email: str, is_verified: bool) -> Optional[str]:
        if self._dm_policy == "disabled":
            return "Sorry, this bot does not accept direct messages."
        if self._dm_policy == "pairing" and not is_verified:
            return "I only chat with verified contacts. Scan my QR code to connect securely."
        if (
            self._dm_policy == "allowlist"
            and self._dm_allow_from
            and sender_email not in self._dm_allow_from
        ):
            return "Sorry, you are not on the allowed list for direct messages."
        return None

    def _check_group(self, sender_email: str) -> Optional[str]:
        if self._group_policy == "disabled":
            return "Sorry, this bot does not respond in group chats."
        if (
            self._group_policy == "allowlist"
            and self._group_allow_from
            and sender_email not in self._group_allow_from
        ):
            return "Sorry, you are not authorized for group interactions."
        return None

    def _is_mentioned(self, text: str) -> bool:
        """Return True if the message text mentions the bot by display name.

        Matches whole-word `@DisplayName` or `DisplayName`, case-insensitive.
        Substrings like `Hermesss` do not count.
        """
        if not text or not self._display_name:
            return False
        name = re.escape(self._display_name)
        pattern = rf"(?:^|\W)@{name}(?:\W|$)|(?:^|\W){name}(?:\W|$)"
        return re.search(pattern, text, re.IGNORECASE) is not None

    async def _check_mention(self, text: str, chat_type: str, chat_id: str) -> bool:
        """Drop group messages that do not mention the bot when required.

        Returns True if the message should be processed.
        """
        if chat_type != "group" or not self._require_mention:
            return True
        if not text or text.startswith("/"):
            return True
        if self._is_mentioned(text):
            return True

        logger.debug("Ignoring unmentioned group message in chat %s", chat_id)
        self._bump_stat("messages_rejected")
        if self._send_rejection_replies:
            await self.send(
                str(chat_id),
                f"Please mention me (@{self._display_name}) to talk in this group.",
            )
        return False

    def _check_loop_guard(self, chat_id, from_id) -> tuple[bool, bool]:
        """Cap consecutive auto-replies to the same sender in one chat.

        Two (or more) agents in a shared group can end up replying to each
        other forever. If the same from_id sends more than
        DELTACHAT_MAX_CONSECUTIVE_REPLIES messages in a row in a chat with no
        other participant chiming in between, stop processing further
        messages from them until someone else speaks.

        Returns (should_process, should_warn) — should_warn is True only the
        first time a given streak trips, so we don't send a notice per message.
        """
        if self._max_consecutive_replies <= 0 or not from_id:
            return True, False
        key = str(chat_id)
        sender = str(from_id)
        with self._lock:
            last_sender, count, warned = self._reply_streak.get(key, (None, 0, False))
            if last_sender == sender:
                count += 1
            else:
                count = 1
                warned = False
            tripped = count > self._max_consecutive_replies
            should_warn = tripped and not warned
            self._reply_streak[key] = (sender, count, warned or should_warn)
        return not tripped, should_warn

    async def _gate_inbound(self, chat_id, msg_id, from_id) -> bool:
        """Run dedup, rate-limit, and DM/group policy checks for an inbound message.

        Returns True if the message should be processed further, False if it
        was dropped or rejected (rejection reply already sent if configured).
        """
        if not self._seen_ids.add(str(msg_id)):
            logger.debug("Ignoring duplicate message %s", msg_id)
            self._bump_stat("duplicate_messages_dropped")
            return False

        sender_email = ""
        is_verified = False
        if from_id:
            try:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
                sender_email = (contact.get("address") or "").lower()
                is_verified = bool(contact.get("is_verified"))
            except Exception as e:
                logger.debug("Could not fetch contact %s: %s", from_id, e)

        if sender_email and not self._rate_limiter.is_allowed(sender_email):
            logger.warning("Rate limit exceeded for %s", sender_email)
            self._bump_stat("messages_rate_limited")
            return False

        if self._allowed_users and sender_email not in self._allowed_users:
            logger.warning("Rejected %s (not in allowed_users)", sender_email)
            self._bump_stat("messages_rejected")
            if self._send_rejection_replies:
                await self.send(
                    str(chat_id), "Sorry, you are not authorized to use this bot."
                )
            return False

        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
        except Exception as e:
            logger.warning("Could not fetch chat %s: %s", chat_id, e)
            return False
        chat_type = chat.get("chat_type")
        is_request = bool(chat.get("is_contact_request"))

        if chat_type == "Single":
            reason = self._check_dm(sender_email, is_verified)
            if reason:
                logger.warning("dm_policy rejected %s", sender_email)
                self._bump_stat("messages_rejected")
                if self._send_rejection_replies:
                    await self.send(str(chat_id), reason)
                return False
            if is_request:
                try:
                    await self.rpc.accept_chat(self.account_id, int(chat_id))
                except Exception as e:
                    logger.warning("accept_chat failed: %s", e)

        elif chat_type == "Group":
            reason = self._check_group(sender_email)
            if reason:
                logger.warning("group_policy rejected %s", sender_email)
                self._bump_stat("messages_rejected")
                if is_request:
                    try:
                        await self.rpc.leave_group(self.account_id, int(chat_id))
                    except Exception as e:
                        logger.warning("leave_group failed: %s", e)
                elif self._send_rejection_replies:
                    await self.send(str(chat_id), reason)
                return False
            if is_request:
                try:
                    await self.rpc.accept_chat(self.account_id, int(chat_id))
                except Exception as e:
                    logger.warning("accept_chat failed: %s", e)

        self._bump_stat("messages_received")
        return True

    def _get_dc_config_dir(self) -> str:
        """Get Delta Chat config directory path.

        Uses DELTACHAT_DATA_DIR if set, otherwise falls back to
        <HERMES_HOME>/deltachat-platform/ for backward compatibility.
        The directory is created with restrictive permissions when first accessed.
        """
        if self._dc_config_dir is None:
            if self._data_dir:
                path = self._data_dir
            else:
                from gateway.config import get_hermes_home

                path = os.path.join(get_hermes_home(), "deltachat-platform")
            # Validate/create the directory, but keep the original (unresolved) path
            # so that existing tests and relative-path configs stay stable.
            expanded = os.path.expanduser(path)
            _safe_data_dir(expanded, create=True)
            self._dc_config_dir = expanded
        return self._dc_config_dir

    def _get_rpc_server_path(self) -> str:
        """Get deltachat-rpc-server binary path.

        Returns:
            Path to RPC server binary from config, env, or default.
        """
        # From config.extra
        if self.config.extra and self.config.extra.get("rpc_server"):
            return self.config.extra["rpc_server"]

        # From environment
        env_path = os.getenv("DELTACHAT_RPC_SERVER")
        if env_path:
            return env_path

        # Default - assume in PATH
        return "deltachat-rpc-server"

    async def _apply_profile(self, rpc, account_id: int) -> None:
        """Apply display name, avatar, and bot mode to the account.

        Failures are logged but do not abort the connection.
        """
        try:
            await rpc.set_config(account_id, "displayname", self._display_name)
            logger.debug("Set display name to %r", self._display_name)
        except Exception as e:
            logger.warning("Could not set display name: %s", e)

        try:
            await rpc.set_config(account_id, "bot", "1")
            logger.debug("Bot mode enabled")
        except Exception as e:
            logger.warning("Could not enable bot mode: %s", e)

        if self._avatar_path:
            try:
                resolved = _validate_avatar_path(self._avatar_path, strict=True)
                await rpc.set_config(account_id, "selfavatar", resolved)
                logger.debug("Set avatar to %r", resolved)
            except Exception as e:
                logger.warning("Could not set avatar: %s", e)

    async def _configure_account(self, rpc) -> bool:
        """Select an existing account or create and configure a new one."""
        accounts = await rpc.get_all_accounts()
        if accounts:
            self.account_id = accounts[0]["id"]
            logger.info("Using existing Delta Chat account: %s", self.account_id)
            await self._apply_profile(rpc, self.account_id)
            # Existing accounts do not need the configured password.
            self._password = None
            return True

        logger.info("No Delta Chat account found; creating one")
        account_id = await rpc.add_account()
        if isinstance(account_id, dict):
            account_id = account_id.get("id", account_id.get("account_id"))
        if not isinstance(account_id, int):
            raise RuntimeError(f"add_account returned unexpected value: {account_id!r}")
        self.account_id = account_id
        logger.info("Created Delta Chat account: %s", self.account_id)

        await self._apply_profile(rpc, self.account_id)

        try:
            if self._email and self._email != "auto" and self._password:
                logger.info("Configuring account with email %s", self._email)
                await rpc.add_or_update_transport(
                    self.account_id,
                    {"addr": self._email, "password": self._password},
                )
                await rpc.configure(self.account_id)
            else:
                await self._create_chatmail_account(rpc)
            return True
        finally:
            # Password is no longer needed after configuration; clear it from memory.
            self._password = None

    async def _create_chatmail_account(self, rpc) -> None:
        """Create a chatmail account by trying configured servers in order."""
        last_error: Optional[Exception] = None
        servers = self._chatmail_servers or ["nine.testrun.org"]
        for server in servers:
            logger.info("Trying chatmail server %s", server)
            try:
                await rpc.set_config_from_qr(
                    self.account_id, f"DCACCOUNT:https://{server}/new"
                )
                await rpc.configure(self.account_id)
                addr = await rpc.get_config(self.account_id, "addr")
                logger.info("Chatmail account ready: %s", addr)
                return
            except Exception as e:
                last_error = e
                logger.warning("Chatmail server %s failed: %s", server, e)
        raise last_error or RuntimeError("All configured chatmail servers failed")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Delta Chat via RPC server.

        Starts the RPC server process, initializes the client,
        checks version, and begins listening for events.

        Returns:
            True if connection successful, False otherwise
        """
        self._loop = asyncio.get_running_loop()

        if not _check_dc2_available():
            logger.error("deltachat2 is not installed. Run: pip install deltachat2")
            return False

        try:
            import deltachat2
        except ImportError as e:
            logger.error(f"Failed to import deltachat2: {e}")
            return False

        try:
            # Get config directory
            dc_accounts_path = self._get_dc_config_dir()
            logger.debug(f"Using DC accounts directory: {dc_accounts_path}")

            # Get RPC server path
            rpc_server_path = self._get_rpc_server_path()
            logger.debug(f"Using RPC server: {rpc_server_path}")

            # Initialize RPC client with deltachat2, passing accounts_dir to transport
            from deltachat2.transport import IOTransport

            os.environ["DC_ACCOUNTS_PATH"] = dc_accounts_path
            self._transport = IOTransport(
                accounts_dir=dc_accounts_path, rpc_server=rpc_server_path
            )
            self._transport.start()
            self.rpc = _AsyncRpc(deltachat2.Rpc(self._transport))

            # Wait for RPC server to be ready
            await asyncio.sleep(1)

            # Check version - REJECT if too old
            if not await _check_dc_version(self.rpc):
                self._cleanup()
                return False

            # Select existing account or create/configure a new one
            if not await self._configure_account(self.rpc):
                self._cleanup()
                return False

            # Start IO for the account to receive events
            await self.rpc.start_io(self.account_id)
            logger.debug("Started IO for account %s", self.account_id)

            # Generate a SecureJoin invite link now that IO is running.
            try:
                link, _svg = await self.rpc.get_chat_securejoin_qr_code_svg(
                    self.account_id, None
                )
                self._invite_link = link
                logger.debug("SecureJoin invite link: %s", link)
            except Exception as e:
                logger.warning("Could not generate SecureJoin invite link: %s", e)
                self._invite_link = None

            # Register graceful shutdown signals.
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    self._loop.add_signal_handler(sig, self._signal_handler)
            except (NotImplementedError, ValueError, RuntimeError):
                pass  # Signals may not be supported on this platform.

            # Start event listener with crash recovery.
            self._running = True
            self._event_loop_task = asyncio.create_task(self._event_supervisor())

            self._mark_connected()
            global _active_adapter
            _active_adapter = self

            from call_handler import CallManager

            self._call_manager = CallManager(self)

            # Log the bot's address for reference and cache it for status.
            self._self_addr = await self.get_my_address()
            if self._self_addr:
                logger.info(
                    f"Delta Chat connected successfully. Bot address: {self._self_addr}"
                )
            else:
                logger.info("Delta Chat connected successfully")
            return True

        except Exception as e:
            logger.error(f"Delta Chat connection failed: {e}")
            self._cleanup()
            return False

    def _cleanup(self) -> None:
        """Clean up resources."""
        global _active_adapter
        if _active_adapter is self:
            _active_adapter = None
        self._running = False
        if self._event_loop_task:
            self._event_loop_task.cancel()
            self._event_loop_task = None
        if self._transport:
            try:
                self._transport.close()
            except Exception as e:
                logger.warning(f"Error closing transport: {e}")
            self._transport = None
        self.rpc = None
        self.account_id = None
        self._self_addr = None
        self._invite_link = None
        self._password = None

    def _signal_handler(self):
        """Handle SIGTERM/SIGINT by scheduling disconnect on the event loop."""
        logger.info("DeltaChat: received shutdown signal")
        if self._loop and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.disconnect(), self._loop)
            except Exception as e:
                logger.warning("DeltaChat: could not schedule disconnect: %s", e)

    async def disconnect(self) -> None:
        """Disconnect from Delta Chat."""
        # Remove signal handlers.
        if self._loop and not self._loop.is_closed():
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    self._loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError, RuntimeError):
                pass

        if self._call_manager:
            await self._call_manager.teardown()
            self._call_manager = None
        self._cleanup()
        self._mark_disconnected()
        logger.info("Delta Chat disconnected")

    def get_status(self) -> dict:
        """Return a snapshot of adapter health and metrics."""
        with self._lock:
            running = self._running
            thread_alive = (
                self._event_loop_task is not None and not self._event_loop_task.done()
            )
            crashes = list(self._crash_times)
            stats = dict(self._stats)
        return {
            "connected": self.rpc is not None and thread_alive,
            "running": running,
            "account_addr": self._self_addr,
            "invite_link": self._invite_link,
            "crashes_last_60s": len(crashes),
            "last_crash": crashes[-1] if crashes else None,
            "stats": stats,
        }

    async def get_my_address(self) -> Optional[str]:
        """Get the Delta Chat account address or SecureJoin link.

        Returns:
            SecureJoin link (e.g., https://delta.chat/s?pk=...) or address (e.g., bot@server.org)
        """
        if not self.rpc or not self.account_id:
            return None

        try:
            # Try to get SecureJoin QR code content (which is the link)
            try:
                qr_content = await self.rpc.get_chat_securejoin_qr_code(
                    self.account_id, None  # chat_id - None for account-level QR
                )
                if qr_content:
                    return qr_content
            except Exception:
                pass

            # Fallback: get account info which should include address
            info = await self.rpc.get_account_info(self.account_id)
            if info:
                # Try different field names for address
                address = info.get("address") or info.get("addr")
                if address:
                    return address
                # Construct from name and server
                name = info.get("name") or info.get("display_name", "")
                server = info.get("server", "")
                if name and server:
                    return f"{name}@{server}"

            # Final fallback: list accounts and find ours
            accounts = await self.rpc.get_all_accounts()
            for acc in accounts:
                if acc.get("id") == self.account_id:
                    name = acc.get("name", acc.get("display_name", ""))
                    server = acc.get("server", "")
                    if name and server:
                        return f"{name}@{server}"
        except Exception as e:
            logger.debug(f"Failed to get account address: {e}")

        return None

    def _format_html_message(self, text: str, max_lines: int = 40) -> tuple:
        """Format long messages with HTML for better readability in Delta Chat.

        If message is longer than max_lines, returns (text_part, html_part)
        where text_part is the first max_lines and html_part is the full
        message with proper styling. Otherwise returns (text, None).

        Args:
            text: The message text
            max_lines: Maximum lines before using HTML (default: 40)

        Returns:
            Tuple of (plain_text, html_text) - html_text is None if not needed
        """
        lines = text.split("\n")
        if len(lines) <= max_lines:
            return (text, None)

        # First max_lines as plain text
        text_part = "\n".join(lines[:max_lines])

        # Full message as HTML with nice formatting; escape to prevent injection
        escaped = html.escape(text).replace("\n", "<br>\n")
        html_part = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 16px;
    line-height: 1.5;
    color: #333;
    background-color: #fff;
    padding: 16px;
    max-width: 800px;
    margin: 0 auto;
}}
</style>
</head>
<body>
{escaped}
</body>
</html>"""

        return (text_part, html_part)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a Delta Chat chat.

        When a voice call is active for this chat the response is routed to
        TTS and played into the call instead of being sent as a DC message.
        """
        if self._call_manager and self._call_manager.has_active_call(chat_id):
            thread_id = (metadata or {}).get("thread_id")
            if self._call_manager.is_call_thread(thread_id):
                # Reply belongs to the call conversation — speak it into the call.
                # In shared-history mode the placing agent's "call connected" ack
                # also lands here (same session), so drop that one line.
                if self._call_manager.consume_call_ack(chat_id):
                    return self._send_result(chat_id, None)
                asyncio.create_task(self._call_manager.play_response(chat_id, content))
                return self._send_result(chat_id, None)
            # Reply from the text/chat thread while a call is active (e.g. the
            # agent's "calling you now" line in separate-thread mode, or a
            # concurrent DM) — deliver it as a normal Delta Chat message instead
            # of speaking it into the call. Falls through to the normal send path.
        # Suppress the AI's reply to the internal "call ended" note so we don't
        # text the user a stray message after a call.
        if self._call_manager and self._call_manager.consume_drop_response(chat_id):
            return self._send_result(chat_id, None)

        try:
            if not self.rpc or not self.account_id:
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            # Delta Chat renders plain text only; strip common markdown syntax.
            stripped = _strip_markdown(content)

            quoted_id = int(reply_to) if reply_to else None

            async def _do_send() -> Optional[int]:
                from deltachat2.types import MsgData

                # Very long messages are split at natural boundaries and sent
                # as multiple plain-text chunks.
                chunks = _split_message(stripped, self._max_message_len)
                if len(chunks) > 1:
                    last_msg_id: Optional[int] = None
                    for idx, chunk in enumerate(chunks):
                        # Only quote-reply the first chunk.
                        chunk_quoted = quoted_id if idx == 0 else None
                        last_msg_id = await self.rpc.send_msg(
                            self.account_id,
                            int(chat_id),
                            MsgData(text=chunk, quoted_message_id=chunk_quoted),
                        )
                    return last_msg_id

                # Shorter messages may use HTML formatting when >40 lines.
                text_part, html_part = self._format_html_message(stripped)
                from deltachat2.types import MessageViewtype

                if html_part:
                    msg_id = await self.rpc.send_msg(
                        self.account_id,
                        int(chat_id),
                        MsgData(
                            text=text_part,
                            html=html_part,
                            viewtype=MessageViewtype.TEXT,
                            quoted_message_id=quoted_id,
                        ),
                    )
                else:
                    msg_id = await self.rpc.send_msg(
                        self.account_id,
                        int(chat_id),
                        MsgData(text=stripped, quoted_message_id=quoted_id),
                    )
                return msg_id

            msg_id = await _async_retry(_do_send, max_attempts=3, base_delay=1.0)
            logger.debug("Sent message %s to chat %s", msg_id, chat_id)
            self._bump_stat("messages_sent")
            return self._send_result(chat_id, msg_id)

        except Exception as e:
            logger.error("Error sending message to chat %s: %s", chat_id, e)
            self._bump_stat("messages_send_failed")
            return self._send_result(chat_id, None, error=str(e))

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file to a Delta Chat chat via send_msg.

        DC core auto-detects the viewtype from the extension — .xdc files
        are delivered as webxdc apps without any special handling here.
        """
        try:
            if not self.rpc or not self.account_id:
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            from deltachat2.types import MsgData

            async def _do_send():
                return await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(
                        file=file_path,
                        text=caption or "",
                        quoted_message_id=int(reply_to) if reply_to else None,
                    ),
                )

            msg_id = await _async_retry(_do_send, max_attempts=2, base_delay=0.5)
            logger.debug(
                "Sent file %s as message %s to chat %s", file_path, msg_id, chat_id
            )
            self._bump_stat("files_sent")
            return self._send_result(chat_id, msg_id)

        except Exception as e:
            logger.error("Error sending file %s to chat %s: %s", file_path, chat_id, e)
            self._bump_stat("files_send_failed")
            return self._send_result(chat_id, None, error=str(e))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment to a Delta Chat chat.

        Delegates to send_file; file_name is ignored because DC derives the
        display name from the blob path.  DC core auto-detects viewtype from
        the file extension (.xdc → webxdc, .pdf → document, etc.).
        """
        return await self.send_file(
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def _download_image_url(self, url: str) -> str:
        """Download an image URL to a temporary file and return the path.

        Validates scheme, Content-Type, and size (25 MiB max).
        The caller is responsible for deleting the returned temp file.
        """
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("httpx is required to download image URLs") from e

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid image URL: {url}")

        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            headers = {"Accept": "image/*"}
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    raise ValueError(
                        f"URL did not return an image (Content-Type: {content_type})"
                    )
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > _MAX_IMAGE_SIZE:
                    raise ValueError("Image exceeds 25 MiB limit")

                suffix = os.path.splitext(parsed.path)[1]
                if not suffix:
                    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
                    suffix = ext or ".bin"
                fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)

                downloaded = 0
                try:
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=8192):
                            downloaded += len(chunk)
                            if downloaded > _MAX_IMAGE_SIZE:
                                raise ValueError("Image exceeds 25 MiB limit")
                            f.write(chunk)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
        return tmp_path

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image file or image URL to a Delta Chat chat.

        Args:
            chat_id: Delta Chat chat ID
            image_path: Path to image file on disk, or an http(s) URL
            caption: Optional caption for the image
            reply_to: Optional message ID to reply to
            metadata: Optional metadata

        Returns:
            SendResult with success status and message ID
        """
        tmp_path: Optional[str] = None
        try:
            if not self.rpc or not self.account_id:
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            if image_path.startswith(("http://", "https://")):
                tmp_path = await self._download_image_url(image_path)
                file_path = tmp_path
            else:
                file_path = image_path

            from deltachat2.types import MsgData, MessageViewtype

            async def _do_send():
                return await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(
                        file=file_path,
                        text=caption or "",
                        viewtype=MessageViewtype.IMAGE,
                        quoted_message_id=int(reply_to) if reply_to else None,
                    ),
                )

            msg_id = await _async_retry(_do_send, max_attempts=2, base_delay=0.5)
            logger.debug(
                "Sent image %s as message %s to chat %s", image_path, msg_id, chat_id
            )
            self._bump_stat("images_sent")
            return self._send_result(chat_id, msg_id)
        except Exception as e:
            logger.error(
                "Error sending image %s to chat %s: %s", image_path, chat_id, e
            )
            self._bump_stat("images_send_failed")
            return self._send_result(chat_id, None, error=str(e))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice message to a Delta Chat chat.

        Delta Chat supports voice messages natively.

        Args:
            chat_id: Delta Chat chat ID
            audio_path: Path to audio file on disk
            caption: Optional caption for the voice message
            reply_to: Optional message ID to reply to
            metadata: Optional metadata

        Returns:
            SendResult with success status and message ID
        """
        import os

        logger.info(
            f"send_voice called: chat_id={chat_id}, audio_path={audio_path}, "
            f"caption={caption[:50] if caption else None}"
        )
        logger.debug(f"send_voice kwargs: {kwargs}")

        # Validate audio file exists and is accessible
        if not os.path.exists(audio_path):
            logger.error("send_voice: Audio file does not exist: %s", audio_path)
            return self._send_result(
                chat_id, None, error=f"Audio file not found: {audio_path}"
            )
        if not os.path.isfile(audio_path):
            logger.error("send_voice: Path is not a file: %s", audio_path)
            return self._send_result(
                chat_id, None, error=f"Path is not a file: {audio_path}"
            )
        file_size = os.path.getsize(audio_path)
        logger.info(f"send_voice: Audio file exists, size={file_size} bytes")

        # Delta Chat sends voice messages as files with VOICE viewtype
        from deltachat2.types import MsgData, MessageViewtype

        try:
            if not self.rpc or not self.account_id:
                logger.error(
                    "send_voice: Delta Chat not connected (rpc={}, account_id={})".format(
                        "None" if not self.rpc else "set",
                        "None" if not self.account_id else self.account_id,
                    )
                )
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            logger.debug(
                "send_voice: Sending to account_id=%s, chat_id=%s",
                self.account_id,
                chat_id,
            )

            async def _do_send():
                return await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(
                        file=audio_path,
                        text=caption or "",
                        viewtype=MessageViewtype.VOICE,
                    ),
                )

            msg_id = await _async_retry(_do_send, max_attempts=2, base_delay=0.5)
            logger.info(
                "Sent voice message %s to chat %s, file=%s, size=%s",
                msg_id,
                chat_id,
                audio_path,
                file_size,
            )
            self._bump_stat("voices_sent")
            return self._send_result(chat_id, msg_id)

        except Exception as e:
            import traceback

            logger.error("Error in send_voice: %s", e)
            logger.debug("send_voice exception traceback:\n%s", traceback.format_exc())
            self._bump_stat("voices_send_failed")
            return self._send_result(chat_id, None, error=str(e))

    async def send_location(
        self,
        chat_id: str,
        latitude: float,
        longitude: float,
        poi_name: str,
    ) -> SendResult:
        """Send a location/point of interest to a Delta Chat chat.

        Note: In Delta Chat, a single emoji character is displayed as that emoji
        on the map. A text message is displayed as a pin icon that can be clicked
        to view the message.

        Args:
            chat_id: Delta Chat chat ID
            latitude: Latitude in degrees
            longitude: Longitude in degrees
            poi_name: POI name or emoji (e.g., "☕" for coffee, "🏠" for home,
                     or "My favorite café" for a pin with text)

        Returns:
            SendResult with success status and message ID
        """
        try:
            if not self.rpc or not self.account_id:
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            from deltachat2.types import MsgData

            async def _do_send():
                # location tuple is (latitude, longitude) per GeoJSON convention
                return await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(text=poi_name, location=(latitude, longitude)),
                )

            msg_id = await _async_retry(_do_send, max_attempts=2, base_delay=0.5)
            logger.debug("Sent location to chat %s", chat_id)
            self._bump_stat("locations_sent")
            return self._send_result(chat_id, msg_id)

        except Exception as e:
            logger.error("Error sending location to chat %s: %s", chat_id, e)
            self._bump_stat("locations_send_failed")
            return self._send_result(chat_id, None, error=str(e))

    # ------------------------------------------------------------------
    # Container-to-host file path mapping
    # ------------------------------------------------------------------
    # The Docker LLM sandbox mounts /workspace inside the container to
    #   ~/.hermes/sandboxes/docker/default/workspace/   on the host.
    # When the agent writes output files to /workspace/ and emits MEDIA
    # directives or bare paths, Hermes's path validator runs on the HOST
    # and can't find container-local paths.  These overrides remap any
    # /workspace/<rel> path to the host sandbox path, copy the file to
    # the Hermes documents cache (a validated safe root), and return the
    # cache path so the base-class validator accepts it.
    #
    # The same pattern works for any output file type (.pdf, .html, .zip,
    # .xdc, etc.) — just write to /workspace/ in the container.
    # ------------------------------------------------------------------

    @staticmethod
    def _container_workspace_to_host(container_path: str) -> Optional[str]:
        """Map a /workspace/<rel> container path to its host-side sandbox path.

        Returns None when the path is not under /workspace/ or when it tries
        to escape the sandbox (e.g. via .. or symlinks).
        """
        from pathlib import Path

        p = str(container_path)
        if not p.startswith("/workspace/"):
            return None
        rel = p[len("/workspace/") :]
        if ".." in Path(rel).parts:
            logger.warning("Rejecting workspace path with '..': %s", container_path)
            return None
        try:
            from tools.environments.base import get_sandbox_dir

            sandbox_workspace = get_sandbox_dir() / "docker" / "default" / "workspace"
        except ImportError:
            from gateway.config import get_hermes_home

            sandbox_workspace = (
                Path(get_hermes_home())
                / "sandboxes"
                / "docker"
                / "default"
                / "workspace"
            )
        target = sandbox_workspace / rel
        try:
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError):
            logger.warning("Could not resolve workspace path: %s", container_path)
            return None
        try:
            if not resolved.is_relative_to(sandbox_workspace):
                logger.warning(
                    "Workspace path escapes sandbox: %s -> %s", container_path, resolved
                )
                return None
        except (OSError, ValueError):
            logger.warning("Could not verify workspace path: %s", container_path)
            return None
        return str(resolved)

    def _copy_container_file_to_cache(self, container_path: str) -> Optional[str]:
        """Copy a /workspace/ container file to the Hermes docs cache.

        Returns the cache path on success, None if the file doesn't exist.
        Same pattern as _copy_to_hermes_cache for DC audio blobs.
        """
        import shutil
        from pathlib import Path
        from gateway.config import get_hermes_home

        host_path_str = self._container_workspace_to_host(container_path)
        if host_path_str is None:
            return None

        host_path = Path(host_path_str)
        if host_path.is_symlink():
            logger.warning("Rejecting symlinked container output file: %s", host_path)
            return None
        if not host_path.is_file():
            logger.warning("Container output file not found on host: %s", host_path)
            return None

        docs_dir = Path(get_hermes_home()) / "cache" / "documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        dest = docs_dir / host_path.name
        shutil.copy2(str(host_path), str(dest))
        logger.info("Copied container output %s → %s", host_path.name, dest)
        return str(dest)

    def extract_media(self, content: str):
        """Extend base extract_media to also handle .xdc MEDIA tags.

        .xdc is not in Hermes's MEDIA_DELIVERY_EXTS so the base staticmethod
        misses it.  We catch those tags here so they flow through the normal
        filter_media_delivery_paths → send_document pipeline, exactly like
        Telegram handles any other document type.
        """
        import re
        from gateway.platforms.base import BasePlatformAdapter

        media_files, remaining = BasePlatformAdapter.extract_media(content)

        xdc_re = re.compile(
            r'[`"\']?MEDIA:\s*[`"\']?((?:~/|/)[\w./\- ]+\.xdc)[`"\']?',
            re.IGNORECASE,
        )
        for match in xdc_re.finditer(content):
            path = match.group(1).strip()
            if not any(p == path for p, _ in media_files):
                media_files.append((path, False))
            remaining = remaining.replace(match.group(0), "").strip()

        return media_files, remaining

    def extract_local_files(self, content: str):
        """Extend base to also pick up bare /workspace/*.xdc paths.

        The base staticmethod checks os.path.isfile() on the host — container
        paths like /workspace/app.xdc don't exist there, so we add them
        explicitly.  filter_local_delivery_paths then does the host mapping.
        """
        import re
        from gateway.platforms.base import BasePlatformAdapter

        files, remaining = BasePlatformAdapter.extract_local_files(content)

        xdc_re = re.compile(r"(?<![/:\w.])(/workspace/[\w./\-]+\.xdc)\b", re.IGNORECASE)
        for match in xdc_re.finditer(content):
            path = match.group(1)
            if path not in files:
                files.append(path)
                remaining = remaining.replace(match.group(0), "").strip()

        return files, remaining

    def filter_media_delivery_paths(self, media_files):
        """Remap /workspace/ container paths to host cache before validation."""
        from gateway.platforms.base import BasePlatformAdapter

        remapped = []
        for media_path, is_voice in media_files or []:
            p = str(media_path)
            if p.startswith("/workspace/"):
                cached = self._copy_container_file_to_cache(p)
                if cached:
                    remapped.append((cached, is_voice))
                    continue
                logger.warning("Could not resolve container path for delivery: %s", p)
            remapped.append((media_path, is_voice))
        return BasePlatformAdapter.filter_media_delivery_paths(remapped)

    def filter_local_delivery_paths(self, file_paths):
        """Remap /workspace/ container paths to host cache before validation."""
        from gateway.platforms.base import BasePlatformAdapter

        remapped = []
        for file_path in file_paths or []:
            p = str(file_path)
            if p.startswith("/workspace/"):
                cached = self._copy_container_file_to_cache(p)
                if cached:
                    remapped.append(cached)
                    continue
                logger.warning("Could not resolve container path for delivery: %s", p)
            else:
                remapped.append(file_path)
        return BasePlatformAdapter.filter_local_delivery_paths(remapped)

    async def _event_listener(self) -> None:
        """Listen for Delta Chat events and forward to Hermes."""
        while self._running:
            try:
                if self.account_id:
                    envelope = await self.rpc.get_next_event()
                    if envelope.get("context_id") == self.account_id:
                        await self._handle_dc_event(envelope.get("event", {}))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Event listener error: {e}")
                await asyncio.sleep(1)

    async def _event_supervisor(self) -> None:
        """Run the event listener and restart it on crash.

        If the listener crashes 3 times within 60 seconds, the adapter gives up
        and disconnects.
        """
        while self._running:
            try:
                await self._event_listener()
                break  # Clean exit
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                now = time.monotonic()
                with self._lock:
                    self._crash_times = [t for t in self._crash_times if now - t < 60]
                    self._crash_times.append(now)
                    recent_crashes = len(self._crash_times)
                self._bump_stat("event_listener_crashes")
                if recent_crashes >= 3:
                    logger.error(
                        "DeltaChat: 3 event listener crashes in 60s — disabling"
                    )
                    self._running = False
                    asyncio.create_task(self.disconnect())
                    break
                logger.error(
                    "DeltaChat: event listener crashed (%s), restarting in 5s", e
                )
                await asyncio.sleep(5)

    async def _handle_dc_event(self, event: Dict[str, Any]) -> None:
        """Handle a Delta Chat event and convert to Hermes MessageEvent.

        Args:
            event: Delta Chat event dictionary
        """
        from deltachat2.types import EventType

        event_kind = event.get("kind")

        if event_kind == EventType.INCOMING_MSG:
            await self._handle_incoming_message(event)
        elif event_kind == EventType.MSG_DELIVERED:
            logger.debug(f"Message delivered: {event.get('msg_id')}")
        elif event_kind == EventType.MSG_FAILED:
            logger.warning(f"Message failed: {event.get('msg_id')}")
        elif event_kind == EventType.INCOMING_CALL:
            if self._call_manager:
                asyncio.create_task(self._call_manager.handle_incoming_call(event))
        elif event_kind == EventType.CALL_ENDED:
            if self._call_manager:
                asyncio.create_task(self._call_manager.handle_call_ended(event))
        elif event_kind == EventType.OUTGOING_CALL_ACCEPTED:
            if self._call_manager:
                asyncio.create_task(
                    self._call_manager.handle_outgoing_call_accepted(event)
                )
        elif event_kind == EventType.INCOMING_CALL_ACCEPTED:
            logger.info("Incoming call accepted msg_id=%s", event.get("msg_id"))
        else:
            logger.debug(f"Unhandled event type: {event_kind}")

    async def _handle_incoming_message(self, event: Dict[str, Any]) -> None:
        """Handle an incoming text message.

        Args:
            event: Delta Chat INCOMING_MSG event
        """
        try:
            chat_id = event.get("chat_id")
            msg_id = event.get("msg_id")

            if not chat_id or not msg_id:
                logger.warning(f"Invalid message event: {event}")
                return

            # Get message details via direct RPC
            msg = await self.rpc.get_message(
                self.account_id,
                int(msg_id),
            )
            if not msg:
                logger.warning(f"Could not retrieve message {msg_id}")
                return

            # Send read receipt immediately
            try:
                await self.rpc.markseen_msgs(self.account_id, [int(msg_id)])
            except Exception as e:
                logger.debug(f"Could not mark message {msg_id} as seen: {e}")

            if not await self._gate_inbound(chat_id, msg_id, msg.get("from_id")):
                return

            text = msg.get("text", "")
            view_type = msg.get("view_type", "")
            has_file = bool(msg.get("file") or msg.get("file_mime"))
            # Route to non-text handler when viewtype is non-text OR when the
            # message has a file attachment even if DC reported viewType=Text
            # (happens for image+caption combos or pending downloads).
            if not text or view_type not in ("Text", "", None) or has_file:
                logger.info(
                    "Non-text message: view_type=%r text=%r file=%r file_mime=%r msg_id=%s",
                    view_type,
                    text[:80] if text else text,
                    msg.get("file"),
                    msg.get("file_mime"),
                    msg_id,
                )
                await self._handle_non_text_message(msg, chat_id, msg_id)
                return

            # Get chat info
            chat = await self.rpc.get_basic_chat_info(
                self.account_id,
                int(chat_id),
            )

            # Get sender info
            from_id = msg.get("from_id")
            if from_id:
                contact = await self.rpc.get_contact(
                    self.account_id,
                    int(from_id),
                )
                user_name = (
                    contact.get("name")
                    or contact.get("display_name")
                    or contact.get("name_and_addr")
                    or f"Contact {from_id}"
                )
                user_id = str(from_id)
            else:
                user_name = "Unknown"
                user_id = "unknown"

            # Determine chat type
            chat_type = "group" if chat.get("chat_type") == "Group" else "dm"
            chat_name = chat.get("name", f"Chat {chat_id}")

            should_process, should_warn = self._check_loop_guard(chat_id, from_id)
            if not should_process:
                self._bump_stat("loop_guard_tripped")
                if should_warn and self._send_rejection_replies:
                    await self.send(
                        str(chat_id),
                        f"Pausing replies in this chat — {self._max_consecutive_replies} "
                        "in a row from the same sender with no one else joining in "
                        "(looks like a bot loop). Send a message to resume.",
                    )
                return

            # A quote-reply to one of this bot's own messages is an implicit
            # mention (continues the thread even under require_mention), and
            # the quoted text is surfaced so the LLM knows which earlier point
            # is being replied to.
            quote = msg.get("quote") or {}
            is_reply_to_self = bool(
                quote.get("kind") == "WithMessage"
                and quote.get("author_display_name")
                and self._display_name
                and quote["author_display_name"].strip().lower()
                == self._display_name.strip().lower()
            )
            if quote.get("kind") == "WithMessage" and quote.get("text"):
                text = (
                    f'[replying to {quote.get("author_display_name") or "a message"}: '
                    f'"{quote["text"]}"]\n{text}'
                )

            if not is_reply_to_self and not await self._check_mention(
                text, chat_type, chat_id
            ):
                return

            # Build source
            source = self.build_source(
                chat_id=str(chat_id),
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
            )

            # Append chat token for dc_safe_rpc_call — skip on slash commands so
            # Hermes doesn't misparse the token as part of the command argument.
            if text.startswith("/"):
                text_with_token = text
                token = None
            else:
                token = await _get_or_create_chat_token(
                    self.rpc, self.account_id, int(chat_id)
                )
                text_with_token = f"{text}\n[dc:chat={token}]"

            # Build and handle message event
            message_event = MessageEvent(
                text=text_with_token,
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(msg_id),
                metadata=self._message_metadata(
                    chat_id, msg_id, from_id, chat_type == "group", token
                ),
            )
            await self.handle_message(message_event)

        except Exception as e:
            logger.error(f"Error handling message event: {e}")

    def _resolve_blob_path(self, filename: str) -> Optional[str]:
        """Resolve a DC file path to an accessible absolute path.

        The RPC returns whatever path DC core has internally, which may be
        absolute already or relative to the blob directory. Try in order:
        the path as-is, then <dc_config_dir>/blobs/<basename>.
        """
        if not filename:
            return None
        if os.path.exists(filename):
            logger.debug("Blob path exists as-is: %s", filename)
            return filename
        blob_path = os.path.join(
            self._get_dc_config_dir(), "blobs", os.path.basename(filename)
        )
        if os.path.exists(blob_path):
            logger.debug("Blob path resolved via blobs dir: %s", blob_path)
            return blob_path
        logger.warning("Media file not found at %r or %r", filename, blob_path)
        return None

    def _copy_to_hermes_cache(self, src: str, kind: str) -> str:
        """Copy a DC blob file into the Hermes cache directory and return the new path.

        DC blob paths are not mounted inside the Docker LLM backend, so files
        must live under ~/.hermes/cache/* for STT and vision to reach them.
        Returns the original path on failure so the caller still has something.
        """
        try:
            ext = os.path.splitext(src)[1] or ""
            data = open(src, "rb").read()
            if kind == "audio":
                from gateway.platforms.base import cache_audio_from_bytes

                dest = cache_audio_from_bytes(data, ext=ext or ".ogg")
            elif kind == "image":
                from gateway.platforms.base import cache_image_from_bytes

                dest = cache_image_from_bytes(data, ext=ext or ".jpg")
            else:
                return src
            logger.info("Copied %s blob to Hermes cache: %s -> %s", kind, src, dest)
            return dest
        except Exception as e:
            logger.warning(
                "Could not copy %s to Hermes cache: %s", src, e, exc_info=True
            )
        return src

    async def _handle_non_text_message(
        self, msg: Dict, chat_id: str, msg_id: str
    ) -> None:
        """Handle non-text messages (files, images, audio, etc.).

        Args:
            msg: Delta Chat message dictionary (AttrDict — keys already snake_case)
            chat_id: Chat ID (string representation)
            msg_id: Message ID (string representation)
        """
        # AttrDict converts viewType → view_type
        view_type = msg.get("view_type", "")
        filename = msg.get("file", "")
        file_mime = msg.get("file_mime", "") or ""

        # If the file isn't available yet (auto-download still in progress),
        # trigger download_full_message and re-fetch once before proceeding.
        if not filename and view_type not in ("Text", "", None):
            logger.info(
                "_handle_non_text_message: file not ready, triggering download for msg %s",
                msg_id,
            )
            try:
                await self.rpc.download_full_message(self.account_id, int(msg_id))
                await asyncio.sleep(2)
                msg = await self.rpc.get_message(self.account_id, int(msg_id))
                filename = msg.get("file", "")
                file_mime = msg.get("file_mime", "") or ""
                view_type = msg.get("view_type", "")
                logger.info(
                    "_handle_non_text_message: after download: file=%r view_type=%r",
                    filename,
                    view_type,
                )
            except Exception as e:
                logger.warning(
                    "_handle_non_text_message: download_full_message failed: %s", e
                )

        logger.info(
            f"_handle_non_text_message: view_type={view_type}, chat_id={chat_id}, "
            f"msg_id={msg_id}, filename={filename[:100] if filename else None}"
        )

        # Resolve sender and chat info (shared by all branches)
        from_id = msg.get("from_id")
        user_name = f"Contact {from_id}" if from_id else "Unknown"
        user_id = str(from_id) if from_id else "unknown"
        try:
            if from_id:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
                user_name = (
                    contact.get("name")
                    or contact.get("display_name")
                    or contact.get("name_and_addr")
                    or user_name
                )
        except Exception:
            pass

        chat_name = f"Chat {chat_id}"
        chat_type = "dm"
        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
            chat_name = chat.get("name", chat_name)
            chat_type = "group" if chat.get("chat_type") == "Group" else "dm"
        except Exception:
            pass

        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        token = await _get_or_create_chat_token(self.rpc, self.account_id, int(chat_id))

        caption = msg.get("text", "") or ""
        if not await self._check_mention(caption, chat_type, chat_id):
            return

        meta = self._message_metadata(
            chat_id, msg_id, from_id, chat_type == "group", token
        )

        from deltachat2.types import MessageViewtype

        # DC sometimes reports viewType=Text for image+caption messages.
        # Infer the real type from file_mime when that happens.
        if view_type in ("Text", "", None) and filename and file_mime:
            if file_mime.startswith("image/"):
                view_type = MessageViewtype.IMAGE.value
            elif file_mime.startswith("audio/"):
                view_type = MessageViewtype.AUDIO.value
            elif file_mime.startswith("video/"):
                view_type = MessageViewtype.VIDEO.value

        # Voice / Audio — let Hermes handle STT via media_urls
        if (
            view_type in (MessageViewtype.VOICE.value, MessageViewtype.AUDIO.value)
            and filename
        ):
            resolved = self._resolve_blob_path(filename)
            if resolved:
                resolved = self._copy_to_hermes_cache(resolved, "audio")
            is_voice = view_type == MessageViewtype.VOICE.value
            hermes_type = MessageType.VOICE if is_voice else MessageType.AUDIO
            caption = msg.get("text", "") or ""
            text = f"[{'Voice' if is_voice else 'Audio'} message from {user_name}]"
            if caption:
                text = f"{text}: {caption}"
            text = f"{text}\n[dc:chat={token}]"
            if not resolved:
                logger.warning(
                    f"Voice/audio file not found, forwarding without media: {filename}"
                )
            message_event = MessageEvent(
                text=text,
                message_type=hermes_type,
                source=source,
                message_id=str(msg_id),
                media_urls=[resolved] if resolved else [],
                media_types=[file_mime or ("audio/ogg" if is_voice else "audio/mpeg")],
                metadata=meta,
            )
            await self.handle_message(message_event)

        # Image
        elif (
            view_type
            in (
                MessageViewtype.IMAGE.value,
                MessageViewtype.GIF.value,
                MessageViewtype.STICKER.value,
            )
            and filename
        ):
            resolved = self._resolve_blob_path(filename)
            if resolved:
                resolved = self._copy_to_hermes_cache(resolved, "image")
            caption = msg.get("text", "") or ""
            text = f"[Image from {user_name}]"
            if caption:
                text = f"{text}: {caption}"
            text = f"{text}\n[dc:chat={token}]"
            message_event = MessageEvent(
                text=text,
                message_type=MessageType.PHOTO,
                source=source,
                message_id=str(msg_id),
                media_urls=[resolved] if resolved else [],
                media_types=[file_mime or "image/jpeg"],
                metadata=meta,
            )
            await self.handle_message(message_event)

        # File / document (including .xdc webxdc apps)
        elif (
            view_type in (MessageViewtype.FILE.value, MessageViewtype.VIDEO.value)
            and filename
        ):
            resolved = self._resolve_blob_path(filename)
            if resolved:
                try:
                    from gateway.platforms.base import cache_document_from_bytes

                    data = open(resolved, "rb").read()
                    file_name = msg.get("file_name") or os.path.basename(resolved)
                    resolved = cache_document_from_bytes(data, file_name)
                    logger.info("Copied document to Hermes cache: %s", resolved)
                except Exception as e:
                    logger.warning("Could not copy document to Hermes cache: %s", e)
            caption = msg.get("text", "") or ""
            file_name = msg.get("file_name") or os.path.basename(filename)
            text = f"[File from {user_name}: {file_name}]"
            if caption:
                text = f"{text}: {caption}"
            text = f"{text}\n[dc:chat={token}]"
            message_event = MessageEvent(
                text=text,
                message_type=MessageType.DOCUMENT,
                source=source,
                message_id=str(msg_id),
                media_urls=[resolved] if resolved else [],
                media_types=[file_mime or "application/octet-stream"],
                metadata=meta,
            )
            await self.handle_message(message_event)

        elif view_type == "Call":
            # DC sends a Call info message (Missed call / Call ended) after calls.
            # The actual call is handled via IncomingCall/CallEnded events — ignore this.
            logger.debug(
                "Ignoring Call info message msg_id=%s text=%r", msg_id, msg.get("text")
            )

        else:
            logger.debug(f"Unhandled view_type={view_type}, file={filename}")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get metadata for a chat.

        Args:
            chat_id: Delta Chat chat ID

        Returns:
            Dictionary with chat info (name, type, etc.)
        """
        try:
            if self.rpc and self.account_id:
                chat = await self.rpc.get_basic_chat_info(
                    self.account_id,
                    int(chat_id),
                )
                return {
                    "name": chat.get("name", chat_id),
                    "type": "group" if chat.get("chat_type") == "Group" else "dm",
                }
        except Exception as e:
            logger.warning(f"Error getting chat info for {chat_id}: {e}")
        return {"name": chat_id, "type": "dm"}

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message from a Delta Chat chat.

        Args:
            chat_id: Delta Chat chat ID
            message_id: Message ID to delete

        Returns:
            True if deletion successful, False otherwise
        """
        try:
            if self.rpc and self.account_id:
                await self.rpc.delete_messages(
                    self.account_id,
                    [int(message_id)],
                )
                logger.debug(f"Deleted message {message_id} from chat {chat_id}")
                return True
        except Exception as e:
            logger.error(
                f"Error deleting message {message_id} from chat {chat_id}: {e}"
            )
            return False
        return False


def check_requirements() -> bool:
    """Check if deltachat2 and deltachat-rpc-server are available."""
    import shutil

    # Check Python package
    try:
        import deltachat2
    except ImportError:
        return False

    # Check binary
    rpc_server = os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server")
    if shutil.which(rpc_server):
        return True

    return False


def validate_config(config) -> bool:
    """Validate platform configuration."""
    if not check_requirements():
        return False

    extra = getattr(config, "extra", {}) or {}

    email = os.getenv("DELTACHAT_EMAIL") or extra.get("email", "auto")
    password = os.getenv("DELTACHAT_PASSWORD") or extra.get("password")
    if email and email != "auto" and not _is_valid_email(email):
        raise ValueError(f"DELTACHAT_EMAIL is not a valid email address: {email!r}")
    if email and email != "auto" and not password:
        raise ValueError(
            "DELTACHAT_PASSWORD required when DELTACHAT_EMAIL is set (not 'auto')"
        )

    dm_policy = os.getenv("DELTACHAT_DM_POLICY") or extra.get("dm_policy", "pairing")
    if dm_policy not in ("open", "allowlist", "pairing", "disabled"):
        raise ValueError(f"Invalid DELTACHAT_DM_POLICY: {dm_policy!r}")

    group_policy = os.getenv("DELTACHAT_GROUP_POLICY") or extra.get(
        "group_policy", "open"
    )
    if group_policy not in ("open", "allowlist", "disabled"):
        raise ValueError(f"Invalid DELTACHAT_GROUP_POLICY: {group_policy!r}")

    # Lightweight path checks (do not create directories or require the binary).
    from gateway.config import get_hermes_home

    data_dir = os.getenv("DELTACHAT_DATA_DIR") or extra.get(
        "data_dir", os.path.join(get_hermes_home(), "deltachat-platform")
    )
    _safe_data_dir(data_dir, create=False)

    avatar_path = os.getenv("DELTACHAT_AVATAR_PATH") or extra.get("avatar_path")
    if avatar_path:
        _validate_avatar_path(avatar_path, strict=False)

    rpc_server = os.getenv("DELTACHAT_RPC_SERVER") or extra.get(
        "rpc_server", "deltachat-rpc-server"
    )
    if rpc_server != "deltachat-rpc-server":
        _validate_rpc_server_path(rpc_server, strict=True)

    chatmail_servers = os.getenv("DELTACHAT_CHATMAIL_SERVERS") or extra.get(
        "chatmail_servers"
    )
    if chatmail_servers:
        servers = _parse_chatmail_servers(chatmail_servers)
        if not servers:
            raise ValueError(
                f"Invalid DELTACHAT_CHATMAIL_SERVERS: {chatmail_servers!r}"
            )

    max_len = os.getenv("DELTACHAT_MAX_MESSAGE_LENGTH") or extra.get(
        "max_message_length"
    )
    if max_len:
        try:
            max_len_int = int(max_len)
        except ValueError:
            raise ValueError(f"Invalid DELTACHAT_MAX_MESSAGE_LENGTH: {max_len!r}")
        if max_len_int < 100 or max_len_int > 10000:
            raise ValueError(
                f"DELTACHAT_MAX_MESSAGE_LENGTH must be between 100 and 10000: {max_len!r}"
            )

    return True


def _apply_yaml_config(
    yaml_cfg: Dict[str, Any], platform_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Bridge YAML config values to env-style extra keys for the platform adapter.

    The gateway config loader calls this hook with the parsed YAML tree and the
    deltachat-platform config block (which may be nested under ``platforms``).
    Values returned here are merged into ``platform_config.extra`` and are then
    read by the adapter constructor.
    """
    seeded: Dict[str, Any] = {}

    for yaml_key, extra_key in (
        ("display_name", "display_name"),
        ("avatar_path", "avatar_path"),
        ("email", "email"),
        ("chatmail_server", "chatmail_server"),
        ("chatmail_servers", "chatmail_servers"),
        ("data_dir", "data_dir"),
        ("home_channel", "home_channel"),
        ("require_mention", "require_mention"),
        ("free_response_channels", "free_response_channels"),
        ("auto_delete_interval", "auto_delete_interval"),
        ("max_message_length", "max_message_length"),
    ):
        value = platform_cfg.get(yaml_key)
        if value is not None:
            seeded[extra_key] = value

    return seeded


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig from environment variables."""
    import shutil

    rpc_server = os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server").strip()

    # Check if binary exists
    if not shutil.which(rpc_server):
        # Try without path
        if shutil.which("deltachat-rpc-server"):
            rpc_server = "deltachat-rpc-server"
        else:
            return None

    result = {"rpc_server": rpc_server}

    # Add onboarding / profile fields if set
    from gateway.config import get_hermes_home

    email = os.getenv("DELTACHAT_EMAIL")
    if email:
        result["email"] = email
    result["data_dir"] = os.getenv(
        "DELTACHAT_DATA_DIR", os.path.join(get_hermes_home(), "deltachat-platform")
    )
    display_name = os.getenv("DELTACHAT_DISPLAY_NAME")
    if display_name:
        result["display_name"] = display_name
    avatar_path = os.getenv("DELTACHAT_AVATAR_PATH")
    if avatar_path:
        result["avatar_path"] = avatar_path
    chatmail_servers = os.getenv("DELTACHAT_CHATMAIL_SERVERS")
    if not chatmail_servers:
        chatmail_servers = os.getenv("DELTACHAT_CHATMAIL_SERVER", "nine.testrun.org")
    result["chatmail_servers"] = chatmail_servers

    # Add home channel if set
    home_channel = os.getenv("DELTACHAT_HOME_CHANNEL")
    if home_channel:
        result["home_channel"] = {
            "chat_id": home_channel,
            "name": "Home",
        }

    return result


def register_platform(ctx):
    """Register Delta Chat platform adapter with Hermes."""
    ctx.register_platform(
        name="deltachat-platform",
        label="Delta Chat",
        adapter_factory=lambda cfg: DeltaChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["DELTACHAT_RPC_SERVER"],
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="DELTACHAT_HOME_CHANNEL",
        emoji="💬",
        platform_hint=(
            "You are chatting via Delta Chat. "
            "Delta Chat does NOT support markdown formatting or message editing. "
            "Messages longer than 40 lines will be automatically formatted with HTML. "
            "For very long content, consider sending as a document file instead. "
            "You CAN send voice messages (use send_voice tool), videos, images, "
            "files, and delete messages. "
            "When a user sends a voice message, it is automatically transcribed — "
            "just respond to the transcribed content normally. "
            "Location messages can be sent to share points of interest on a map. "
            "You CAN build and send webxdc mini apps and other files (PDF, HTML, etc.). "
            "MANDATORY: before attempting to build any webxdc app, you MUST first call "
            "skill_view('plugin:deltachat-platform:webxdc-converter') "
            "to load the build instructions. "
            "For file delivery from the Docker sandbox: write output files "
            "to /workspace/ (NOT /tmp/), "
            "then use a MEDIA directive — e.g. 'MEDIA:/workspace/app.xdc'. "
            "The adapter maps /workspace/ paths to the host and sends via send_document. "
            "DC core auto-detects .xdc as webxdc — just send it as a regular file. "
            "Each message ends with a [dc:chat=<token>] metadata tag. "
            "IGNORE this tag during normal conversation — it is only needed "
            "if you call dc_safe_rpc_call. "
            "Do NOT call dc_safe_rpc_call, dc_chat_rpc_spec, or dc_rpc_spec "
            "unless the user explicitly "
            "asks for a Delta Chat-specific operation that cannot be done with the standard tools."
        ),
        max_message_length=DC_MESSAGE_MAX_LEN,
    )

    # Register bundled skills so skill_view('deltachat-platform:<name>') resolves them.
    from pathlib import Path as _Path

    skills_dir = _Path(_plugin_dir) / "skills"
    logger.info(f"Checking for skills in: {skills_dir}")
    if skills_dir.is_dir():
        for skill_dir in skills_dir.iterdir():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.is_file():
                try:
                    ctx.register_skill(skill_dir.name, skill_md)
                    logger.info(
                        "Registered plugin skill: %s from %s", skill_dir.name, skill_md
                    )
                except Exception as e:
                    logger.warning("Could not register skill %s: %s", skill_dir.name, e)
    else:
        logger.warning("Skills directory not found: %s", skills_dir)


def register_rpc_tools(ctx) -> None:
    """Register Delta Chat RPC tools.

    Always registers:
      - dc_rpc_spec: full OpenRPC spec
      - dc_chat_rpc_spec: spec filtered to chatId-scoped, non-destructive methods
      - dc_safe_rpc_call: chat-scoped calls with token-validated chatId injection

    Only registers when DELTACHAT_ENABLE_RAW_RPC is set:
      - dc_rpc_call: unrestricted access to any RPC method
    """

    async def _spec_handler(args: dict = None, **kwargs) -> str:
        try:
            return json.dumps(await _fetch_spec(), indent=2)
        except Exception as e:
            return f"Error: {e}"

    async def _call_handler(args: dict, **kwargs) -> str:
        method = (args or {}).get("method")
        params = (args or {}).get("params") or []
        if not method or not isinstance(method, str):
            return json.dumps({"error": "Missing 'method' (snake_case RPC name)."})
        if _active_adapter is None or _active_adapter.rpc is None:
            return json.dumps({"error": "Delta Chat is not connected"})

        logger.warning("Raw RPC call: %s", method)

        if _RAW_RPC_ALLOWLIST and method not in _RAW_RPC_ALLOWLIST:
            return json.dumps({"error": f"'{method}' is not in the raw RPC allowlist"})
        if (
            method in _RAW_RPC_BLOCKLIST
            or method in _DESTRUCTIVE_METHODS
            or method.startswith("delete_")
            or method.startswith("remove_")
        ):
            return json.dumps({"error": f"'{method}' is blocked"})

        try:
            result = await getattr(_active_adapter.rpc, method)(*params)
            return json.dumps(result, default=str)
        except AttributeError:
            return json.dumps({"error": f"Unknown method '{method}'"})
        except Exception as e:
            logger.error("Raw RPC call %s failed: %s", method, e, exc_info=True)
            return json.dumps({"error": "RPC call failed"})

    async def _chat_spec_handler(args: dict = None, **kwargs) -> str:
        """Return only the chatId-scoped, non-destructive methods."""
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return f"Error: {e}"
        safe_methods = [
            m
            for m in spec.get("methods", [])
            if any(p["name"] == "chatId" for p in m.get("params", []))
            and m["name"] not in _DESTRUCTIVE_METHODS
            and not m["name"].startswith("delete_")
            and not m["name"].startswith("remove_")
        ]
        return json.dumps({**spec, "methods": safe_methods}, indent=2)

    async def _safe_call_handler(args: dict, **kwargs) -> Any:
        method = (args or {}).get("method")
        chat_token = (args or {}).get("chat_token")
        params = (args or {}).get("params") or []
        if not method or not isinstance(method, str):
            return json.dumps(
                {
                    "error": (
                        "Missing 'method' (snake_case RPC name). "
                        "Use dc_chat_rpc_spec to find one."
                    )
                }
            )
        adapter = _active_adapter
        if adapter is None or adapter.rpc is None:
            return {"error": "Delta Chat is not connected"}

        # Resolve token → real chat_id
        real_chat_id = await _resolve_chat_token(
            adapter.rpc, adapter.account_id, chat_token
        )
        if real_chat_id is None:
            return json.dumps(
                {
                    "error": "Unknown chat_token — use the [dc:chat=...] value from your message"
                }
            )

        # Block destructive methods
        if (
            method in _DESTRUCTIVE_METHODS
            or method.startswith("delete_")
            or method.startswith("remove_")
        ):
            return json.dumps({"error": f"'{method}' is not allowed in safe mode"})

        # Verify method exists and has a chatId param
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return json.dumps({"error": f"Could not fetch spec: {e}"})

        method_entry = next(
            (m for m in spec.get("methods", []) if m["name"] == method), None
        )
        if method_entry is None:
            return json.dumps(
                {
                    "error": (
                        f"Unknown method '{method}' — "
                        "use dc_chat_rpc_spec to browse available methods"
                    )
                }
            )

        param_names = [p["name"] for p in method_entry.get("params", [])]
        if "chatId" not in param_names:
            return json.dumps(
                {
                    "error": (
                        f"'{method}' has no chatId parameter — "
                        "use dc_rpc_call for non-chat methods"
                    )
                }
            )

        # Build positional params: accountId at [0], chatId at [1]
        full_params = [adapter.account_id, real_chat_id] + list(params or [])

        logger.info("Safe RPC call: %s (chat_id=%s)", method, real_chat_id)
        try:
            result = await getattr(adapter.rpc, method)(*full_params)
            return json.dumps(result, default=str)
        except AttributeError:
            return json.dumps({"error": f"Unknown method '{method}'"})
        except Exception as e:
            logger.error("Safe RPC call %s failed: %s", method, e, exc_info=True)
            return json.dumps({"error": "RPC call failed"})

    async def _end_call_handler(args: dict, **kwargs) -> str:
        adapter = _active_adapter
        if adapter is None or adapter._call_manager is None:
            return json.dumps({"error": "No active call"})

        # The AI is in a call — find the active session.
        # There is typically only one active call at a time.
        chat_id = adapter._call_manager.first_active_chat_id()
        if chat_id is None:
            return json.dumps({"error": "No active call"})

        success = await adapter._call_manager.request_hangup(chat_id)
        if success:
            return json.dumps({"success": True, "message": "Call ended"})
        return json.dumps({"error": "Failed to end call"})

    async def _start_call_handler(args: dict, **kwargs) -> str:
        args = args or {}
        chat_token = args.get("chat_token")
        # `opening` is the exact line spoken on connect; accept `topic` as alias.
        opening = (args.get("opening") or args.get("topic") or "").strip()
        adapter = _active_adapter
        if adapter is None or adapter._call_manager is None:
            return json.dumps({"error": "Delta Chat not connected"})

        if not opening:
            return json.dumps(
                {
                    "error": "Provide 'opening' — the exact words to say when they pick up."
                }
            )

        real_chat_id = await _resolve_chat_token(
            adapter.rpc, adapter.account_id, chat_token
        )
        if real_chat_id is None:
            return json.dumps(
                {"error": "Unknown chat_token — use the [dc:chat=...] value"}
            )

        try:
            msg_id = await adapter._call_manager.start_call(
                str(real_chat_id), opening=opening
            )
            return json.dumps(
                {
                    "success": True,
                    "msg_id": msg_id,
                    "message": "Call connected — the opening line is being "
                    "spoken and the conversation is live.",
                }
            )
        except asyncio.TimeoutError:
            return json.dumps({"error": "Call was not answered"})
        except Exception as e:
            logger.error("start_call failed: %s", e, exc_info=True)
            return json.dumps({"error": f"Failed to start call: {e}"})

    async def _send_message_handler(args: dict, **kwargs) -> str:
        """Send text to a chat proactively (not as a reply to an inbound message).

        Used for cron/scheduled pushes or agent-to-agent chatter where there
        is no incoming [dc:chat=...] token in hand yet.
        """
        args = args or {}
        text = (args.get("text") or "").strip()
        chat_token = args.get("chat_token")
        adapter = _active_adapter
        if adapter is None or adapter.rpc is None:
            return json.dumps({"error": "Delta Chat is not connected"})
        if not text:
            return json.dumps({"error": "Provide 'text' to send."})

        if chat_token:
            real_chat_id = await _resolve_chat_token(
                adapter.rpc, adapter.account_id, chat_token
            )
            if real_chat_id is None:
                return json.dumps(
                    {
                        "error": "Unknown chat_token — use the [dc:chat=...] value "
                        "from a message in that chat"
                    }
                )
        else:
            home_channel = os.getenv("DELTACHAT_HOME_CHANNEL")
            if not home_channel:
                return json.dumps(
                    {
                        "error": "No chat_token given and DELTACHAT_HOME_CHANNEL is "
                        "not configured — there is no default chat to send to."
                    }
                )
            try:
                real_chat_id = int(home_channel)
            except ValueError:
                return json.dumps(
                    {"error": "DELTACHAT_HOME_CHANNEL is not a valid chat id"}
                )

        try:
            result = await adapter.send(str(real_chat_id), text)
        except Exception as e:
            logger.error("dc_send_message failed: %s", e, exc_info=True)
            return json.dumps({"error": "Send failed"})
        if not result.success:
            return json.dumps({"error": result.error or "Send failed"})
        return json.dumps({"success": True, "message_id": result.message_id})

    ctx.register_tool(
        name="dc_rpc_spec",
        toolset="deltachat",
        schema={
            "description": (
                "Fetch the full OpenRPC specification of the running Delta Chat RPC server. "
                "Lists every available method with parameter types and descriptions. "
                "Only call this when the user explicitly asks for low-level Delta Chat API access. "
                "Use dc_chat_rpc_spec instead when you only need chat-scoped methods."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_spec_handler,
        is_async=True,
        emoji="📋",
    )

    ctx.register_tool(
        name="dc_chat_rpc_spec",
        toolset="deltachat",
        schema={
            "description": (
                "Fetch the OpenRPC spec filtered to methods that accept a chatId parameter, "
                "excluding all destructive operations. "
                "Only call this when you are about to use dc_safe_rpc_call for an "
                "explicit user request that cannot be handled by normal messaging tools."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_chat_spec_handler,
        is_async=True,
        emoji="📋",
    )

    if os.getenv("DELTACHAT_ENABLE_RAW_RPC"):
        ctx.register_tool(
            name="dc_rpc_call",
            toolset="deltachat",
            schema={
                "description": (
                    "Call any Delta Chat RPC method directly by name and params. "
                    "Use dc_rpc_spec first to see available methods. "
                    "CAUTION: unrestricted access — can modify or delete account data. "
                    "Prefer dc_safe_rpc_call for chat-scoped operations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "description": (
                                "RPC method name in snake_case (e.g. 'get_account_info'). "
                                "Use dc_rpc_spec to see all available methods."
                            ),
                        },
                        "params": {
                            "type": "array",
                            "description": "Full positional parameters. account_id is always 1.",
                            "default": [],
                        },
                    },
                    "required": ["method"],
                },
            },
            handler=_call_handler,
            is_async=True,
            emoji="⚡",
        )

    ctx.register_tool(
        name="dc_safe_rpc_call",
        toolset="deltachat",
        schema={
            "description": (
                "Call a chat-scoped Delta Chat RPC method safely. "
                "Only use this when the user explicitly asks for a Delta Chat-specific operation "
                "that cannot be done with the normal send, send_file, send_voice, "
                "or delete_message tools. "
                "Do NOT call this for routine message handling, reading messages, "
                "or sending replies — those go through the standard tools. "
                "accountId and chatId are injected automatically from the chat_token. "
                "Destructive methods are blocked. "
                "Use dc_chat_rpc_spec first to find the method name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": (
                            "RPC method name in snake_case (e.g. 'get_chat_contacts'). "
                            "Must accept chatId. Use dc_chat_rpc_spec to browse available methods."
                        ),
                    },
                    "chat_token": {
                        "type": "string",
                        "description": (
                            "The opaque chat token from the [dc:chat=...] line "
                            "in the current message. Never use a token from a "
                            "different conversation."
                        ),
                    },
                    "params": {
                        "type": "array",
                        "description": (
                            "Extra positional parameters after accountId and chatId. "
                            "accountId (always 1) and chatId are injected automatically."
                        ),
                        "default": [],
                    },
                },
                "required": ["method", "chat_token"],
            },
        },
        handler=_safe_call_handler,
        is_async=True,
        emoji="🔒",
    )

    ctx.register_tool(
        name="dc_end_call",
        toolset="deltachat",
        schema={
            "description": (
                "End the active voice call. "
                "The goodbye message is spoken first (via normal send), then this "
                "tool waits until TTS finishes playing before disconnecting. "
                "Only use this when the user explicitly says goodbye or asks to end the call. "
                "No parameters needed — there is only one active call at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_end_call_handler,
        is_async=True,
        emoji="📞",
    )

    ctx.register_tool(
        name="dc_start_call",
        toolset="deltachat",
        schema={
            "description": (
                "Place an outgoing voice call to a Delta Chat contact and talk to them. "
                "Use this to proactively call someone — e.g. from a scheduled/cron task "
                "(a reminder, an alert, a check-in). Creates the WebRTC offer, rings the "
                "contact, and blocks until they answer (or times out if unanswered). "
                "Once connected you speak normally; the conversation runs like an incoming "
                "call. Identify the recipient with the chat_token from one of their messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_token": {
                        "type": "string",
                        "description": (
                            "The opaque chat token from the [dc:chat=...] line in a message "
                            "from the person to call. Never use a token from another conversation."
                        ),
                    },
                    "opening": {
                        "type": "string",
                        "description": (
                            "The EXACT words to say the instant they pick up "
                            '(e.g. "Hi Simon, quick reminder to take your medication."). '
                            "Synthesized while the phone is still ringing and played "
                            "immediately on answer — no startup delay. Write it as natural "
                            "speech, not a topic label."
                        ),
                    },
                },
                "required": ["chat_token", "opening"],
            },
        },
        handler=_start_call_handler,
        is_async=True,
        emoji="📞",
    )

    ctx.register_tool(
        name="dc_send_message",
        toolset="deltachat",
        schema={
            "description": (
                "Send a text message to a Delta Chat chat proactively — not as a reply "
                "to an inbound message. Use this from a scheduled/cron task, or when "
                "one agent needs to post into a shared group without having an inbound "
                "[dc:chat=...] token in hand yet (e.g. a multi-agent group where other "
                "bots run on different servers). If chat_token is omitted, sends to "
                "DELTACHAT_HOME_CHANNEL if configured."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message text to send.",
                    },
                    "chat_token": {
                        "type": "string",
                        "description": (
                            "The opaque chat token from the [dc:chat=...] line in a "
                            "message from that chat. Omit to fall back to "
                            "DELTACHAT_HOME_CHANNEL. Never use a token from a "
                            "different conversation."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
        handler=_send_message_handler,
        is_async=True,
        emoji="📤",
    )
