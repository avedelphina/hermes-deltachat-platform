"""JSON-RPC transports to communicate with Delta Chat core."""

import itertools
import json
import logging
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from queue import Queue
from threading import Event, Thread
from typing import Any, Iterator, Optional

from ._utils import to_attrdict


class JsonRpcError(Exception):
    """An error occurred in your request to the JSON-RPC API."""


class RpcTransport(ABC):
    """Delta Chat RPC client's transport."""

    @abstractmethod
    def call(self, method: str, *args) -> Any:
        """Request the RPC server to call a function and return its return value if any."""


# Local divergence from upstream deltachat2: see the "vendored deltachat2" note
# in README.md. This file carries patches (dead-server handling here, to_attrdict
# on results, close() guards) that a naive re-vendor would silently drop.

_DISCONNECTED_ERROR = {"error": {"code": -1, "message": "RPC server disconnected"}}


class _Result(Event):
    def __init__(self) -> None:
        self._value: Any = None
        super().__init__()

    def set(self, value: Any) -> None:  # noqa
        self._value = value
        super().set()

    def wait(self, timeout: Optional[float] = None) -> bool:  # noqa
        """Block until a value is set; return True if set, False on timeout.

        Read the value from ``._value`` after a True return.
        """
        return super().wait(timeout)


