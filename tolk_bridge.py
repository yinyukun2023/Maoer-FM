from __future__ import annotations

import base64
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


BRIDGE_DIR = Path(__file__).resolve().parent / "tolk_x86"
BRIDGE_EXE = BRIDGE_DIR / "TolkBridge.exe"
RETRY_SECONDS = 30.0
RESPONSE_TIMEOUT_SECONDS = 5.0


def debug_log(message: str) -> None:
    if os.environ.get("MAOER_DEBUG"):
        print(f"[tolk] {message}", flush=True)


class TolkBridge:
    """Send speech to x86 Tolk without blocking wx's UI thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: queue.Queue[str | None] = queue.Queue(maxsize=128)
        self._ready = False
        self._closed = False
        self._starting = False
        self._last_attempt = float("-inf")
        self._process: subprocess.Popen[str] | None = None
        self._start_if_needed()

    def speak(self, text: str) -> bool:
        with self._lock:
            ready = self._ready and not self._closed
        if not ready:
            self._start_if_needed()
            return False
        try:
            self._pending.put_nowait(text)
        except queue.Full:
            # Live captions should not keep speaking stale lines indefinitely.
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self._pending.put_nowait(text)
            except queue.Full:
                pass
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ready = False
            process = self._process
        try:
            self._pending.put_nowait(None)
        except queue.Full:
            try:
                self._pending.get_nowait()
                self._pending.put_nowait(None)
            except queue.Empty:
                pass
        if process is not None:
            self._terminate(process)

    def _start_if_needed(self) -> None:
        if os.name != "nt" or not BRIDGE_EXE.is_file():
            return
        with self._lock:
            now = time.monotonic()
            if self._closed or self._starting or self._ready or now - self._last_attempt < RETRY_SECONDS:
                return
            self._starting = True
            self._last_attempt = now
        threading.Thread(target=self._run, daemon=True, name="tolk-bridge").start()

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            pass

    def _read_status(self, process: subprocess.Popen[str]) -> str:
        watchdog = threading.Timer(RESPONSE_TIMEOUT_SECONDS, self._terminate, args=(process,))
        watchdog.daemon = True
        watchdog.start()
        try:
            return process.stderr.readline().strip() if process.stderr is not None else ""
        finally:
            watchdog.cancel()

    def _run(self) -> None:
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [str(BRIDGE_EXE)],
                cwd=str(BRIDGE_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            with self._lock:
                self._process = process
                if self._closed:
                    return
            status = self._read_status(process)
            if status != "READY":
                debug_log(f"screen reader not detected: {status or 'no response'}")
                return
            with self._lock:
                if self._closed:
                    return
                self._ready = True
            while True:
                message = self._pending.get()
                if message is None:
                    return
                if process.stdin is None:
                    return
                encoded = base64.b64encode(message.encode("utf-8")).decode("ascii")
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
                if self._read_status(process) != "OK":
                    debug_log("Tolk output failed; using UIA fallback")
                    return
        except (OSError, ValueError, UnicodeError) as exc:
            debug_log(f"bridge failed: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._ready = False
                self._starting = False
                self._process = None
            while True:
                try:
                    self._pending.get_nowait()
                except queue.Empty:
                    break
            if process is not None:
                self._terminate(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                if process.stdin is not None:
                    process.stdin.close()
                if process.stderr is not None:
                    process.stderr.close()
