"""Delta Chat platform adapter for Hermes Gateway.

Integrates Delta Chat as a messaging platform using deltachat2 (direct JSON-RPC).
"""

import email.utils
import functools
import inspect
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import asyncio
import logging
import mimetypes
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
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

# why: Hermes >= 0.21.5 marks only a turn's final reply with metadata["notify"]
# (base.py _mark_notify_metadata). Voice calls speak only that; on an older
# core nothing carries the flag, so filtering on it would silence every call.
_BASE_MARKS_FINAL_REPLY = hasattr(
    sys.modules.get("gateway.platforms.base"), "_mark_notify_metadata"
)

# Must use "hermes_plugins.*" prefix so records appear in gateway.log.
# __name__ resolves to "adapter" (standalone module), which only goes to agent.log.
logger = logging.getLogger("hermes_plugins.deltachat")


def _is_on(value) -> bool:
    """The one on/off rule for settings: "1"/"true"/"yes"/"on" are on.

    Everything else is off — "0", "off", a blank, and anything unrecognised.
    why one rule: there were three (plain truthiness, where "0" was on; a
    list without "on"; a list of off-words, where a typo was on), so the same
    value meant different things for different settings.
    call_handler._env_flag is the same rule (that module cannot import this one).
    """
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Enable debug logging for RPC if requested. It logs every RPC request and
# response, passwords and invite links included.
if _is_on(os.getenv("DELTACHAT_DEBUG", "")):
    logging.getLogger("deltachat2").setLevel(logging.DEBUG)
    logging.getLogger("deltachat2.IOTransport").setLevel(logging.DEBUG)

# Minimum required Delta Chat core version
# Plugin will NOT connect with older versions
MIN_DC_VERSION = "2.51.0"

# DC truncates at ~3800; split conservatively
DC_MESSAGE_MAX_LEN = 3600

# Conservative conversational line ceiling for a single Delta Chat message.
# Delta Chat has no markdown rendering and auto-converts long text to HTML;
# keeping each outbound message short and plain reads like a chat, not a doc.
DC_MESSAGE_MAX_LINES = 20

# Maximum image download size for send_image_file() URLs (25 MiB)
_MAX_IMAGE_SIZE = 25 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Delta Chat's reserved contact id for the account itself.
DC_CONTACT_ID_SELF = 1

# DC config key pairing the database with Hermes' state; see _check_db_id.
_DB_ID_KEY = "ui.hermes.db_id"

_WORKSPACE_PREFIX = "/workspace/"
_XDC_MEDIA_RE = re.compile(
    r'[`"\']?MEDIA:\s*[`"\']?((?:~/|/)[\w./\- ]+\.xdc)[`"\']?', re.IGNORECASE
)
# Bare local .xdc path in reply text (not inside a MEDIA: tag): a Docker
# sandbox /workspace/ container path (filter_local_delivery_paths maps it to
# the host), or a real host-cwd absolute/~ path for non-Docker deployments.
_XDC_LOCAL_PATH_RE = re.compile(
    r"(?<![/:\w.])((?:~/|/)[\w./\-]+\.xdc)\b", re.IGNORECASE
)


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


def _cfg_bool(config, env: str, key: str, default: str = "false") -> bool:
    """Read a boolean platform config value; see _is_on for the rule.

    why "on" counts: DELTACHAT_REQUIRE_MENTION=on used to read as off,
    silently leaving a gate open.
    """
    return _is_on(_cfg(config, env, key, default))


# (env var, config.extra key, default, min, max) — shared by the adapter
# constructor (warn + default) and validate_config (raise).
_MAX_LEN_CFG = (
    "DELTACHAT_MAX_MESSAGE_LENGTH",
    "max_message_length",
    DC_MESSAGE_MAX_LEN,
    100,
    10000,
)
_MAX_LINES_CFG = (
    "DELTACHAT_MAX_MESSAGE_LINES",
    "max_message_lines",
    DC_MESSAGE_MAX_LINES,
    1,
    200,
)


def _bounded_int(raw, lo: int, hi: int) -> Optional[int]:
    """Return int(raw) if it parses and lies within [lo, hi], else None."""
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if lo <= val <= hi else None


def _cfg_bounded_int(config, env: str, key: str, default: int, lo: int, hi: int) -> int:
    """Read an int config value; fall back to *default* if invalid or out of range."""
    raw = _cfg(config, env, key, str(default))
    val = _bounded_int(raw, lo, hi)
    if val is None:
        logger.warning(
            "%s %r invalid or out of bounds (%d-%d), using default %s",
            env,
            raw,
            lo,
            hi,
            default,
        )
        return default
    return val