class IOTransport:
    """Delta Chat RPC transport over IO using external deltachat-rpc-server program."""

    def __init__(self, accounts_dir: Optional[str] = None, rpc_server: str = "deltachat-rpc-server", **kwargs):
        """The given arguments will be passed to subprocess.Popen()"""
        self.logger = logging.getLogger("deltachat2.IOTransport")
        self._rpc_server = rpc_server
        if accounts_dir:
            kwargs["env"] = {
                **kwargs.get("env", os.environ),
                "DC_ACCOUNTS_PATH": str(accounts_dir),
            }

        self._kwargs = kwargs
        self.process: subprocess.Popen
        self.id_iterator: Iterator[int]
        # Map from request ID to the result.
        self.pending_results: dict[int, _Result]
        self.request_queue: Queue
        self.closing: bool
        self.reader_thread: Thread
        self.writer_thread: Thread

    def start(self) -> None:
        """Start the RPC server process."""
        if sys.version_info >= (3, 11):
            # Prevent subprocess from capturing SIGINT.
            kwargs = {"process_group": 0, **self._kwargs}
        else:
            # `process_group` is not supported before Python 3.11.
            kwargs = {"preexec_fn": os.setpgrp, **self._kwargs}  # noqa: PLW1509
        self.process = subprocess.Popen(  # pylint:disable=consider-using-with
            self._rpc_server,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **kwargs,
        )
        self.id_iterator = itertools.count(start=1)
        self.pending_results = {}
        self.request_queue = Queue()
        self.closing = False
        self.reader_thread = Thread(target=self._reader_loop)
        self.reader_thread.start()
        self.writer_thread = Thread(target=self._writer_loop)
        self.writer_thread.start()

    def close(self, timeout: float = 5.0, stop_io_timeout: float = 35.0) -> None:
        """Stop the RPC server process and wait for the transport threads to finish.

        Blocking: callers on an event loop must run it in a thread (the adapter
        does). Every wait is bounded — a wedged server used to freeze the whole
        gateway in the stop_io call or the reader join. stop_io gets
        *stop_io_timeout*, above core's own 30 s IMAP/SMTP shutdown budget, so a
        slow but healthy server is not killed mid-shutdown; each later step
        gets *timeout*. A server that ignores stdin EOF is killed.
        """
        if not hasattr(self, "process"):
            return  # start() was never called or failed before process was created
        self.closing = True
        try:
            self._call("stop_io_for_all_accounts", (), timeout=stop_io_timeout)
        except Exception:
            pass
        assert self.process.stdin
        self.request_queue.put(None)
        # The writer only flushes what is queued; it can only stay blocked if
        # the server stopped reading, and then the kill below breaks the pipe.
        self.writer_thread.join(timeout)
        if not self.writer_thread.is_alive():
            try:
                self.process.stdin.close()  # EOF asks the server to exit
            except OSError:
                # why: a writer that died on EPIPE leaves its data buffered, so
                # close() re-flushes and raises BrokenPipeError; letting that
                # escape skipped the kill below and left the server running.
                pass
        try:
            self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            # why no SIGTERM step: rpc-server cancels the same token on stdin
            # EOF and on SIGTERM, so after EOF a SIGTERM changes nothing.
            self.logger.warning("deltachat-rpc-server did not exit within %ss, killing it", timeout)
            self.process.kill()
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                # SIGKILL cannot take effect in uninterruptible sleep (D state).
                self.logger.error(
                    "deltachat-rpc-server (pid %s) still alive %ss after SIGKILL", self.process.pid, timeout
                )
        self.writer_thread.join(timeout)
        self.reader_thread.join(timeout)
        try:
            self.process.stdin.close()
        except OSError:
            pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def _fail_all_pending(self) -> None:
        """Resolve every waiting caller with the disconnect error.

        Called from both the reader and writer loop when the RPC server dies, so
        no caller blocks forever on a request that will never be answered. A
        call() that registers between the snapshot and clear() is dropped
        without being resolved; its own poll on _server_dead() catches that
        within one slice.
        """
        for pending in list(self.pending_results.values()):
            pending.set(_DISCONNECTED_ERROR)
        self.pending_results.clear()

    def _server_dead(self) -> bool:
        """True once no further request can be answered.

        That is: the RPC server subprocess has exited (any exit code), or the
        reader or writer thread has died, so nothing will ever send the
        request or deliver its reply. Before start() none of them exists,
        which does not count as dead.
        """
        if not hasattr(self, "process"):
            return False
        if self.process.poll() is not None:
            return True
        for name in ("reader_thread", "writer_thread"):
            thread = getattr(self, name, None)
            # ident is None until the thread has started.
            if thread is not None and thread.ident is not None and not thread.is_alive():
                return True
        return False

    def _reader_loop(self) -> None:
        try:
            assert self.process.stdout
            while True:
                line = self.process.stdout.readline()
                if not line:  # EOF
                    break
                try:
                    response = json.loads(line)
                except ValueError:
                    # A stray non-JSON line (a panic message, a log line on
                    # stdout) must not kill the reader: the server may be fine,
                    # and with no reader every later call would hang.
                    self.logger.warning("Ignoring non-JSON line from RPC server: %r", line[:200])
                    continue
                # why: valid JSON need not be an object; `in` on a number or a
                # list would raise and kill the reader.
                is_obj = isinstance(response, dict)
                if is_obj and "id" in response:
                    # The caller may already have given up on a dead server
                    # (see call()); dropping its reply must not kill the reader.
                    pending = self.pending_results.pop(response["id"], None)
                    if pending is not None:
                        pending.set(response)
                else:
                    # Not the whole response at WARNING: a result can carry
                    # message text or addresses. The error object explains it.
                    self.logger.warning(
                        "Got a response without ID: %s",
                        response.get("error", sorted(response)) if is_obj else type(response).__name__,
                    )
                    self.logger.debug("Response without ID: %s", response)
        except Exception:
            # Log an exception if the reader loop dies.
            self.logger.exception("Exception in the reader loop")
            # why: no reply can be read any more, but a live process passes the
            # adapter's exit-code probe. Retire it so the gateway reconnects.
            if not self.closing:
                self.process.kill()
        finally:
            self._fail_all_pending()

    def _writer_loop(self) -> None:
        """Writer loop ensuring only a single thread writes requests."""
        try:
            assert self.process.stdin
            while True:
                request = self.request_queue.get()
                if not request:
                    break
                data = (json.dumps(request) + "\n").encode()
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except Exception:
            # Log an exception if the writer loop dies.
            self.logger.exception("Exception in the writer loop")
        finally:
            # A dead writer means no queued request will ever be sent; wake
            # every caller instead of letting them block forever.
            self._fail_all_pending()

    def call(self, method: str, *args) -> Any:
        """Request the RPC server to call a function and return its return value if any."""
        return self._call(method, args)

    def _call(self, method: str, args: tuple, timeout: Optional[float] = None) -> Any:
        """call() with an optional deadline; only close() sets one.

        Ordinary calls stay unbounded on purpose: get_next_event long-polls,
        and configuring a transport can legitimately take minutes. A dead
        server is caught by the polling below instead.
        """
        if self._server_dead():
            raise JsonRpcError(_DISCONNECTED_ERROR["error"])

        request_id = next(self.id_iterator)
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": args,
            "id": request_id,
        }
        # why lazy %s: an f-string here rendered every request and response
        # (whole messages, passwords, invite links) on every call, debug or not.
        self.logger.debug("RPC request: method=%s, params=%s", method, args)

        result = self.pending_results[request_id] = _Result()
        self.request_queue.put(request)
        # Poll in short slices instead of an untimed wait: a legitimate slow
        # call (network round-trip) keeps waiting as long as the server is
        # alive, but a server that dies mid-call surfaces as an error within a
        # slice instead of hanging this thread forever.
        deadline = None if timeout is None else time.monotonic() + timeout
        while not result.wait(timeout=1.0 if deadline is None else min(1.0, max(0.0, deadline - time.monotonic()))):
            if deadline is not None and time.monotonic() >= deadline:
                self.pending_results.pop(request_id, None)
                raise JsonRpcError({"code": -1, "message": f"RPC call {method} timed out after {timeout}s"})
            if self._server_dead():
                # A reply the server wrote just before exiting can still be in
                # the pipe; give the reader one more slice to deliver it (or to
                # hit EOF and fail us) rather than reporting a call that
                # succeeded, e.g. a sent message, as failed.
                if result.wait(timeout=1.0):
                    break
                self.pending_results.pop(request_id, None)
                raise JsonRpcError(_DISCONNECTED_ERROR["error"])
        response = result._value

        self.logger.debug("RPC response for %s: %s", method, response)

        if "error" in response:
            raise JsonRpcError(response["error"])
        if "result" in response:
            return to_attrdict(response["result"])
        return None