_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _split_table_row(line: str) -> list:
    """Split one ``| a | b |`` row into stripped cell values."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _strip_md_tables(text: str) -> str:
    """Flatten GFM pipe tables into plain ``Header: value`` lines.

    A table is a line containing ``|`` immediately followed by a
    delimiter row (``| --- | --- |``). Each body row becomes
    ``h1: c1, h2: c2`` (or just the cells joined by ``, `` when the row
    width doesn't match the header). Lines with stray pipes but no
    delimiter row underneath are left untouched.
    """
    # ponytail: a pipe table written inside a ``` code fence would also be
    # flattened here (this runs before fence stripping). Add fence tracking
    # if a real case shows up.
    lines = text.split("\n")
    out: list = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if (
            "|" in line
            and i + 1 < n
            and "|" in lines[i + 1]
            and _TABLE_DELIM_RE.match(lines[i + 1])
        ):
            headers = _split_table_row(line)
            i += 2
            while i < n and lines[i].strip() and "|" in lines[i]:
                cells = _split_table_row(lines[i])
                if len(cells) == len(headers) and any(headers):
                    pairs = [
                        f"{h}: {c}" if h else c
                        for h, c in zip(headers, cells)
                        if c or h
                    ]
                    out.append(", ".join(p for p in pairs if p))
                else:
                    out.append(", ".join(c for c in cells if c))
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _strip_markdown(text: str) -> str:
    """Render markdown down to plain text for Delta Chat.

    Delta Chat has no markdown rendering, so markers would otherwise leak
    into the delivered message. Pipe tables collapse to ``Header: value``
    lines; headings lose their ``#``; emphasis loses ``*``/``_``; links
    become ``label (URL)``; fenced-code delimiters are removed but the
    code content (and its indentation) is kept; bullet markers are
    normalised to ``- ``. Paragraph spacing and ordinary punctuation/URLs
    are left untouched.
    """
    if not text:
        return text
    text = _strip_md_tables(text)
    # Fenced code: drop the ``` delimiters (and any info string), keep body.
    text = re.sub(r"```[^\n]*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # ATX headings, including the optional closing run of #.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    # Bullet list markers (*, +, -, •) -> "- ", indentation preserved.
    text = re.sub(r"(?m)^(\s*)[*+•-]\s+", r"\1- ", text)
    text = re.sub(r"(\*\*\*|___)(.+?)\1", r"\2", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<!\w)(\*|_)(.+?)\1(?!\w)", r"\2", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    return text


def _hard_wrap_line(line: str, max_len: int) -> list[str]:
    """Last-resort split of one over-long line, preferring word boundaries.

    Only reached when a single line exceeds ``max_len`` with no newline to
    break on. Prefers a sentence/word boundary; a mid-word cut (no boundary
    found) logs a warning and steps back off any combining character so a
    code point is never split.
    """
    out: list[str] = []
    remaining = line
    while len(remaining) > max_len:
        split_at = -1
        for sep, extra in ((". ", 1), (", ", 1), (" ", 0)):
            idx = remaining.rfind(sep, 0, max_len)
            if idx > max_len * 0.25:
                split_at = idx + extra
                break
        if split_at <= 0:
            split_at = max_len
            while split_at > 1 and unicodedata.combining(remaining[split_at]):
                split_at -= 1
            logger.warning(
                "DeltaChat: hard character split of a %d-char line with no "
                "word boundary; a token may be broken across messages",
                len(line),
            )
        out.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def _split_message(
    text: str,
    max_len: int = DC_MESSAGE_MAX_LEN,
    max_lines: int = DC_MESSAGE_MAX_LINES,
) -> list[str]:
    """Split text so every chunk is within ``max_len`` chars and ``max_lines``
    lines.

    Splits at line and paragraph boundaries first; only a single line longer
    than ``max_len`` falls back to a hard character split (which logs a
    warning). Ordering is preserved and nothing is truncated.
    """
    if not text:
        return []
    if max_len < 1:
        max_len = DC_MESSAGE_MAX_LEN
    if max_lines < 1:
        max_lines = DC_MESSAGE_MAX_LINES
    if len(text) <= max_len and text.count("\n") + 1 <= max_lines:
        return [text]

    chunks: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            chunk = "\n".join(cur).strip("\n")
            if chunk:
                chunks.append(chunk)
            cur.clear()

    for line in text.split("\n"):
        if len(line) > max_len:
            flush()
            chunks.extend(_hard_wrap_line(line, max_len))
            continue
        would_be = ("\n".join(cur + [line])) if cur else line
        if cur and (len(cur) + 1 > max_lines or len(would_be) > max_len):
            flush()
        cur.append(line)
    flush()
    return chunks


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


def _default_dc_data_dir() -> str:
    """Default Delta Chat account-data directory (when DELTACHAT_DATA_DIR unset).

    v1.6.0 briefly renamed the plugin (and this default) from
    deltachat-platform to deltachat; v1.7.0 reverted the rename, since
    "-platform" turned out to be Hermes's own naming convention for
    messaging platform plugins. Falls back to a v1.6.x install's
    <HERMES_HOME>/deltachat/ directory when it already holds account data
    and the (restored) default doesn't, so installs made during that
    window keep working without a manual migration step.
    """
    from gateway.config import get_hermes_home

    home = get_hermes_home()
    new_path = os.path.join(home, "deltachat-platform")
    old_path = os.path.join(home, "deltachat")

    def _has_data(p: str) -> bool:
        return os.path.isdir(p) and any(os.scandir(p))

    if _has_data(old_path) and not _has_data(new_path):
        return old_path
    return new_path


def _base_supports_session_key(fn) -> bool:
    """Whether a BasePlatformAdapter static method accepts a session_key kwarg.

    Newer Hermes cores added ``session_key: str = ""`` to
    ``filter_media_delivery_paths``/``filter_local_delivery_paths``; older
    ones (e.g. 0.15.1) take a single positional argument. Forwarding the
    kwarg unconditionally would crash on those older cores the exact same
    way omitting it crashes on newer ones — inspect the installed base's
    actual signature instead of assuming either.
    """
    try:
        return "session_key" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


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
        True if version is compatible, False if it is too old or could not be
        determined at all. Fail-closed on both counts.
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
            # info, not warning: a newer-than-floor core is the common case
            # (MIN_DC_VERSION is a floor, not a pin) and isn't actionable —
            # logging it at WARNING every startup is just noise.
            logger.info(
                "Delta Chat version %s is newer than the minimum "
                "required version %s. Continuing.",
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


def _parse_csv_unique(raw: str) -> list[str]:
    """Split a comma-separated string into trimmed, non-empty items.

    Case-insensitive duplicates are dropped; first spelling and order win.
    """
    items = [s.strip() for s in raw.split(",") if s.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for s in items:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def _build_mention_pattern(name: str) -> Optional[re.Pattern]:
    """Build an @mention regex for *name*, tolerant of short case-ending
    variation (e.g. Czech declension: Alice -> @Alici/@Alicí, Anikke ->
    @Anikko) by matching a stem plus up to 2 trailing word characters.

    Falls back to an exact word match for names too short to safely stem
    (the stem must be >=3 chars — otherwise a short/generic stem could
    match unrelated words that happen to share a prefix).

    Requires a leading "@" so a bare name used in prose (e.g. "napis
    Alici", asking someone else to message Alice) does not count as
    addressing the bot directly.
    """
    if not name:
        return None
    stem = name[:-1]
    core = re.escape(stem) + r"\w{0,2}" if len(stem) >= 3 else re.escape(name)
    return re.compile(rf"(?:^|\W)@{core}(?:\W|$)", re.IGNORECASE)


# why: /start only acknowledges Telegram's start ping and /topic refuses
# everything but Telegram DMs; every other gateway command works here.
_BIO_SKIP_COMMANDS = frozenset({"start", "topic"})

# Everything above this line in the bio is the operator's own text and is
# kept; everything below it is regenerated on connect.
_BIO_MARKER = "Hermes commands:"
_BIO_DEFAULT_INTRO = "Hermes AI assistant – just write to me."


def _own_bio(current: str) -> Optional[str]:
    """The operator's text above the command list, or None when there is no list."""
    # splitlines: a bio edited on another client may come back with \r\n.
    lines = current.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _BIO_MARKER:
            return "\n".join(lines[:i]).strip()
    return None


def _commands_bio(own: str, extra: Dict) -> Optional[str]:
    """*own* bio text with the gateway's slash commands appended, one per line.

    Built from Hermes' own registry so it follows the installed version; None
    when that API isn't there (older cores).
    """
    try:
        # Private helpers, but the ones Hermes builds Telegram's command menu from.
        from hermes_cli.commands_platforms import _gateway_available_commands
        from hermes_cli.commands import _iter_plugin_command_entries
        from gateway.slash_access import policy_from_extra

        entries = [
            (c.name, c.args_hint, c.description) for c in _gateway_available_commands()
        ]
        entries += [
            (name, hint, desc) for name, desc, hint in _iter_plugin_command_entries()
        ]
        # Everyone who gets a message sees the bio, so list only what a
        # non-admin may run in a DM — the filter Hermes' /help applies.
        policy = policy_from_extra(extra, "dm")
    except Exception as e:
        logger.warning(
            "Not setting the commands bio, Hermes command registry unavailable: %s", e
        )
        return None
    lines = [own or _BIO_DEFAULT_INTRO, "", _BIO_MARKER]
    for name, hint, desc in entries:
        if name in _BIO_SKIP_COMMANDS or not policy.can_run(None, name):
            continue
        usage = f"/{name} {hint}".strip()
        # One line per command, without the parenthesised details.
        lines.append(f"{usage} – {' '.join(desc.split()).split(' (')[0]}")
    return "\n".join(lines)


# Telegram-style addressed command: "/cmd@<name>". See _command_for_us.
_COMMAND_ADDR_RE = re.compile(r"/[\w-]+@")


def _url_is_public(url: str) -> bool:
    """Whether *url* resolves only to public addresses. Blocking (DNS).

    why: send_image_file fetches whatever URL the agent hands it, from the
    host — loopback, the LAN and cloud metadata endpoints included. Hermes'
    own check when present (it honours the operator's allow_private_urls);
    otherwise every resolved address must be global. Fail-closed on errors.
    """
    try:
        from tools.url_safety import is_safe_url
    except ImportError:
        is_safe_url = None
    if is_safe_url is not None:
        return bool(is_safe_url(url))
    # ponytail: resolve-then-fetch leaves a DNS-rebinding window on cores
    # without tools.url_safety; pin the resolved address if that matters.
    try:
        host = urllib.parse.urlparse(url).hostname
        infos = socket.getaddrinfo(host, None)
        return bool(infos) and all(
            ipaddress.ip_address(info[4][0]).is_global for info in infos
        )
    except Exception:
        return False


def _calling_chat_mismatch(adapter, real_chat_id) -> bool:
    """True when the current tool call comes from a chat other than *real_chat_id*.

    why: a chat token is only shown in its own chat, but the agent can carry
    one elsewhere (memory, a cron listing), and then anyone in chat A could
    have it read chat B. Hermes binds the calling session's platform and chat
    id per task; when it names a chat, the token must be that chat's. No chat
    bound (CLI, cron, an older core) leaves the token as the only check.
    """
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return False
    chat = (get_session_env("HERMES_SESSION_CHAT_ID") or "").strip()
    if not chat:
        return False
    platform = (get_session_env("HERMES_SESSION_PLATFORM") or "").strip()
    own = getattr(adapter.platform, "value", str(adapter.platform))
    return platform != own or chat != str(real_chat_id)


_WRONG_CHAT_ERROR = json.dumps(
    {
        "error": (
            "This chat_token belongs to a different conversation — use the "
            "[dc:chat=...] value from the current message"
        )
    }
)


def _contact_name(contact: dict, fallback: str) -> str:
    """Best display name from a ``get_contact`` snapshot."""
    return (
        contact.get("name")
        or contact.get("display_name")
        or contact.get("name_and_addr")
        or fallback
    )


class _RateLimiter:
    """Simple sliding-window rate limiter keyed by arbitrary strings."""

    def __init__(self, max_calls: int = 30, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        self._buckets: Dict[str, deque] = {}
        self._lock = threading.Lock()

    # Sweep idle senders only once this many are tracked (an O(n) pass then).
    _SWEEP_ABOVE = 1024

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            # why: one bucket per sender address, kept forever, grew without
            # bound under an open policy. A bucket idle for a whole window
            # carries no state worth keeping.
            if len(self._buckets) > self._SWEEP_ABOVE:
                self._buckets = {
                    k: b
                    for k, b in self._buckets.items()
                    if b and now - b[-1] <= self.window
                }
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

# Methods the RPC tools refuse, beyond the delete_*/remove_* prefix rule.
# Each is here for what it does, not for what it is called.
_BLOCKED_METHODS = frozenset(
    {
        # destroys or detaches
        "leave_group",
        "set_chat_ephemeral_timer",  # timed deletion: delete_messages by another name
        # reaches outside the chat the token scopes
        "forward_messages",  # messageIds are global: copies out of any other chat
        "add_contact_to_chat",
        # hands out credentials: the QR text *is* the group invite
        "get_chat_securejoin_qr_code",
        "get_chat_securejoin_qr_code_svg",
        # leaks the device, not the chat
        "send_locations_to_chat",
        # hides the conversation from the bot's own operator
        "set_chat_mute_duration",
        "set_chat_visibility",
        "block_chat",
        # reachable by a better route, or not ours to touch
        "place_outgoing_call",  # dc_start_call handles the opening line
        "init_webxdc_integration",
    }
)


def _is_blocked(method: str) -> bool:
    """Whether the RPC tools refuse *method*.

    The prefix half is open-ended on purpose: a delete_* method added by a
    future core is blocked the day it appears. This is a *name* rule, so it
    bounds names, not capabilities (set_config can still wipe messages via
    delete_device_after) — DELTACHAT_RAW_RPC_ALLOWLIST is the real control
    for dc_rpc_call. File paths are a parameter problem and are checked in
    the safe call handler.
    """
    return method in _BLOCKED_METHODS or method.startswith(("delete_", "remove_"))


def _parse_method_list(value: Optional[str]) -> frozenset:
    """Parse a comma-separated list of RPC method names into a set."""
    if not value:
        return frozenset()
    return frozenset(m.strip() for m in value.split(",") if m.strip())


# Spec parameter names that carry a local filesystem path for core to read.
# send_msg's path is nested as data.file and handled separately.
_PATH_PARAMS = frozenset({"file", "imagePath", "stickerPath"})
# Names that look like paths but are not: filename is the display name only.
_NOT_PATH_PARAMS = frozenset({"filename"})


def _unchecked_path_name(names) -> Optional[str]:
    """First name that looks like a path but has no handling here, else None.

    why: _PATH_PARAMS matches today's spec by name, and the spec is fetched
    from whatever core is installed. A future core that renames `file` or
    adds a `filePath` would otherwise slip past unchecked; refusing unknown
    path-shaped names fails closed instead.
    """
    for name in names:
        lowered = name.lower()
        if (
            ("file" in lowered or "path" in lowered)
            and name not in _PATH_PARAMS
            and name not in _NOT_PATH_PARAMS
        ):
            return name
    return None


def _protected_dirs(adapter) -> list:
    """Directories whose contents must never be sent: Delta Chat state and logs.

    Covers every profile of this Hermes root, not just ours: a bot steered in
    profile A could otherwise send profile B's dc.db. Hermes' own helper
    enumerates those homes; older cores lack it, so fall back to ours.
    """
    from gateway.config import get_hermes_home

    try:
        from gateway.platforms.base import _credential_home_roots

        homes = [str(h) for h in _credential_home_roots()]
    except Exception:
        homes = []
    homes.append(str(get_hermes_home()))
    dirs = [adapter._get_dc_config_dir()]
    for home in homes:
        # "deltachat" is the v1.6.x data dir (see _default_dc_data_dir).
        dirs += [
            os.path.join(home, d) for d in ("deltachat-platform", "deltachat", "logs")
        ]
    return dirs


def _is_inside(path: str, dirs) -> bool:
    """True if *path* or any parent is one of *dirs*, compared by inode.

    why: inode, not string prefix. On a case-insensitive filesystem (macOS)
    realpath keeps the caller's spelling, so ~/.HERMES/deltachat-platform/
    opens the same files while missing a prefix match.
    """
    ids = set()
    for d in dirs:
        try:
            st = os.stat(d)
        except OSError:
            continue
        ids.add((st.st_dev, st.st_ino))
    current = os.path.realpath(path)
    while True:
        try:
            st = os.stat(current)
            if (st.st_dev, st.st_ino) in ids:
                return True
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _safe_delivery_path(adapter, path) -> Optional[str]:
    """The validated host path for *path*, or None if delivery policy refuses it.

    Hermes' policy denylists its own secrets (.env, state.db, ~/.ssh) but
    knows nothing about ours: the Delta Chat account dir holds dc.db (PGP
    secret key, mail password, every chat) and the logs carry message
    content. Refuse both on top of Hermes' check.
    """
    if not isinstance(path, str):
        return None
    safe = adapter.filter_local_delivery_paths([path])
    if not safe or _is_inside(safe[0], _protected_dirs(adapter)):
        return None
    return safe[0]


def _refuse_path(method: str, path) -> str:
    # why: %r — the path is model-supplied; a newline must not forge a log line.
    logger.warning("Safe RPC call %r REFUSED (unsafe file path): %r", method, path)
    return json.dumps(
        {
            "error": (
                f"'{method}': file path refused — it does not exist on this host "
                "or lies under a location the delivery policy protects"
            )
        }
    )


# Cached OpenRPC spec (fetched lazily on first use).
_spec_cache: Optional[dict] = None
# why threading, not asyncio: Hermes runs async tool handlers on their own
# event loop in a worker thread (model_tools._run_async), while message
# handling runs on the gateway loop. An asyncio.Lock shared between loops
# raises or strands its waiter the first time it is contended. The sections
# it guards are plain dict access with no await inside.
_token_lock = threading.Lock()


async def _get_or_create_chat_token(rpc, account_id: int, chat_id: int) -> str:
    """Return a stable opaque token for *chat_id*.

    Checks memory cache first, then DC UI config (persists across restarts),
    creating and storing a new token if none exists yet.
    """
    with _token_lock:
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

    with _token_lock:
        _chat_id_to_token[chat_id] = token
        _chat_token_to_id[token] = chat_id
    return token


def _quote_id(reply_to) -> Optional[int]:
    """DC message id to quote, or None when reply_to is not a real DC message.

    why: Hermes anchors replies on the triggering event's message_id, and our
    synthetic events (call notes) carry non-numeric ids; int() on those failed
    the whole send instead of just sending it unquoted.
    """
    s = str(reply_to or "").strip()
    return int(s) if s.isdigit() else None


async def _resolve_chat_token(rpc, account_id: int, token: str) -> Optional[int]:
    """Resolve an opaque token back to the real chat_id.

    Checks memory cache first, then DC UI config as a fallback for
    tokens issued in a previous session.
    """
    with _token_lock:
        if token in _chat_token_to_id:
            return _chat_token_to_id[token]

    dc_key = f"ui.hermes.token_chat.{token}"
    try:
        chat_id_str = await rpc.get_config(account_id, dc_key)
    except Exception:
        chat_id_str = None

    if chat_id_str:
        chat_id = int(chat_id_str)
        with _token_lock:
            _chat_token_to_id[token] = chat_id
            _chat_id_to_token[chat_id] = token
        return chat_id

    return None


async def _fetch_spec() -> dict:
    """Fetch and cache the OpenRPC spec from deltachat-rpc-server --openrpc."""
    global _spec_cache
    if _spec_cache is not None:
        return _spec_cache
    # No lock: callers may be on different event loops (see _token_lock), and
    # two racing first calls only run `--openrpc` twice and cache the same spec.
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

    @property
    def enforces_own_access_policy(self) -> bool:
        """Declares this adapter's dm_policy/group_policy as gateway.authz_mixin's
        BasePlatformAdapter.enforces_own_access_policy contract expects.

        Core's ``_is_user_authorized`` only consults this (via
        ``getattr(adapter, "enforces_own_access_policy", False)``) as a
        fallback when NO env-based allowlist (DELTACHAT_ALLOWED_USERS,
        GATEWAY_ALLOWED_USERS, ...) is configured, and even then only trusts
        it when the adapter's effective policy for that chat type
        (``self._dm_policy`` / ``self._group_policy``, which core reads
        directly) is exactly "allowlist" — never "open" or "pairing", which
        would be the fail-open SECURITY.md forbids. Without this flag, an
        operator who configures `dm_allowed_users`/`group_allowed_users` in
        `config.yaml`'s `extra:` block (rather than the
        DELTACHAT_ALLOWED_USERS env var) gets denied by core regardless —
        core has no way to know this adapter already gated it.
        """
        return True

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
        self._fatal_notify_task: Optional[asyncio.Task] = None
        # why: _running is *shared* with BasePlatformAdapter — it is both this
        # listener's loop condition and the base's `is_connected`. That is what
        # makes the escalation guard in _event_listener correct (the loop can
        # only go false via a deliberate teardown), so it cannot be stopped
        # without also declaring the adapter disconnected.
        self._running = False
        self._dc_config_dir: Optional[str] = None
        self._call_manager = None

        cfg = functools.partial(_cfg, config)
        cfg_bool = functools.partial(_cfg_bool, config)

        self._allow_all = cfg_bool("DELTACHAT_ALLOW_ALL_USERS", "allow_all_users")
        self._allowed_users = (
            set()
            if self._allow_all
            else _parse_email_list(cfg("DELTACHAT_ALLOWED_USERS", "allowed_users"))
        )

        self._dm_policy = cfg("DELTACHAT_DM_POLICY", "dm_policy", "pairing")
        self._dm_allow_from = _parse_email_list(
            cfg("DELTACHAT_DM_ALLOWED_USERS", "dm_allowed_users")
        )

        self._group_policy = cfg("DELTACHAT_GROUP_POLICY", "group_policy", "open")
        self._group_allow_from = _parse_email_list(
            cfg("DELTACHAT_GROUP_ALLOWED_USERS", "group_allowed_users")
        )

        self._send_rejection_replies = cfg_bool(
            "DELTACHAT_SEND_REJECTION_REPLIES", "send_rejection_replies", "true"
        )

        self._seen_ids = _MessageCache(max_size=1000)
        # why bounded: int()/float() on a typo raised out of __init__, and a
        # max of 0 or less made deque(maxlen=...) admit one message then none.
        self._rate_limiter = _RateLimiter(
            max_calls=_cfg_bounded_int(
                config, "DELTACHAT_RATE_LIMIT_MAX", "rate_limit_max", 30, 1, 100000
            ),
            window_seconds=_cfg_bounded_int(
                config, "DELTACHAT_RATE_LIMIT_WINDOW", "rate_limit_window", 60, 1, 86400
            ),
        )

        self._max_message_len = _cfg_bounded_int(config, *_MAX_LEN_CFG)
        self._max_message_lines = _cfg_bounded_int(config, *_MAX_LINES_CFG)

        self._require_mention = cfg_bool("DELTACHAT_REQUIRE_MENTION", "require_mention")
        self._free_response_channels = set(
            _parse_csv_unique(
                cfg("DELTACHAT_FREE_RESPONSE_CHANNELS", "free_response_channels")
            )
        )
        # Inverse of free_response_channels: when require_mention is off
        # (free response by default), these chat IDs opt back into mention
        # gating instead — e.g. a noisy multi-agent group that should stay
        # conversational vs. a support/ops group that shouldn't.
        raw_require_mention_channels = cfg(
            "DELTACHAT_REQUIRE_MENTION_CHANNELS", "require_mention_channels"
        )
        self._require_mention_channels = set(
            _parse_csv_unique(raw_require_mention_channels)
        )

        # Guards against bot-to-bot auto-reply loops (e.g. multiple agents in
        # one group replying to each other forever). <=0 disables the guard.
        self._max_consecutive_replies = _cfg_bounded_int(
            config,
            "DELTACHAT_MAX_CONSECUTIVE_REPLIES",
            "max_consecutive_replies",
            20,
            -1,
            100000,
        )
        self._reply_streak: dict[str, tuple[str, int, bool]] = {}

        # Caps total bot-to-bot messages in a chat (any senders, not just one
        # repeat offender) before requiring a check-in from a configured human
        # address. Only active when DELTACHAT_HUMAN_USERS is set — with no
        # human addresses configured there is no way to detect a "check-in",
        # so the guard stays off rather than trip on every message.
        self._human_users = _parse_email_list(
            cfg("DELTACHAT_HUMAN_USERS", "human_users")
        )
        self._max_bot_exchanges = _cfg_bounded_int(
            config, "DELTACHAT_MAX_BOT_EXCHANGES", "max_bot_exchanges", 12, -1, 100000
        )
        self._bot_exchange_streak: dict[str, tuple[int, bool]] = {}

        # Onboarding / profile settings
        self._email = cfg("DELTACHAT_EMAIL", "email", "auto").strip() or "auto"
        self._password = cfg("DELTACHAT_PASSWORD", "password") or None
        self._display_name = cfg("DELTACHAT_DISPLAY_NAME", "display_name", "Hermes")
        self._mention_aliases = _parse_csv_unique(
            cfg("DELTACHAT_MENTION_ALIASES", "mention_aliases")
        )
        self._mention_patterns = [
            p
            for p in map(
                _build_mention_pattern, (self._display_name, *self._mention_aliases)
            )
            if p is not None
        ]
        # why opt-in: core sends the bio as the signature of every outgoing
        # message, so the command list costs ~5 KB per reply.
        self._commands_bio_enabled = cfg_bool("DELTACHAT_COMMANDS_BIO", "commands_bio")
        self._avatar_path = _validate_avatar_path(
            cfg("DELTACHAT_AVATAR_PATH", "avatar_path") or None, strict=False
        )
        self._data_dir = cfg("DELTACHAT_DATA_DIR", "data_dir") or None

        chatmail_servers = cfg(
            "DELTACHAT_CHATMAIL_SERVERS", "chatmail_servers"
        ) or os.getenv("DELTACHAT_CHATMAIL_SERVER", "nine.testrun.org")
        self._chatmail_servers = _parse_csv_unique(chatmail_servers)

        # Runtime state for observability and crash recovery
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._crash_times: list[float] = []
        self._stats: dict[str, int] = {}
        self._lock = threading.RLock()
        self._self_addr: Optional[str] = None
        self._invite_link: Optional[str] = None
        # Exec-approval prompt msg id -> (Hermes session key, request_id),
        # oldest first. Only prompts whose request_id is known are here.
        self._approval_prompts: Dict[int, tuple] = {}
        self._background_tasks: set = set()
        self._roster_cache: dict[str, tuple[float, list]] = {}

    def _spawn(self, coro) -> asyncio.Task:
        """create_task with a strong reference held until the task is done.

        why: the event loop keeps only a weak reference to a task, so a
        fire-and-forget one (answering a call, speaking a reply) can be
        garbage-collected mid-flight and simply never finish.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _bump_stat(self, key: str, count: int = 1) -> None:
        """Increment an internal counter under the adapter lock."""
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + count

    # Group rosters change rarely; a plain TTL avoids an RPC round-trip per
    # message. ponytail: no invalidation on membership-change events — if
    # rosters go stale in practice, listen for DC's ChatModified event instead.
    _ROSTER_CACHE_TTL = 300

    async def _get_group_roster(self, chat_id: Any) -> list[dict]:
        """Return [{"name", "address"}, ...] for a group's members, excluding self.

        Cached per chat_id for _ROSTER_CACHE_TTL seconds to avoid an RPC
        round-trip on every inbound message.
        """
        key = str(chat_id)
        now = time.monotonic()
        cached = self._roster_cache.get(key)
        if cached and now - cached[0] < self._ROSTER_CACHE_TTL:
            return cached[1]
        try:
            contact_ids = await self.rpc.get_chat_contacts(
                self.account_id, int(chat_id)
            )
        except Exception as e:
            logger.debug("Could not fetch group roster for chat %s: %s", chat_id, e)
            return cached[1] if cached else []
        roster = []
        for cid in contact_ids:
            if cid == DC_CONTACT_ID_SELF:
                continue
            try:
                contact = await self.rpc.get_contact(self.account_id, int(cid))
            except Exception as e:
                logger.debug("Could not fetch contact %s for roster: %s", cid, e)
                continue
            roster.append(
                {
                    "name": (
                        contact.get("name")
                        or contact.get("display_name")
                        or contact.get("address")
                        or f"Contact {cid}"
                    ),
                    "address": contact.get("address") or "",
                }
            )
        self._roster_cache[key] = (now, roster)
        return roster

    def _is_address_in_known_rosters(self, address: str) -> bool:
        """Whether *address* appears in any group roster this bot has already fetched.

        Used to guard cold-DM sends (dc_send_message's ``address`` param): an
        address is only reachable if it's a member of a group this bot
        participates in, not an arbitrary Delta Chat address.
        """
        address = address.lower()
        return any(
            any(contact.get("address", "").lower() == address for contact in roster)
            for _, roster in self._roster_cache.values()
        )

    def _message_metadata(
        self,
        chat_id: Any,
        msg_id: Any,
        from_id: Any,
        is_group: bool,
        token: Optional[str] = None,
        roster: Optional[list] = None,
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
        if is_group and roster is not None:
            meta["participants"] = roster
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

    def _on_allowlist(self, sender_email: str, scoped: set) -> bool:
        """Whether *sender_email* passes an ``allowlist`` policy.

        *scoped* is dm_allowed_users / group_allowed_users. Left empty, the
        policy falls back to the global allowed_users, which _gate_inbound has
        already enforced. why the last line: with neither list set (and no
        allow_all_users) an "allowlist" that names nobody used to admit
        everybody — and Hermes core trusts an adapter under an allowlist
        policy without checking again. Naming nobody now admits nobody.
        """
        if scoped:
            return sender_email in scoped
        return self._allow_all or bool(self._allowed_users)

    def _check_dm(self, sender_email: str, is_verified: bool) -> Optional[str]:
        if self._dm_policy == "disabled":
            return "Sorry, this bot does not accept direct messages."
        if self._dm_policy == "pairing" and not is_verified:
            return "I only chat with verified contacts. Scan my QR code to connect securely."
        if self._dm_policy == "allowlist" and not self._on_allowlist(
            sender_email, self._dm_allow_from
        ):
            return "Sorry, you are not on the allowed list for direct messages."
        return None

    def _check_group(self, sender_email: str) -> Optional[str]:
        if self._group_policy == "disabled":
            return "Sorry, this bot does not respond in group chats."
        if self._group_policy == "allowlist" and not self._on_allowlist(
            sender_email, self._group_allow_from
        ):
            return "Sorry, you are not authorized for group interactions."
        return None

    def _is_mentioned(self, text: str) -> bool:
        """Return True if the message text @mentions the bot by display name.

        Requires a leading "@" — a bare display name in prose (e.g. "napis
        Alici", asking someone else to message Alice) is about the bot, not
        addressed to it. Case-insensitive, tolerant of short case-ending
        variation (e.g. Czech declension: "@Alici" mentions display_name
        "Alice"). Also checks any configured DELTACHAT_MENTION_ALIASES.
        Substrings like `@Hermesss` (more than ~2 extra trailing characters)
        do not count.
        """
        if not text or not self._mention_patterns:
            return False
        return any(p.search(text) for p in self._mention_patterns)

    def _command_for_us(self, text: str, addr_start: int) -> Optional[str]:
        """For "/cmd@<name> args": "/cmd args" if <name> is this bot, else None.

        *addr_start* is the index right after the "@". Exact (case-insensitive)
        name or alias only — an address is typed deliberately, so the
        declension tolerance of prose @mentions does not apply.
        """
        rest = text[addr_start:]
        names = [n for n in (self._display_name, *self._mention_aliases) if n]
        # why: longest first, so with names "Hermes" and "Hermes Bot",
        # "/reset@Hermes Bot" strips the whole name instead of leaving " Bot".
        for name in sorted(names, key=len, reverse=True):
            m = re.match(re.escape(name) + r"(?![\w-])", rest, re.IGNORECASE)
            if m:
                return text[: addr_start - 1] + rest[m.end() :]
        return None

    async def _quote_is_self_authored(self, quote: dict) -> bool:
        """Whether a ``WithMessage`` quote points at one of this bot's own messages.

        A quote-reply to the bot's own message is treated as an implicit
        mention (keeps a thread going under mention-gating). The reliable
        signal is the quoted message's ``from_id == DC_CONTACT_ID_SELF`` —
        core reports ``author_display_name`` for a self-authored quote as the
        localized "Me" string ("Me"/"Ich"/"Já"...), never the configured
        displayname, so a name comparison alone misses every reply-to-self.
        Falls back to the name match only when the quoted message can't be
        fetched locally (e.g. not downloaded).
        """
        msg_id = quote.get("message_id")
        if msg_id:
            try:
                quoted = await self.rpc.get_message(self.account_id, int(msg_id))
                if quoted and quoted.get("from_id") == DC_CONTACT_ID_SELF:
                    return True
            except Exception as e:
                logger.debug("Could not fetch quoted message %s: %s", msg_id, e)
        name = (quote.get("author_display_name") or "").strip().lower()
        return bool(
            name and self._display_name and name == self._display_name.strip().lower()
        )

    async def _check_mention(self, text: str, chat_type: str, chat_id: str) -> bool:
        """Drop group messages that do not mention the bot when required.

        Silently ignored — no rejection reply is sent here. In a multi-bot
        group every bot enforces this independently, so a "please mention me"
        notice would fire once per bot per unmentioned message; silence is
        the only option that doesn't spam the chat.

        Two modes, selected by the global require_mention default:
        - require_mention=true (legacy): every group is gated, except chat
          IDs listed in free_response_channels.
        - require_mention=false (default): every group responds freely,
          except chat IDs listed in require_mention_channels, which stay
          gated.

        Returns True if the message should be processed.
        """
        if chat_type != "group":
            return True
        if self._require_mention:
            if str(chat_id) in self._free_response_channels:
                return True
        else:
            if str(chat_id) not in self._require_mention_channels:
                return True
        # why: an empty body (captionless image/file/voice) has no mention, so it
        # must NOT be exempt — only slash commands bypass the gate.
        if text and text.startswith("/"):
            return True
        if self._is_mentioned(text):
            return True

        logger.debug("Ignoring unmentioned group message in chat %s", chat_id)
        self._bump_stat("messages_rejected")
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

    def _check_bot_exchange_guard(
        self, chat_id, sender_email: str, is_bot: Optional[bool] = None
    ) -> tuple[bool, bool]:
        """Cap total bot-to-bot messages in a chat, regardless of who's sending.

        Unlike _check_loop_guard (which only catches one sender flooding),
        this catches 3+ bots round-robining a group — from each bot's own
        view the sender keeps changing, so the same-sender streak never
        trips. Every bot message counts toward DELTACHAT_MAX_BOT_EXCHANGES; a
        human message resets the count. Human = a DELTACHAT_HUMAN_USERS address,
        or any contact core reports as not ``is_bot`` — so groups need no
        per-user list. With ``is_bot`` unknown (None) only the list decides,
        and the guard stays off when the list is empty.

        Returns (should_process, should_warn) — should_warn is True only the
        first time a given streak trips.
        """
        if self._max_bot_exchanges <= 0 or (is_bot is None and not self._human_users):
            return True, False
        # why: a contact not flagged is_bot is a human even if not listed —
        # requiring every group member in HUMAN_USERS defeats using groups.
        is_human = sender_email in self._human_users or is_bot is False
        key = str(chat_id)
        with self._lock:
            count, warned = self._bot_exchange_streak.get(key, (0, False))
            if is_human:
                count, warned = 0, False
            else:
                count += 1
            tripped = count > self._max_bot_exchanges
            should_warn = tripped and not warned
            self._bot_exchange_streak[key] = (count, warned or should_warn)
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
        verified_field = None
        if from_id:
            try:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
            except Exception as e:
                # Fail-closed: under an open policy an unidentifiable sender
                # used to be let through with an empty address.
                logger.warning(
                    "Dropping message %s: could not load sender %s: %s",
                    msg_id,
                    from_id,
                    e,
                )
                return False
            # why: identity in Delta Chat is the key. A sender without one is
            # plain unencrypted mail, whose From address anyone can forge — and
            # allowed_users / the allowlists match on that address. Dropped
            # silently and unread, like upstream 2.0.0.
            if contact.get("is_key_contact") is False:
                logger.debug(
                    "Dropping message %s from contact %s: no key", msg_id, from_id
                )
                self._bump_stat("keyless_messages_dropped")
                return False
            sender_email = (contact.get("address") or "").lower()
            verified_field = contact.get("is_verified")

        if sender_email and not self._rate_limiter.is_allowed(sender_email):
            logger.warning("Rate limit exceeded for %s", sender_email)
            self._bump_stat("messages_rate_limited")
            return False

        if self._allowed_users and sender_email not in self._allowed_users:
            logger.warning("Rejected %s (not in allowed_users)", sender_email)
            return await self._reject(
                chat_id, "Sorry, you are not authorized to use this bot."
            )

        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
        except Exception as e:
            logger.warning("Could not fetch chat %s: %s", chat_id, e)
            return False
        chat_type = chat.get("chat_type")
        is_request = bool(chat.get("is_contact_request"))

        if chat_type == "Single":
            reason = await self._dm_rejection(from_id, sender_email, verified_field)
            if reason:
                logger.warning("dm_policy rejected %s", sender_email)
                return await self._reject(chat_id, reason)
        elif chat_type == "Group":
            reason = self._check_group(sender_email)
            if reason:
                logger.warning("group_policy rejected %s", sender_email)
                if not is_request:
                    return await self._reject(chat_id, reason)
                # An unaccepted invite: leave instead of replying into it.
                try:
                    await self.rpc.leave_group(self.account_id, int(chat_id))
                except Exception as e:
                    logger.warning("leave_group failed: %s", e)
                return await self._reject(chat_id, None)

        if is_request and chat_type in ("Single", "Group"):
            try:
                await self.rpc.accept_chat(self.account_id, int(chat_id))
            except Exception as e:
                logger.warning("accept_chat failed: %s", e)

        self._bump_stat("messages_received")
        return True

    async def _dm_rejection(
        self, from_id, sender_email, verified_field
    ) -> Optional[str]:
        """Why dm_policy refuses this contact (message or call), or None."""
        # why: core >= 2.6x dropped Contact.is_verified, so the key is absent
        # (None), not False. Only then require the SecureJoin-completion
        # marker recorded by _record_securejoin_pairing. A present
        # is_verified (older core) stays authoritative. Fail-closed.
        if verified_field is not None:
            is_paired = bool(verified_field)
        else:
            is_paired = bool(from_id) and await self._is_securejoin_paired(from_id)
        return self._check_dm(sender_email, is_paired)

    async def _sender_allowed(
        self, from_id, chat_type: str, chat_id, *, strict: bool = False
    ) -> bool:
        """Sender policy for events that are not messages (calls, reactions).

        The rules _gate_inbound applies, minus its side effects (no dedup,
        rate limit or rejection reply), then Hermes' own verdict. Fail-closed
        on a contact we cannot load.

        strict (reactions that approve a command): the contact must be a key
        contact — an address is spoofable, a key is not — and Hermes must say
        yes. Otherwise only Hermes' explicit no refuses; None ("no check
        wired", older cores) is not a verdict.
        """
        if not from_id:
            return False
        try:
            contact = await self.rpc.get_contact(self.account_id, int(from_id))
        except Exception as e:
            logger.warning("Could not load contact %s: %s", from_id, e)
            return False
        if contact.get("is_key_contact") is False:
            return False  # an address is forgeable, a key is not (see _gate_inbound)
        email = (contact.get("address") or "").lower()
        if self._allowed_users and email not in self._allowed_users:
            return False
        if chat_type == "group":
            reason = self._check_group(email)
        else:
            reason = await self._dm_rejection(
                from_id, email, contact.get("is_verified")
            )
        if reason:
            return False
        core_check = getattr(self, "_is_sender_authorized", None)
        verdict = (
            core_check(str(from_id), chat_type, str(chat_id)) if core_check else None
        )
        if strict:
            return bool(contact.get("is_key_contact")) and verdict is True
        return verdict is not False

    async def _caller_allowed(self, from_id, chat_id) -> bool:
        """Whether an incoming call may be answered: a call is a DM.

        Checked before answering — that sets up WebRTC and loads STT before
        Hermes ever sees (and drops) what an unauthorized caller says.
        """
        return await self._sender_allowed(from_id, "dm", chat_id)

    async def _reject(self, chat_id, reply: Optional[str]) -> bool:
        """Count a rejected inbound message and send *reply* if configured.

        Always returns False so callers can ``return await self._reject(...)``.
        """
        self._bump_stat("messages_rejected")
        if reply and self._send_rejection_replies:
            await self.send(str(chat_id), reply)
        return False

    def _get_dc_config_dir(self) -> str:
        """Get Delta Chat config directory path.

        Uses DELTACHAT_DATA_DIR if set, otherwise falls back to
        <HERMES_HOME>/deltachat-platform/ (or <HERMES_HOME>/deltachat/ from
        a v1.6.x install, if that's where an existing account already lives
        — see _default_dc_data_dir). The directory is created with
        restrictive permissions when first accessed.
        """
        if self._dc_config_dir is None:
            if self._data_dir:
                path = self._data_dir
            else:
                path = _default_dc_data_dir()
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

        Failures are logged but do not abort the connection. displayname and
        bot are skipped when already set to the target value — this runs on
        every connect() including reconnects, and re-setting an unchanged
        displayname causes DC core to gossip an updated Autocrypt header to
        1:1 chat partners, which their clients surface as a "verification
        changed" system message on every gateway restart even though nothing
        actually changed (#observed: banner on every restart, no re-pairing
        actually required).
        """
        try:
            current_name = await rpc.get_config(account_id, "displayname")
            if current_name != self._display_name:
                await rpc.set_config(account_id, "displayname", self._display_name)
                logger.debug("Set display name to %r", self._display_name)
        except Exception as e:
            logger.warning("Could not set display name: %s", e)

        try:
            current_bot = await rpc.get_config(account_id, "bot")
            if current_bot != "1":
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

    async def _update_commands_bio(self) -> None:
        """Write the command list into the profile bio, or take it out when disabled."""
        try:
            current = await self.rpc.get_config(self.account_id, "selfstatus") or ""
            own = _own_bio(current)
            if self._commands_bio_enabled:
                bio = _commands_bio(
                    current.strip() if own is None else own, self.config.extra or {}
                )
            elif own is not None:
                # Turned off again: take the list back out.
                bio = "" if own == _BIO_DEFAULT_INTRO else own
            else:
                bio = None
            # Only on change: a write is synced to the account's other devices.
            if bio is not None and bio != current:
                await self.rpc.set_config(self.account_id, "selfstatus", bio)
                logger.info("Updated the command list in the profile bio")
        except Exception as e:
            logger.warning("Could not update the commands bio: %s", e)

    def _write_invite_file(self, link: str) -> None:
        """Persist the SecureJoin invite link to a 0600 file in the accounts dir.

        why a file: with headless onboarding nobody watches setup.py's
        terminal, and under dm_policy=pairing whoever holds the link can reach
        the agent — so it must not go to the world-readable gateway log at
        INFO. The log only names the file.
        """
        if not link:
            return
        path = os.path.join(self._get_dc_config_dir(), "invite.txt")
        try:
            # os.open with an explicit mode: no window where the link sits in
            # a umask-default (usually 0644) file.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(link + "\n")
            os.chmod(path, 0o600)  # O_CREAT's mode is ignored for an existing file
        except OSError as e:
            # Nothing to point at, so the link itself is the only way to pair.
            logger.warning(
                "Could not write invite link to %s (%s). Invite link: %s", path, e, link
            )
            return
        logger.info("Delta Chat invite link written to %s", path)

    async def _check_db_id(self) -> bool:
        """Refuse to run on a Delta Chat database Hermes' state doesn't belong to.

        Hermes keys pairing approvals, sessions, the home channel and cron
        targets on DC contact and chat IDs, which are local to one DC database.
        If that database is lost and recreated, the IDs are handed out again —
        an approved contact 10 can now be a stranger, inheriting the access and
        the conversation history. So the same random ID is kept in the DC
        config and in a dotfile in HERMES_HOME (next to Hermes' state, not
        inside the accounts dir, so resetting the database can't take it
        along); on mismatch we stop instead of guessing.

        Neither side having an ID is a fresh install *or* an upgrade from
        before this check: both just adopt a new one. With self.account_id
        None (no DC account yet) only a mismatch is detected, so onboarding
        doesn't create an account first.
        """
        from gateway.config import get_hermes_home

        marker = os.path.join(str(get_hermes_home()), ".deltachat-db-id")
        dc_id = None
        if self.account_id is not None:
            dc_id = await self.rpc.get_config(self.account_id, _DB_ID_KEY) or None
        try:
            try:
                with open(marker) as f:
                    hermes_id = f.read().strip() or None
            except FileNotFoundError:
                hermes_id = None
            if hermes_id is None:
                if self.account_id is None:
                    return True
                if dc_id is None:
                    # DC first: dying before the file is written leaves the
                    # adoptable state below, never a refusing one.
                    dc_id = str(uuid.uuid4())
                    await self.rpc.set_config(self.account_id, _DB_ID_KEY, dc_id)
                    self._warn_if_already_paired()
                # else: only the marker is gone (deleted by hand, as the
                # recovery below says) — adopt the database's ID.
                tmp = marker + ".tmp"
                with open(tmp, "w") as f:
                    f.write(dc_id + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, marker)
                return True
        except (OSError, UnicodeDecodeError) as e:
            message = (
                f"Cannot read or write the Delta Chat database marker {marker}: {e}"
            )
            logger.error(message)
            # Not retryable: needs the operator to fix permissions or the disk.
            self._set_fatal_error("deltachat_db_marker_io", message, retryable=False)
            return False

        if dc_id == hermes_id:
            return True

        message = (
            f"The Delta Chat database does not belong to this Hermes state "
            f"(ID {dc_id or 'missing'} in the database, {hermes_id} in {marker}). "
            "It was probably recreated, so its contact and chat IDs now mean "
            "different people. Refusing to start: Hermes' pairing approvals, "
            "sessions, DELTACHAT_HOME_CHANNEL and cron delivery targets would "
            "apply to the wrong contacts. To start over on this database "
            "(add `-p <profile>` if this isn't the default profile): revoke the "
            "deltachat-platform approvals (`hermes pairing list`, `hermes "
            "pairing revoke deltachat-platform <id>`) and remove Delta Chat IDs "
            "from GATEWAY_ALLOWED_USERS; delete its sessions (`hermes sessions "
            "prune --source deltachat-platform --include-pinned "
            "--include-archived`, then `hermes sessions delete <id>` for each "
            "one still listed); unset DELTACHAT_HOME_CHANNEL and fix cron jobs "
            f"that deliver to Delta Chat; then delete {marker} and restart."
        )
        logger.error(message)
        # Not retryable: a reconnect would find the same mismatch.
        self._set_fatal_error("deltachat_db_mismatch", message, retryable=False)
        return False

    @staticmethod
    def _warn_if_already_paired() -> None:
        """On upgrade, note that existing approvals were trusted unverified."""
        try:
            from gateway.pairing import PairingStore

            approved = PairingStore().list_approved("deltachat-platform")
        except Exception:
            return
        if approved:
            logger.warning(
                "Delta Chat database ID created with %d pairing approval(s) "
                "already present. They are assumed to belong to this database; "
                "if it was recreated earlier, check `hermes pairing list`.",
                len(approved),
            )

    async def _configure_account(self, rpc) -> bool:
        """Select an existing account or create and configure a new one."""
        accounts = await rpc.get_all_accounts()
        if accounts:
            self.account_id = accounts[0]["id"]
            addr = await rpc.get_config(self.account_id, "addr")
            kind = accounts[0].get("kind")
            if addr or kind == "Configured":
                logger.info("Using existing Delta Chat account: %s", self.account_id)
                await self._apply_profile(rpc, self.account_id)
                # Existing accounts do not need the configured password.
                self._password = None
                return True

            # why: removing an account destroys its keys and every pairing, so
            # only do it when core itself says the account is unconfigured — a
            # missing "addr" config key alone (a renamed key on a newer core)
            # must never delete a working account.
            if kind != "Unconfigured":
                raise RuntimeError(
                    f"Delta Chat account {self.account_id} has no address but "
                    f"core reports it as {kind!r}; refusing to remove it"
                )
            # A cancelled provisioning run leaves an account record without a
            # transport or address. It cannot become valid merely by reconnecting.
            logger.warning(
                "Removing incomplete Delta Chat account %s before reprovisioning",
                self.account_id,
            )
            await rpc.remove_account(self.account_id)
            self.account_id = None

        # Before creating anything: an existing marker with no usable account
        # means the database was lost, and registering a new account would
        # bind it to the old Hermes state.
        if not await self._check_db_id():
            return False

        logger.info("No usable Delta Chat account found; creating one")
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
                # Delta Chat 2.59 deprecated the legacy configure-after-QR flow.
                await rpc.add_transport_from_qr(
                    self.account_id, f"DCACCOUNT:https://{server}/new"
                )
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

            if not await self._check_db_id():
                self._cleanup()
                return False

            await self._update_commands_bio()

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
                self._write_invite_file(link)
            except Exception as e:
                logger.warning("Could not generate SecureJoin invite link: %s", e)
                self._invite_link = None

            # Register graceful shutdown signals.
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    self._loop.add_signal_handler(sig, self._signal_handler)
            except (NotImplementedError, ValueError, RuntimeError):
                pass  # Signals may not be supported on this platform.

            # Start the event listener. It escalates its own death to the
            # gateway (see _event_listener); Hermes owns supervision and rebuilds
            # a fresh adapter on a retryable fatal error, so there is no
            # adapter-side restart loop to race that watcher.
            self._running = True
            self._event_loop_task = asyncio.create_task(self._event_listener())
            # Retrieve the task's exception if it ever escapes, so a crash is
            # logged when it happens rather than surfacing as "Task exception
            # was never retrieved" whenever the GC gets to it.
            self._event_loop_task.add_done_callback(self._on_listener_done)

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

    @staticmethod
    def _on_listener_done(task: asyncio.Task) -> None:
        """Retrieve the listener task's outcome so an escaped crash can't vanish.

        Without this, an exception escaping the task is only reported by asyncio
        as "Task exception was never retrieved" whenever the garbage collector
        happens to get to it — if at all.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Delta Chat event listener died: %s", exc, exc_info=exc)

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
        # _cleanup() is the failure path out of connect() as well as part of
        # disconnect(); without this a failed connect leaves the previous
        # runtime status in place.
        self._mark_disconnected()

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

        try:
            if self._call_manager:
                await self._call_manager.teardown()
                self._call_manager = None
        except Exception as e:
            # A raising teardown used to skip _cleanup() entirely, leaking the
            # RPC subprocess and the accounts-dir lock — which then blocks the
            # replacement adapter the gateway builds on reconnect.
            logger.warning("DeltaChat: call manager teardown failed: %s", e)
        finally:
            self._cleanup()  # marks the adapter disconnected itself
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
        """The account's email address, or None.

        why not the SecureJoin link (which this used to prefer): the result is
        logged at INFO on connect and reported as ``account_addr``, and under
        dm_policy=pairing whoever holds that link can reach the agent. The
        link lives in invite.txt and get_status()["invite_link"] only.
        """
        if not self.rpc or not self.account_id:
            return None
        try:
            addr = await self.rpc.get_config(self.account_id, "addr")
            if not addr:
                info = await self.rpc.get_account_info(self.account_id)
                addr = (info or {}).get("addr")
            return addr or None
        except Exception as e:
            logger.debug("Failed to get account address: %s", e)
            return None

    async def _resolve_chat_id(self, chat_id) -> int:
        """Real DC chat id for an outbound target: a numeric id or a chat token.

        why: the agent sees only the [dc:chat=<token>] tag, so when it writes a
        delivery target itself (a cron job's `deliver: deltachat-platform:<x>`)
        it uses the token. Hermes passes that through verbatim as chat_id.
        """
        s = str(chat_id).strip()
        # token_hex(8) is 16 hex chars and can, rarely, be all digits.
        if not s.isdigit() or len(s) == 16:
            real = await _resolve_chat_token(self.rpc, self.account_id, s)
            if real is not None:
                return real
        if not s.isdigit():
            raise ValueError(f"unknown Delta Chat chat id or token: {s!r}")
        return int(s)

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
        # Suppress the AI's reply to an internal "call ended" note so we don't
        # text the user a stray message after a call. Checked before call
        # routing so a late reply can't be spoken into a follow-up call.
        if self._call_manager and self._call_manager.is_call_end_reply(reply_to):
            return self._send_result(chat_id, None)

        if self._call_manager and self._call_manager.has_active_call(chat_id):
            thread_id = (metadata or {}).get("thread_id")
            if self._call_manager.is_call_thread(thread_id):
                # Only the turn's final reply is spoken. Hermes also routes
                # status traffic through send() — memory notices, tool
                # progress, busy acks, the "Working" heartbeat. Checked before
                # the call-ack drop so a status line can't use up that drop.
                if _BASE_MARKS_FINAL_REPLY and not (metadata or {}).get("notify"):
                    logger.debug(
                        "Call %s: not speaking non-final send: %r",
                        chat_id,
                        (content or "")[:80],
                    )
                    return self._send_result(chat_id, None)
                # Reply belongs to the call conversation — speak it into the call.
                # In shared-history mode the placing agent's "call connected" ack
                # also lands here (same session), so drop that one line.
                if self._call_manager.consume_call_ack(chat_id):
                    return self._send_result(chat_id, None)
                self._spawn(self._call_manager.play_response(chat_id, content))
                return self._send_result(chat_id, None)
            # Reply from the text/chat thread while a call is active (e.g. the
            # agent's "calling you now" line in separate-thread mode, or a
            # concurrent DM) — deliver it as a normal Delta Chat message instead
            # of speaking it into the call. Falls through to the normal send path.

        try:
            if not self.rpc or not self.account_id:
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            # Delta Chat renders plain text only; strip common markdown syntax.
            stripped = _strip_markdown(content)

            quoted_id = _quote_id(reply_to)
            real_chat_id = await self._resolve_chat_id(chat_id)

            async def _do_send() -> Optional[int]:
                from deltachat2.types import MsgData

                # Keep Delta Chat messages short and plain: over-limit
                # responses are split at paragraph/line boundaries and sent
                # as ordered plain-text chunks. Only the first chunk carries
                # the quote-reply.
                chunks = _split_message(
                    stripped, self._max_message_len, self._max_message_lines
                ) or [stripped]
                last_msg_id: Optional[int] = None
                for idx, chunk in enumerate(chunks):
                    chunk_quoted = quoted_id if idx == 0 else None
                    last_msg_id = await self.rpc.send_msg(
                        self.account_id,
                        real_chat_id,
                        MsgData(text=chunk, quoted_message_id=chunk_quoted),
                    )
                return last_msg_id

            msg_id = await _async_retry(_do_send, max_attempts=3, base_delay=1.0)
            logger.debug("Sent message %s to chat %s", msg_id, chat_id)
            self._bump_stat("messages_sent")
            return self._send_result(chat_id, msg_id)

        except Exception as e:
            logger.error("Error sending message to chat %s: %s", chat_id, e)
            self._bump_stat("messages_send_failed")
            return self._send_result(chat_id, None, error=str(e))

    async def _send_msg_data(
        self,
        chat_id: str,
        stat: str,
        what: str,
        reply_to: Optional[str] = None,
        **fields,
    ) -> SendResult:
        """Send one ``MsgData`` built from *fields*, with retry.

        Shared tail of the ``send_*`` attachment methods. *stat* prefixes the
        ``<stat>_sent`` / ``<stat>_send_failed`` counters; *what* names the
        payload in log lines.
        """
        try:
            if not self.rpc or not self.account_id:
                return self._send_result(
                    chat_id, None, error="Delta Chat not connected"
                )

            from deltachat2.types import MsgData

            msg_data = MsgData(quoted_message_id=_quote_id(reply_to), **fields)
            real_chat_id = await self._resolve_chat_id(chat_id)
            msg_id = await _async_retry(
                lambda: self.rpc.send_msg(self.account_id, real_chat_id, msg_data),
                max_attempts=2,
                base_delay=0.5,
            )
            logger.debug("Sent %s as message %s to chat %s", what, msg_id, chat_id)
            self._bump_stat(f"{stat}_sent")
            return self._send_result(chat_id, msg_id)
        except Exception as e:
            logger.error("Error sending %s to chat %s: %s", what, chat_id, e)
            self._bump_stat(f"{stat}_send_failed")
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
        return await self._send_msg_data(
            chat_id,
            "files",
            f"file {file_path}",
            reply_to,
            file=file_path,
            text=caption or "",
        )

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
        if not await asyncio.to_thread(_url_is_public, url):
            raise ValueError("Image URL points at a private or internal address")

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
            from deltachat2.types import MessageViewtype

            # Skip the download when disconnected; _send_msg_data reports that.
            if (
                self.rpc
                and self.account_id
                and image_path.startswith(("http://", "https://"))
            ):
                tmp_path = await self._download_image_url(image_path)
            return await self._send_msg_data(
                chat_id,
                "images",
                f"image {image_path}",
                reply_to,
                file=tmp_path or image_path,
                text=caption or "",
                viewtype=MessageViewtype.IMAGE,
            )
        except Exception as e:
            # Download/validation failures; _send_msg_data handles its own.
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

    # Delta Chat has no buttons: an exec-approval prompt is answered by reacting
    # to it. Compared with skin tones/variation selectors removed.
    _APPROVAL_REACTIONS = {"👍": "once", "👎": "deny"}
    _MAX_APPROVAL_PROMPTS = 64

    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        """Send Hermes' approval prompt and remember it for _handle_reaction.

        A reaction must only ever answer the approval its prompt shows, so the
        prompt offers reactions only when that approval's request_id is known;
        otherwise it's the plain /approve, /deny prompt.
        """
        try:
            request_id = self._pending_request_id(prompt)
        except Exception as e:
            logger.warning(
                "Can't tell which pending approval prompt %r is for, sending it "
                "without reactions: %s",
                prompt.command[:80],
                e,
            )
            request_id = None
        commands = ["/approve"] + [
            f"/approve {c}" for c in ("session", "always") if c in prompt.choices
        ]
        reply = f"{', '.join(commands)} or /deny."
        if request_id:
            text = (
                f"{prompt.text}\n\n"
                "React to this exact message:\n👍 = approve once\n👎 = deny\n\n"
                f"Or reply {reply}"
            )
        else:
            text = f"{prompt.text}\n\nReply {reply}"
        result = await self.send(prompt.chat_id, text, metadata=prompt.metadata)
        if result.success and result.message_id and request_id:
            self._approval_prompts[int(result.message_id)] = (
                prompt.session_key,
                request_id,
            )
            while len(self._approval_prompts) > self._MAX_APPROVAL_PROMPTS:
                del self._approval_prompts[next(iter(self._approval_prompts))]
        return result

    def _pending_request_id(self, prompt) -> Optional[str]:
        """The request_id of the pending approval *prompt* was rendered from.

        Hermes doesn't hand it to us, and without it the resolver can only
        take the session's oldest approval, which with parallel tool calls may
        be another prompt's. The entry is queued before Hermes notifies us;
        match it the way Hermes built the prompt from it (redacted command,
        description), skipping entries our other prompts answer. None when
        nothing pending matches. Hermes' queue is internal: anything
        unexpected raises, and the caller falls back to a plain prompt.
        """
        from tools import approval
        from gateway.run import _redact_approval_command

        claimed = {rid for _, rid in self._approval_prompts.values()}
        with approval._lock:
            entries = [
                e.data for e in approval._gateway_queues.get(prompt.session_key, [])
            ]
        for data in entries:
            if (
                data["request_id"] not in claimed
                and _redact_approval_command(data.get("command", "")) == prompt.command
                and data.get("description", "dangerous command") == prompt.description
            ):
                return data["request_id"]
        return None

    async def _handle_reaction(self, event: Dict[str, Any]) -> None:
        """Resolve the exec approval a 👍/👎 reaction answers.

        Hermes never sees reactions, so this is the authorization gate: the
        reactor must pass _sender_allowed(strict) for this chat, the prompt's
        session must belong to this chat and, for per-user group sessions, to
        the reactor — whoever could have typed /approve for it.
        """
        msg_id, chat_id, contact_id = (
            event.get("msg_id"),
            event.get("chat_id"),
            event.get("contact_id"),
        )
        if msg_id not in self._approval_prompts:
            return
        session_key, request_id = self._approval_prompts[msg_id]
        emojis = re.sub(
            "[\U0001f3fb-\U0001f3ff\ufe0f]", "", event.get("reaction") or ""
        ).split()
        choices = {self._APPROVAL_REACTIONS.get(e) for e in emojis}
        if len(choices) != 1 or None in choices:
            return
        (choice,) = choices
        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
        except Exception as e:
            logger.warning("Ignoring approval reaction on prompt %s: %s", msg_id, e)
            return
        chat_type = "group" if chat.get("chat_type") == "Group" else "dm"
        # why: exact keys, not a ":<chat_id>" suffix — chat and contact ids share
        # a range, so group 12's per-user key for contact 12 ends in ":12" too.
        own = f":{chat_type}:{chat_id}"
        if not session_key.endswith((own, f"{own}:{contact_id}")):
            logger.info(
                "Ignoring approval reaction from contact %s on prompt %s: not "
                "their session",
                contact_id,
                msg_id,
            )
            return
        if not await self._sender_allowed(contact_id, chat_type, chat_id, strict=True):
            logger.info(
                "Ignoring approval reaction from unauthorized contact %s", contact_id
            )
            return
        from gateway.slash_access import policy_from_extra

        # why: Hermes refuses /approve and /deny from non-admins when
        # allow_admin_from is set; a reaction must not get around that.
        command = "deny" if choice == "deny" else "approve"
        policy = policy_from_extra(self.config.extra or {}, chat_type)
        if not policy.can_run(str(contact_id), command):
            logger.info(
                "Ignoring approval reaction from contact %s: /%s is admin-only",
                contact_id,
                command,
            )
            return

        from tools.approval import resolve_gateway_approval

        del self._approval_prompts[msg_id]
        count = resolve_gateway_approval(session_key, choice, request_id=request_id)
        logger.info(
            "Contact %s reacted to approval prompt %s: %s (%d resolved)",
            contact_id,
            msg_id,
            choice,
            count,
        )
        if not count:
            reply = "⌛ Nothing pending anymore: it timed out or was already answered."
        else:
            reply = "✅ Approved." if choice == "once" else "❌ Denied."
        await self.send(str(chat_id), reply, reply_to=str(msg_id))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file as an inline-playable video.

        why: without this override Hermes falls back to the base class, which
        posts a "couldn't send video" notice instead of the file — on both the
        reply-flow MEDIA path and cron delivery.
        """
        from deltachat2.types import MessageViewtype

        return await self._send_msg_data(
            chat_id,
            "videos",
            f"video {video_path}",
            reply_to,
            file=video_path,
            text=caption or "",
            viewtype=MessageViewtype.VIDEO,
        )

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

        Delta Chat supports voice messages natively. *reply_to* is ignored.

        Args:
            chat_id: Delta Chat chat ID
            audio_path: Path to audio file on disk
            caption: Optional caption for the voice message
            reply_to: Unused
            metadata: Optional metadata

        Returns:
            SendResult with success status and message ID
        """
        if not os.path.isfile(audio_path):
            logger.error("send_voice: audio file not found: %s", audio_path)
            return self._send_result(
                chat_id, None, error=f"Audio file not found: {audio_path}"
            )

        from deltachat2.types import MessageViewtype

        return await self._send_msg_data(
            chat_id,
            "voices",
            f"voice {audio_path} ({os.path.getsize(audio_path)} bytes)",
            file=audio_path,
            text=caption or "",
            viewtype=MessageViewtype.VOICE,
        )

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
        # location tuple is (latitude, longitude) per GeoJSON convention
        return await self._send_msg_data(
            chat_id,
            "locations",
            "location",
            text=poi_name,
            location=(latitude, longitude),
        )

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
        p = str(container_path)
        if not p.startswith(_WORKSPACE_PREFIX):
            return None
        rel = p[len(_WORKSPACE_PREFIX) :]
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
        media_files, remaining = BasePlatformAdapter.extract_media(content)

        for match in _XDC_MEDIA_RE.finditer(content):
            path = match.group(1).strip()
            if not any(p == path for p, _ in media_files):
                media_files.append((path, False))
            remaining = remaining.replace(match.group(0), "").strip()

        return media_files, remaining

    def extract_local_files(self, content: str):
        """Extend base to also pick up bare .xdc paths.

        .xdc is not in Hermes's MEDIA_DELIVERY_EXTS, so the base staticmethod
        never picks up bare .xdc paths. We add them explicitly for both
        deployment shapes:
          * Docker sandbox container paths like /workspace/app.xdc, which
            don't exist on the host — filter_local_delivery_paths then maps
            them to the host sandbox before validation.
          * Non-Docker deployments where the agent writes to its real host
            cwd and references it by absolute (or ~/) path — these flow
            unchanged to the base validator, same as extract_media above.
        """
        files, remaining = BasePlatformAdapter.extract_local_files(content)

        for match in _XDC_LOCAL_PATH_RE.finditer(content):
            path = match.group(1)
            if path not in files:
                files.append(path)
                remaining = remaining.replace(match.group(0), "").strip()

        return files, remaining

    def _remap_container_path(self, path: str) -> Optional[str]:
        """Host cache path for a /workspace/ container path, or None.

        None means "not a container path" or "could not be copied" (logged).
        """
        p = str(path)
        if not p.startswith(_WORKSPACE_PREFIX):
            return None
        cached = self._copy_container_file_to_cache(p)
        if not cached:
            logger.warning("Could not resolve container path for delivery: %s", p)
        return cached

    def filter_media_delivery_paths(self, media_files, session_key: str = ""):
        """Remap /workspace/ container paths to host cache before validation.

        An unresolvable container path is passed through unchanged; the base
        validator then rejects it.
        """
        remapped = [
            (self._remap_container_path(media_path) or media_path, is_voice)
            for media_path, is_voice in media_files or []
        ]
        base_fn = BasePlatformAdapter.filter_media_delivery_paths
        if _base_supports_session_key(base_fn):
            return base_fn(remapped, session_key=session_key)
        return base_fn(remapped)

    def filter_local_delivery_paths(self, file_paths, session_key: str = ""):
        """Remap /workspace/ container paths to host cache before validation.

        An unresolvable container path is dropped.
        """
        remapped = []
        for file_path in file_paths or []:
            if not str(file_path).startswith(_WORKSPACE_PREFIX):
                remapped.append(file_path)
            elif cached := self._remap_container_path(file_path):
                remapped.append(cached)
        base_fn = BasePlatformAdapter.filter_local_delivery_paths
        if _base_supports_session_key(base_fn):
            return base_fn(remapped, session_key=session_key)
        return base_fn(remapped)

    def _rpc_server_exit_code(self) -> Optional[int]:
        """Exit code of the deltachat-rpc-server subprocess, or None if alive.

        IOTransport only binds `.process` once start() has been called, so a
        missing attribute means "not started yet", not "dead".
        """
        process = getattr(self._transport, "process", None)
        if process is None:
            return None
        return process.poll()

    async def _event_listener(self) -> None:
        """Listen for Delta Chat events and forward to Hermes.

        Retries transient RPC errors in place. If the loop ever stops while we
        still believe we are connected — the RPC subprocess died, or the task
        was cancelled by something other than disconnect() — the adapter is
        deaf: DC keeps queueing events and nothing drains them. That used to be
        silent and permanent. Now it is escalated to the gateway, which owns
        supervision and rebuilds a fresh adapter (see _escalate_listener_death).
        """
        try:
            while self._running:
                try:
                    if self.account_id:
                        envelope = await self.rpc.get_next_event()
                        if envelope.get("context_id") == self.account_id:
                            await self._handle_dc_event(envelope.get("event", {}))
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    if not await self._handle_listener_error(e):
                        break
        finally:
            # is_connected is the base class's self._running, which _cleanup()
            # and _set_fatal_error() both clear — so a deliberate teardown (and
            # an error path that already escalated) falls through here without
            # escalating again.
            if self.is_connected:
                self._escalate_listener_death(
                    "event_listener_stopped",
                    "Delta Chat event listener stopped while connected",
                )

    async def _handle_listener_error(self, exc: Exception) -> bool:
        """Handle an error from the listen loop. Return True to keep polling.

        A transient RPC error is logged and retried after a short sleep. But
        once the deltachat-rpc-server subprocess is gone, retrying is futile
        and actively harmful: the vendored transport resolves the in-flight
        call with an error and then every subsequent call would raise (or, on
        an older transport, hang) — see vendor/deltachat2/transport.py. So the
        moment poll() shows the subprocess has exited, stop the loop and let
        the `finally` in _event_listener escalate to the gateway, which
        respawns the RPC server by rebuilding the adapter.
        """
        exit_code = self._rpc_server_exit_code()
        # why also ask the transport: with its reader or writer thread gone the
        # subprocess is still running (no exit code) but can never answer.
        # Treating that as transient retried once a second, forever.
        transport_dead = (
            getattr(self._transport, "_server_dead", lambda: False)() is True
        )
        if exit_code is None and not transport_dead:
            logger.error("Event listener error: %s", exc)
            now = time.monotonic()
            with self._lock:
                self._crash_times = [t for t in self._crash_times if now - t < 60]
                self._crash_times.append(now)
            self._bump_stat("event_listener_errors")
            await asyncio.sleep(1)
            return True

        what = (
            "transport stopped"
            if exit_code is None
            else f"exited with code {exit_code}"
        )
        logger.error(
            "deltachat-rpc-server %s; stopping the event listener. Last error: %s",
            what,
            exc,
        )
        self._escalate_listener_death("rpc_server_died", f"deltachat-rpc-server {what}")
        return False

    def _escalate_listener_death(self, code: str, message: str) -> None:
        """Report a dead event listener to the gateway and let it recover us.

        Hermes owns supervision: _handle_adapter_fatal_error drops this adapter
        and _platform_reconnect_watcher rebuilds a *fresh* one with 30s->300s
        backoff. So we must not restart the listener ourselves — an adapter-side
        supervisor would race that watcher and keep the RPC subprocess and the
        accounts-dir lock alive, which is exactly what blocks the replacement
        from connecting.

        The notify is fired as its own task rather than awaited: the gateway's
        fatal handler calls back into disconnect(), which cancels *this* task.
        Awaiting from inside the task would cancel it mid-teardown.
        """
        if not self.is_connected:
            return
        self._set_fatal_error(code, message, retryable=True)
        # Held on the instance so the task isn't garbage-collected mid-flight.
        self._fatal_notify_task = asyncio.create_task(self._notify_fatal_error())

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
            msg_id = event.get("msg_id")
            chat_id = event.get("chat_id")
            error = None
            try:
                msg = await self.rpc.get_message(self.account_id, int(msg_id))
                error = msg.get("error")
            except Exception as e:
                logger.debug(
                    "Could not fetch failed message %s for error detail: %s",
                    msg_id,
                    e,
                )
            logger.warning(
                "Message failed: msg_id=%s chat_id=%s error=%s",
                msg_id,
                chat_id,
                error or "unknown",
            )
        elif event_kind == EventType.INCOMING_CALL:
            if self._call_manager:
                self._spawn(self._call_manager.handle_incoming_call(event))
        elif event_kind == EventType.CALL_ENDED:
            if self._call_manager:
                self._spawn(self._call_manager.handle_call_ended(event))
        elif event_kind == EventType.OUTGOING_CALL_ACCEPTED:
            if self._call_manager:
                self._spawn(self._call_manager.handle_outgoing_call_accepted(event))
        elif event_kind == EventType.INCOMING_CALL_ACCEPTED:
            logger.info("Incoming call accepted msg_id=%s", event.get("msg_id"))
        elif event_kind == EventType.SECUREJOIN_INVITER_PROGRESS:
            await self._record_securejoin_pairing(event)
        elif event_kind == EventType.INCOMING_REACTION:
            await self._handle_reaction(event)
        else:
            logger.debug(f"Unhandled event type: {event_kind}")

    @staticmethod
    def _paired_key(contact_id) -> str:
        return f"ui.hermes.paired.{int(contact_id)}"

    async def _record_securejoin_pairing(self, event: Dict[str, Any]) -> None:
        """Persist that a contact completed SecureJoin against this account's invite.

        why: core >= 2.6x no longer tracks Contact.is_verified, and
        is_key_contact / accepted-chat state say nothing about SecureJoin. The
        inviter-side completion event is the only proof the contact scanned our
        QR, so remember it in a UI config key (survives restarts).
        """
        try:
            if event.get("progress") != 1000 or event.get("chat_type") != "Single":
                return
            contact_id = event.get("contact_id")
            if not contact_id:
                return
            await self.rpc.set_config(
                self.account_id, self._paired_key(contact_id), "1"
            )
            logger.info("SecureJoin completed, paired contact %s", contact_id)
        except Exception as e:
            logger.warning("Could not record SecureJoin pairing: %s", e)

    async def _is_securejoin_paired(self, contact_id) -> bool:
        """True if SecureJoin completion was recorded for this contact. Fail-closed."""
        try:
            return (
                await self.rpc.get_config(self.account_id, self._paired_key(contact_id))
                == "1"
            )
        except Exception as e:
            logger.warning("Could not read pairing marker for %s: %s", contact_id, e)
            return False

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

            if not await self._gate_inbound(chat_id, msg_id, msg.get("from_id")):
                return

            # why after the gate: a read receipt tells a rejected sender the
            # bot is there and read them — the one thing
            # send_rejection_replies=false is meant to keep quiet.
            try:
                await self.rpc.markseen_msgs(self.account_id, [int(msg_id)])
            except Exception as e:
                logger.debug(f"Could not mark message {msg_id} as seen: {e}")

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

            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))

            from_id = msg.get("from_id")
            sender_email = ""
            is_bot = None
            if from_id:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
                user_name = _contact_name(contact, f"Contact {from_id}")
                user_id = str(from_id)
                sender_email = (contact.get("address") or "").lower()
                is_bot = contact.get("is_bot")
            else:
                user_name, user_id = "Unknown", "unknown"

            chat_type = "group" if chat.get("chat_type") == "Group" else "dm"
            chat_name = chat.get("name", f"Chat {chat_id}")

            # Both bot guards assume a shared group with a changing/checkable
            # participant set. A DM has exactly one counterparty by
            # definition, so "someone else chiming in" can never happen —
            # from_id never changes and the streak would only grow, tripping
            # permanently with no way to recover. Restrict to groups.
            #
            # A DC "Group" with only one other member (e.g. a solo-topic
            # group like "Household" created for a single human) has the
            # identical problem: that one member is structurally the only
            # possible sender, so a same-sender streak can never be broken
            # by "someone else chiming in" and, once tripped, never
            # recovers — permanently and silently. Treat it like a DM.
            roster = (
                await self._get_group_roster(chat_id) if chat_type == "group" else None
            )
            if roster is not None and len(roster) > 1:
                if not await self._apply_bot_guards(
                    chat_id, from_id, sender_email, is_bot
                ):
                    return

            # "/cmd@<name>": addressed to us → Hermes sees a plain "/cmd". In
            # a group, one addressed to another bot is not ours to run. A bare
            # "/cmd" still reaches every bot, as before.
            addressed = _COMMAND_ADDR_RE.match(text)
            if addressed:
                for_us = self._command_for_us(text, addressed.end())
                if for_us is not None:
                    text = for_us
                elif chat_type == "group":
                    logger.debug(
                        "Ignoring command %s addressed to another bot", text.split()[0]
                    )
                    return

            # why: mention gate must run on the reply body only — matching inside
            # spliced-in quoted text would treat "someone quoted an old message
            # that once mentioned us" as a fresh mention of the new reply.
            should_process, is_reply_to_self = await self._gate_mention(
                msg, text, chat_type, chat_id
            )
            if not should_process:
                return

            # Surface the quoted text so the LLM knows which earlier point is
            # being replied to.
            quote = msg.get("quote") or {}
            if quote.get("kind") == "WithMessage" and quote.get("text"):
                # why: core reports author_display_name as the localized "Me"
                # for self-authored quotes — substitute the bot's real name so
                # the LLM sees "replying to <bot>" not "replying to Me".
                quoted_author = (
                    self._display_name
                    if is_reply_to_self
                    else (quote.get("author_display_name") or "a message")
                )
                text = f'[replying to {quoted_author}: "{quote["text"]}"]\n{text}'

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
                    chat_id, msg_id, from_id, chat_type == "group", token, roster
                ),
            )
            await self.handle_message(message_event)

        except Exception as e:
            logger.error(f"Error handling message event: {e}")

    async def _apply_bot_guards(
        self, chat_id, from_id, sender_email: str, is_bot: Optional[bool] = None
    ) -> bool:
        """Run the loop and bot-exchange guards. Return True to keep processing."""
        should_process, should_warn = self._check_loop_guard(chat_id, from_id)
        if not should_process:
            return await self._guard_tripped(
                chat_id,
                "loop_guard_tripped",
                should_warn,
                f"loop_guard tripped in chat {chat_id}: sender {from_id} hit "
                f"max_consecutive_replies={self._max_consecutive_replies} with no "
                "other participant chiming in; further messages from them here "
                "are dropped until someone else speaks",
                f"Pausing replies in this chat — {self._max_consecutive_replies} "
                "in a row from the same sender with no one else joining in "
                "(looks like a bot loop). Send a message to resume.",
            )

        should_process, should_warn = self._check_bot_exchange_guard(
            chat_id, sender_email, is_bot
        )
        if not should_process:
            return await self._guard_tripped(
                chat_id,
                "bot_exchange_guard_tripped",
                should_warn,
                f"bot_exchange_guard tripped in chat {chat_id}: "
                f"max_bot_exchanges={self._max_bot_exchanges} hit with no "
                f"human check-in (last sender {sender_email!r}, is_bot={is_bot}); "
                "further bot messages here are dropped until one checks in",
                f"Pausing replies in this chat — {self._max_bot_exchanges} "
                "bot-to-bot messages with no human check-in. Send a message "
                "to resume.",
            )
        return True

    async def _guard_tripped(
        self, chat_id, stat: str, should_warn: bool, warning: str, notice: str
    ) -> bool:
        """Record a dropped message; on a streak's first trip, warn and notify.

        The WARNING is unconditional (unlike the in-chat notice): with
        send_rejection_replies=false it is the only trace a trip ever leaves —
        a guard can stay tripped in a multi-member group whose other members
        simply go quiet, which is otherwise indistinguishable from an outage.
        Always returns False.
        """
        self._bump_stat(stat)
        if should_warn:
            logger.warning(warning)
            if self._send_rejection_replies:
                await self.send(str(chat_id), notice)
        return False

    async def _gate_mention(
        self, msg: Dict, text: str, chat_type: str, chat_id
    ) -> tuple[bool, bool]:
        """Mention gate shared by the text and non-text (image/file/voice) paths.

        A quote-reply to one of this bot's own messages is an implicit mention:
        it continues the thread under require_mention, e.g. a screenshot sent
        in reply to what the bot just said. Otherwise *text* (the reply body or
        caption) must pass _check_mention.

        Returns (should_process, is_reply_to_self).
        """
        quote = msg.get("quote") or {}
        if quote.get("kind") == "WithMessage" and await self._quote_is_self_authored(
            quote
        ):
            return True, True
        return await self._check_mention(text, chat_type, chat_id), False

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
            data = Path(src).read_bytes()
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
        sender_email = ""
        is_bot = None
        try:
            if from_id:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
                user_name = _contact_name(contact, user_name)
                sender_email = (contact.get("address") or "").lower()
                is_bot = contact.get("is_bot")
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

        roster = await self._get_group_roster(chat_id) if chat_type == "group" else None
        # why: same guards as the text path (see _handle_incoming_message) — a
        # human's image/voice message must reset the bot-exchange count too.
        if roster is not None and len(roster) > 1:
            if not await self._apply_bot_guards(chat_id, from_id, sender_email, is_bot):
                return

        caption = msg.get("text", "") or ""
        should_process, _ = await self._gate_mention(msg, caption, chat_type, chat_id)
        if not should_process:
            return

        meta = self._message_metadata(
            chat_id, msg_id, from_id, chat_type == "group", token, roster
        )

        async def forward(label: str, hermes_type, resolved: Optional[str], mime):
            text = f"[{label}]: {caption}" if caption else f"[{label}]"
            await self.handle_message(
                MessageEvent(
                    text=f"{text}\n[dc:chat={token}]",
                    message_type=hermes_type,
                    source=source,
                    message_id=str(msg_id),
                    media_urls=[resolved] if resolved else [],
                    media_types=[mime],
                    metadata=meta,
                )
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
            else:
                logger.warning(
                    "Voice/audio file not found, forwarding without media: %s", filename
                )
            is_voice = view_type == MessageViewtype.VOICE.value
            await forward(
                f"{'Voice' if is_voice else 'Audio'} message from {user_name}",
                MessageType.VOICE if is_voice else MessageType.AUDIO,
                resolved,
                file_mime or ("audio/ogg" if is_voice else "audio/mpeg"),
            )

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
            await forward(
                f"Image from {user_name}",
                MessageType.PHOTO,
                resolved,
                file_mime or "image/jpeg",
            )

        # File / document (including .xdc webxdc apps)
        elif (
            view_type in (MessageViewtype.FILE.value, MessageViewtype.VIDEO.value)
            and filename
        ):
            resolved = self._resolve_blob_path(filename)
            if resolved:
                try:
                    from gateway.platforms.base import cache_document_from_bytes

                    resolved = cache_document_from_bytes(
                        Path(resolved).read_bytes(),
                        msg.get("file_name") or os.path.basename(resolved),
                    )
                    logger.info("Copied document to Hermes cache: %s", resolved)
                except Exception as e:
                    logger.warning("Could not copy document to Hermes cache: %s", e)
            file_name = msg.get("file_name") or os.path.basename(filename)
            await forward(
                f"File from {user_name}: {file_name}",
                MessageType.DOCUMENT,
                resolved,
                file_mime or "application/octet-stream",
            )

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
                    await self._resolve_chat_id(chat_id),
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
    rpc_server = os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server")
    return _check_dc2_available() and shutil.which(rpc_server) is not None


def validate_config(config) -> bool:
    """Validate platform configuration."""
    if not check_requirements():
        return False

    cfg = functools.partial(_cfg, config)

    email = cfg("DELTACHAT_EMAIL", "email", "auto")
    password = cfg("DELTACHAT_PASSWORD", "password")
    if email and email != "auto" and not _is_valid_email(email):
        raise ValueError(f"DELTACHAT_EMAIL is not a valid email address: {email!r}")
    if email and email != "auto" and not password:
        raise ValueError(
            "DELTACHAT_PASSWORD required when DELTACHAT_EMAIL is set (not 'auto')"
        )

    dm_policy = cfg("DELTACHAT_DM_POLICY", "dm_policy", "pairing")
    if dm_policy not in ("open", "allowlist", "pairing", "disabled"):
        raise ValueError(f"Invalid DELTACHAT_DM_POLICY: {dm_policy!r}")

    group_policy = cfg("DELTACHAT_GROUP_POLICY", "group_policy", "open")
    if group_policy not in ("open", "allowlist", "disabled"):
        raise ValueError(f"Invalid DELTACHAT_GROUP_POLICY: {group_policy!r}")

    # why: an allowlist that names nobody admits nobody (see _on_allowlist);
    # say so at startup instead of silently rejecting every sender.
    if not _cfg_bool(
        config, "DELTACHAT_ALLOW_ALL_USERS", "allow_all_users"
    ) and not cfg("DELTACHAT_ALLOWED_USERS", "allowed_users"):
        for kind, policy, env, key in (
            ("DM", dm_policy, "DELTACHAT_DM_ALLOWED_USERS", "dm_allowed_users"),
            (
                "GROUP",
                group_policy,
                "DELTACHAT_GROUP_ALLOWED_USERS",
                "group_allowed_users",
            ),
        ):
            if policy == "allowlist" and not cfg(env, key):
                raise ValueError(
                    f"DELTACHAT_{kind}_POLICY is 'allowlist' but neither {env} nor "
                    "DELTACHAT_ALLOWED_USERS names anyone"
                )

    # Lightweight path checks (do not create directories or require the binary).
    _safe_data_dir(
        cfg("DELTACHAT_DATA_DIR", "data_dir", _default_dc_data_dir()),
        create=False,
    )

    avatar_path = cfg("DELTACHAT_AVATAR_PATH", "avatar_path")
    if avatar_path:
        _validate_avatar_path(avatar_path, strict=False)

    rpc_server = cfg("DELTACHAT_RPC_SERVER", "rpc_server", "deltachat-rpc-server")
    if rpc_server != "deltachat-rpc-server":
        _validate_rpc_server_path(rpc_server, strict=True)

    chatmail_servers = cfg("DELTACHAT_CHATMAIL_SERVERS", "chatmail_servers")
    if chatmail_servers and not _parse_csv_unique(chatmail_servers):
        raise ValueError(f"Invalid DELTACHAT_CHATMAIL_SERVERS: {chatmail_servers!r}")

    for env, key, _default, lo, hi in (_MAX_LEN_CFG, _MAX_LINES_CFG):
        raw = cfg(env, key)
        if raw and _bounded_int(raw, lo, hi) is None:
            raise ValueError(f"{env} must be an integer between {lo} and {hi}: {raw!r}")

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

    # why: Hermes replaces extra wholesale with our return value, so values already
    # under platform_cfg["extra"] (e.g. set by an earlier hook) must be carried
    # forward or they are silently dropped.
    extra_block = platform_cfg.get("extra") or {}
    if isinstance(extra_block, dict):
        seeded.update(extra_block)

    for key in (
        "display_name",
        "avatar_path",
        "email",
        "chatmail_server",
        "chatmail_servers",
        "data_dir",
        "home_channel",
        "allowed_users",
        "allow_all_users",
        "dm_allowed_users",
        "group_allowed_users",
        "dm_policy",
        "group_policy",
        "human_users",
        "max_bot_exchanges",
        "require_mention",
        "mention_aliases",
        "free_response_channels",
        "require_mention_channels",
        "commands_bio",
        "auto_delete_interval",
        "max_message_length",
        "max_message_lines",
    ):
        value = platform_cfg.get(key)
        if value is not None:
            seeded[key] = value

    return seeded


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig from environment variables."""
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
    email = os.getenv("DELTACHAT_EMAIL")
    if email:
        result["email"] = email
    result["data_dir"] = os.getenv("DELTACHAT_DATA_DIR", _default_dc_data_dir())
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


# Message.state values from deltachat-core (the OpenRPC spec types it as a bare int).
_MSG_STATE_FAILED = 24
_MSG_STATE_DELIVERED = 26
_STANDALONE_DELIVERY_TIMEOUT = 60.0


async def _wait_delivered(adapter: "DeltaChatAdapter", msg_ids: list) -> Optional[str]:
    """Poll until every message left the outbox; return an error string or None.

    why: send_msg only queues. Closing the RPC server before SMTP finishes would
    drop the message, so a short-lived sender must wait for OutDelivered.
    """
    deadline = time.monotonic() + _STANDALONE_DELIVERY_TIMEOUT
    pending = set(msg_ids)
    while pending:
        for mid in list(pending):
            msg = await adapter.rpc.get_message(adapter.account_id, mid)
            state = int(msg.get("state") or 0)
            if state == _MSG_STATE_FAILED:
                return "Delta Chat could not deliver the message"
            if state >= _MSG_STATE_DELIVERED:
                pending.discard(mid)
        if pending:
            if time.monotonic() > deadline:
                return "timed out waiting for Delta Chat delivery (retryable)"
            await asyncio.sleep(0.5)
    return None


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
    caption=None,
    **_ignored,
) -> Dict[str, Any]:
    """Send without a running gateway (``hermes send``, headless cron).

    Opens a short-lived deltachat-rpc-server on the profile's accounts dir.
    The core holds an exclusive ``accounts.lock``, so if the gateway is up the
    second server exits at once; that is reported as a retryable error rather
    than risking a second writer on the same database.
    ``thread_id`` is ignored: Delta Chat has no threads.
    """
    result: Dict[str, Any] = {
        "success": False,
        "platform": "deltachat-platform",
        "chat_id": str(chat_id),
    }
    if not _check_dc2_available():
        result["error"] = "deltachat2 is not installed"
        return result
    # A numeric id or a 16-hex chat token (resolved in send() once RPC is up).
    if not re.fullmatch(r"\d+|[0-9a-f]{16}", str(chat_id).strip()):
        result["error"] = f"invalid Delta Chat chat id: {chat_id!r}"
        return result

    # why: core passes the caption separately for captionable media sends.
    message = message or caption or ""
    files = list(media_files or [])
    # Validate up front so a bad path fails before anything is sent.
    for item in files:
        path = item[0] if isinstance(item, (tuple, list)) else item
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            result["error"] = (
                f"media file missing or unreadable: {os.path.basename(str(path))}"
            )
            return result

    adapter = DeltaChatAdapter(pconfig)
    try:
        import deltachat2
        from deltachat2.transport import IOTransport
        from deltachat2.types import MessageViewtype

        dc_accounts_path = adapter._get_dc_config_dir()
        os.environ["DC_ACCOUNTS_PATH"] = dc_accounts_path
        adapter._transport = IOTransport(
            accounts_dir=dc_accounts_path, rpc_server=adapter._get_rpc_server_path()
        )
        adapter._transport.start()
        adapter.rpc = _AsyncRpc(deltachat2.Rpc(adapter._transport))

        try:
            accounts = await adapter.rpc.get_all_accounts()
        except Exception:
            if adapter._rpc_server_exit_code() is not None:
                result["error"] = (
                    "Delta Chat database is in use by another process "
                    "(is the gateway running?); retry later"
                )
                result["retryable"] = True
                return result
            raise
        if not accounts:
            result["error"] = "no Delta Chat account configured; run setup.py"
            return result
        adapter.account_id = accounts[0]["id"]
        # Chat ids from cron/`hermes send` belong to one database, too.
        if not await adapter._check_db_id():
            result["error"] = (
                "Delta Chat database does not match this Hermes state (see log)"
            )
            return result
        await adapter.rpc.start_io(adapter.account_id)

        sent = []
        if message and message.strip():
            res = await adapter.send(str(chat_id), message)
            if not res.success:
                result["error"] = "Delta Chat send failed (see gateway log)"
                return result
            sent.append(res)
        for item in files:
            path = item[0] if isinstance(item, (tuple, list)) else item
            fields = {"file": str(path), "text": ""}
            if force_document:
                fields["viewtype"] = MessageViewtype.FILE
            res = await adapter._send_msg_data(
                str(chat_id), "files", f"file {os.path.basename(str(path))}", **fields
            )
            if not res.success:
                result["error"] = "Delta Chat send failed (see gateway log)"
                return result
            sent.append(res)
        if not sent:
            result["error"] = "nothing to send"
            return result

        ids = [int(r.message_id) for r in sent if r.message_id]
        err = await _wait_delivered(adapter, ids)
        if err:
            result["error"] = err
            return result
        result.update(success=True, message_id=sent[-1].message_id)
        return result
    except Exception as e:
        logger.error("Standalone Delta Chat send failed: %s", e)
        # why: str(e) can echo paths/config; keep only the exception type.
        result["error"] = f"Delta Chat send failed ({type(e).__name__})"
        return result
    finally:
        adapter._cleanup()


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
        standalone_sender_fn=_standalone_send,
        # why: without these, gateway._is_user_authorized has no way to know
        # DELTACHAT_ALLOW_ALL_USERS/DELTACHAT_ALLOWED_USERS exist, and never
        # trusts our own dm_policy/group_policy: open as authorization (by
        # design — see gateway authz_mixin.py), so it silently drops every
        # sender regardless of our own access-control config.
        allowed_users_env="DELTACHAT_ALLOWED_USERS",
        allow_all_env="DELTACHAT_ALLOW_ALL_USERS",
        emoji="💬",
        platform_hint=(
            "You are chatting via Delta Chat. "
            "Delta Chat does NOT support markdown formatting or message editing. "
            "Markdown markers are stripped and long replies are split into "
            "several short plain-text messages, so keep responses brief and "
            "conversational. "
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
            "For file delivery: write output files to your current working directory "
            "(run `pwd` to find it), NOT /tmp/. "
            "Then reference the file by ABSOLUTE path in a MEDIA directive — e.g. "
            "'MEDIA:/abs/path/app.xdc'. In the Docker sandbox the working directory "
            "is /workspace/, so there it is 'MEDIA:/workspace/app.xdc'. "
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
    skills_dir = Path(_plugin_dir) / "skills"
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
      - dc_rpc_spec: OpenRPC spec, minus the methods we refuse
      - dc_chat_rpc_spec: spec filtered to chatId-scoped methods we do not refuse
      - dc_safe_rpc_call: chat-scoped calls with token-validated chatId injection

    Only registers when DELTACHAT_ENABLE_RAW_RPC is on:
      - dc_rpc_call: any RPC method _is_blocked does not refuse
    """

    def _visible_methods(spec: dict, chat_scoped: bool) -> list:
        # why: never advertise a method the call gate then refuses — the model
        # cannot tell "not permitted" from "wrong name" and burns the turn retrying.
        return [
            m
            for m in spec.get("methods", [])
            if not _is_blocked(m["name"])
            and (
                not chat_scoped
                or any(p["name"] == "chatId" for p in m.get("params", []))
            )
        ]

    async def _spec_handler(args: dict = None, **kwargs) -> str:
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return f"Error: {e}"
        return json.dumps(
            {**spec, "methods": _visible_methods(spec, chat_scoped=False)}, indent=2
        )

    async def _call_handler(args: dict, **kwargs) -> str:
        method = (args or {}).get("method")
        params = (args or {}).get("params") or []
        if not method or not isinstance(method, str):
            return json.dumps({"error": "Missing 'method' (snake_case RPC name)."})
        if _active_adapter is None or _active_adapter.rpc is None:
            return json.dumps({"error": "Delta Chat is not connected"})

        # why: %r, not %s — `method` is model-supplied; an embedded newline
        # would otherwise forge a second audit line.
        def _refuse(reason: str, detail: str) -> str:
            logger.warning("Raw RPC call REFUSED (%s): %r", reason, method)
            return json.dumps({"error": detail})

        # Read at call time, not import time — Hermes loads ~/.hermes/.env
        # after this module is imported.
        raw_allowlist = (os.getenv("DELTACHAT_RAW_RPC_ALLOWLIST") or "").strip()
        allowlist = _parse_method_list(raw_allowlist)
        # why: a non-blank value that names nothing means "allow nothing", not
        # "unrestricted" — a typo like " , ," must not remove the gate.
        if raw_allowlist and not allowlist:
            return _refuse(
                "unusable allowlist",
                "DELTACHAT_RAW_RPC_ALLOWLIST is set but lists no method names",
            )
        if allowlist and method not in allowlist:
            return _refuse(
                "not allowlisted", f"'{method}' is not in the raw RPC allowlist"
            )
        blocklist = _parse_method_list(os.getenv("DELTACHAT_RAW_RPC_BLOCKLIST"))
        if method in blocklist or _is_blocked(method):
            return _refuse("blocked", f"'{method}' is blocked")

        # Logged after every gate so the audit trail tells ran from refused.
        logger.warning("Raw RPC call ACCEPTED: %r", method)

        try:
            result = await getattr(_active_adapter.rpc, method)(*params)
            return json.dumps(result, default=str)
        except AttributeError:
            return json.dumps({"error": f"Unknown method '{method}'"})
        except Exception as e:
            logger.error("Raw RPC call %s failed: %s", method, e, exc_info=True)
            return json.dumps({"error": "RPC call failed"})

    async def _chat_spec_handler(args: dict = None, **kwargs) -> str:
        """Return only the chatId-scoped methods _is_blocked does not refuse."""
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return f"Error: {e}"
        return json.dumps(
            {**spec, "methods": _visible_methods(spec, chat_scoped=True)}, indent=2
        )

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

        if _calling_chat_mismatch(adapter, real_chat_id):
            logger.warning("Safe RPC call %r REFUSED (token of another chat)", method)
            return _WRONG_CHAT_ERROR

        if _is_blocked(method):
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

        # why: bind by name, not position. [account_id, chat_id] + params
        # assumes chatId is parameter 1; search_messages(accountId, query,
        # chatId) breaks that, and there the caller's own value would land in
        # the chatId slot — defeating the token. The spec declares the order.
        supplied = list(params or [])
        full_params = []
        for name in param_names:
            if name == "accountId":
                full_params.append(adapter.account_id)
            elif name == "chatId":
                full_params.append(real_chat_id)
            elif supplied:
                full_params.append(supplied.pop(0))
            else:
                break  # trailing optional parameters the caller left off
        if supplied:
            return json.dumps(
                {
                    "error": (
                        f"'{method}' takes {len(param_names)} parameters "
                        f"({', '.join(param_names)}); accountId and chatId are "
                        f"injected, so pass only the rest — {len(supplied)} too "
                        "many were given"
                    )
                }
            )

        # why: core sends whatever local path it is handed, so without this
        # one call mails ~/.hermes/.env to the chat. The token scopes the
        # chat, not the file. Same filter the adapter's own sends use.
        unchecked = _unchecked_path_name(param_names)
        for name, value in zip(param_names, full_params):
            if unchecked is None and name == "data" and isinstance(value, dict):
                unchecked = _unchecked_path_name(value)
        if unchecked is not None:
            logger.warning(
                "Safe RPC call %r REFUSED (unchecked path parameter %r)",
                method,
                unchecked,
            )
            return json.dumps(
                {
                    "error": (
                        f"'{method}' takes a file path ('{unchecked}') this tool "
                        "cannot validate"
                    )
                }
            )
        for i, name in enumerate(param_names[: len(full_params)]):
            value = full_params[i]
            if name == "data" and isinstance(value, dict) and value.get("file"):
                safe = _safe_delivery_path(adapter, value["file"])
                if safe is None:
                    return _refuse_path(method, value["file"])
                full_params[i] = {**value, "file": safe}
            elif name in _PATH_PARAMS and value:
                safe = _safe_delivery_path(adapter, value)
                if safe is None:
                    return _refuse_path(method, value)
                full_params[i] = safe

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

        # Hang up the call of the chat this tool call came from. Only when no
        # chat is bound (see _calling_chat_mismatch) fall back to "the" call —
        # there is typically only one at a time.
        mgr = adapter._call_manager
        candidates = [
            c for c in mgr.active_chat_ids() if not _calling_chat_mismatch(adapter, c)
        ]
        if not candidates:
            return json.dumps({"error": "No active call"})
        chat_id = candidates[0]

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
        if _calling_chat_mismatch(adapter, real_chat_id):
            logger.warning("dc_start_call REFUSED (token of another chat)")
            return _WRONG_CHAT_ERROR

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
        is no incoming [dc:chat=...] token in hand yet, or to cold-DM a
        contact this bot has already seen in a group (see 'address' below).
        """
        args = args or {}
        text = (args.get("text") or "").strip()
        file_path = (args.get("file_path") or "").strip()
        chat_token = args.get("chat_token")
        address = (args.get("address") or "").strip().lower()
        adapter = _active_adapter
        if adapter is None or adapter.rpc is None:
            return json.dumps({"error": "Delta Chat is not connected"})
        if not text and not file_path:
            return json.dumps({"error": "Provide 'text' and/or 'file_path' to send."})

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
        elif address:
            if not _is_valid_email(address):
                return json.dumps(
                    {"error": f"'{address}' is not a valid email address"}
                )
            # why: without this, any agent could cold-DM an arbitrary Delta Chat
            # address it merely knows the string of. Restricting to addresses
            # already seen via get_chat_contacts (i.e. a current member of a
            # group this bot participates in) keeps the blast radius to
            # contacts this bot already has a legitimate reason to know about.
            if not adapter._is_address_in_known_rosters(address):
                return json.dumps(
                    {
                        "error": f"'{address}' is not visible in any group roster "
                        "this bot has fetched — cold-DMing an address outside a "
                        "shared group is not allowed."
                    }
                )
            try:
                contact_id = await adapter.rpc.lookup_contact_id_by_addr(
                    adapter.account_id, address
                )
                if contact_id is None:
                    contact_id = await adapter.rpc.create_contact(
                        adapter.account_id, address, None
                    )
                real_chat_id = await adapter.rpc.create_chat_by_contact_id(
                    adapter.account_id, contact_id
                )
            except Exception as e:
                logger.error(
                    "dc_send_message address resolution failed: %s", e, exc_info=True
                )
                return json.dumps(
                    {"error": f"Could not open a chat with {address}: {e}"}
                )
        else:
            home_channel = os.getenv("DELTACHAT_HOME_CHANNEL")
            if not home_channel:
                return json.dumps(
                    {
                        "error": "No chat_token/address given and DELTACHAT_HOME_CHANNEL "
                        "is not configured — there is no default chat to send to."
                    }
                )
            try:
                real_chat_id = await adapter._resolve_chat_id(home_channel)
            except ValueError:
                return json.dumps(
                    {"error": "DELTACHAT_HOME_CHANNEL is not a valid chat id"}
                )

        if file_path:
            # why: reuses the same remap-then-validate pipeline the reply-flow
            # MEDIA directive uses — /workspace/ paths (Docker sandbox) go
            # through _copy_container_file_to_cache, anything else flows to
            # Hermes's own denylist-aware host-path validator. dc_send_message
            # has no other downstream validator of its own, so this call is
            # the only thing standing between an agent-supplied path and an
            # arbitrary host file read.
            validated_paths = adapter.filter_local_delivery_paths([file_path])
            if not validated_paths:
                return json.dumps(
                    {
                        "error": f"'{file_path}' could not be delivered — not found, "
                        "or blocked by policy. In the Docker sandbox write to "
                        "/workspace/; otherwise use an absolute path that exists "
                        "on the host."
                    }
                )
            try:
                result = await adapter.send_document(
                    str(real_chat_id), validated_paths[0], caption=text or None
                )
            except Exception as e:
                logger.error("dc_send_message file send failed: %s", e, exc_info=True)
                return json.dumps({"error": "Send failed"})
        else:
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

    if _is_on(os.getenv("DELTACHAT_ENABLE_RAW_RPC", "")):
        ctx.register_tool(
            name="dc_rpc_call",
            toolset="deltachat",
            schema={
                "description": (
                    "Call any Delta Chat RPC method directly by name and params. "
                    "Use dc_rpc_spec first to see available methods. "
                    "CAUTION: account-wide access — can modify account data. "
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
                "DELTACHAT_HOME_CHANNEL if configured. To message a specific contact "
                "directly instead of a chat you've already seen traffic in, use "
                "'address' — this opens (or reuses) a 1:1 chat with that contact, but "
                "only works for an address that is a current member of a group this "
                "bot participates in; it cannot cold-DM an arbitrary address. "
                "To push a generated file (e.g. a .md report) instead of/alongside "
                "text, write it to your current working directory (run `pwd` to "
                "find it — in the Docker sandbox that's /workspace/) and pass its "
                "absolute path as 'file_path' — text becomes the caption. DC "
                "auto-detects the viewtype from the extension."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "The message text to send. Optional if 'file_path' is given "
                            "(used as the file's caption); required otherwise."
                        ),
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to a file to attach, e.g. "
                            "'/workspace/report.md' in the Docker sandbox, or any "
                            "absolute host path in a non-Docker deployment. Write "
                            "the file to your current working directory first. "
                            "Sent as a document; 'text' (if given) becomes its "
                            "caption. Rejected if the file doesn't exist or is "
                            "blocked by delivery policy."
                        ),
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
                    "address": {
                        "type": "string",
                        "description": (
                            "Delta Chat email address to message directly, opening a "
                            "1:1 chat if one doesn't already exist. Only usable when "
                            "the address belongs to a member of a group this bot is "
                            "in (e.g. another bot seen in a shared group) — an "
                            "unknown/arbitrary address is rejected. Ignored if "
                            "chat_token is also given."
                        ),
                    },
                },
            },
        },
        handler=_send_message_handler,
        is_async=True,
        emoji="📤",
    )
