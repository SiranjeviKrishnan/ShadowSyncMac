"""
ShadowSync - IPC Server
Exposes a Unix domain socket at /tmp/shadowsync.sock
The HTML dashboard and any external clients send JSON commands and get JSON back.

Protocol: newline-delimited JSON  (each message is one JSON line)

Commands:
  {"cmd": "status"}              → engine stats + peer list
  {"cmd": "peers"}               → list of active peers
  {"cmd": "transfers"}           → active transfers
  {"cmd": "conflicts"}           → recent conflict history
  {"cmd": "pause"}               → pause file watcher
  {"cmd": "resume"}              → resume file watcher
  {"cmd": "force_sync"}          → queue all local files for sync
  {"cmd": "add_watch", "path": "..."} → add a new watch directory
  {"cmd": "subscribe"}           → stay connected; engine pushes events as they happen

Events pushed to subscribers:
  {"event": "sync_ok", "detail": "..."}
  {"event": "peer_connected", "detail": "..."}
  {"event": "conflict", "detail": "..."}
  {"event": "progress", "data": {...}}
"""

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("shadowsync.ipc")

IPC_SOCKET_PATH = "/tmp/shadowsync.sock"


class IPCServer:
    """
    Listens on a Unix domain socket.
    Bridges the HTML dashboard / external tools ↔ SyncEngine.
    """

    def __init__(self):
        self._engine = None         # Set by app.py after engine is created
        self._watcher = None
        self._registry = None
        self._paused = False

        self._subscribers: list[socket.socket] = []
        self._sub_lock = threading.Lock()

        self._server_sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def attach(self, engine, watcher=None, registry=None):
        """Called by app.py to give IPC access to the live components."""
        self._engine = engine
        self._watcher = watcher
        self._registry = registry

    def start(self):
        # Remove stale socket
        try:
            os.unlink(IPC_SOCKET_PATH)
        except OSError:
            pass

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(IPC_SOCKET_PATH)
        self._server_sock.listen(10)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="ipc-server")
        self._thread.start()
        logger.info(f"[IPC] Listening on {IPC_SOCKET_PATH}")

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        try:
            os.unlink(IPC_SOCKET_PATH)
        except OSError:
            pass

    def broadcast_event(self, event_type: str, detail: str, data: dict = None):
        """Push an event to all subscribed clients (dashboard, menubar, etc.)."""
        msg = {"event": event_type, "detail": detail, "ts": time.time()}
        if data:
            msg["data"] = data
        self._broadcast(msg)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _accept_loop(self):
        while self._running:
            try:
                self._server_sock.settimeout(1.0)
                conn, _ = self._server_sock.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                    name="ipc-client"
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn: socket.socket):
        conn.settimeout(30)
        buf = b""
        try:
            while self._running:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        response = self._dispatch(msg, conn)
                        if response is not None:
                            conn.sendall(json.dumps(response).encode() + b"\n")
                    except json.JSONDecodeError as e:
                        conn.sendall(json.dumps({"error": f"bad json: {e}"}).encode() + b"\n")
                    except Exception as e:
                        logger.error(f"[IPC] Dispatch error: {e}")
                        try:
                            conn.sendall(json.dumps({"error": str(e)}).encode() + b"\n")
                        except Exception:
                            pass
        except (ConnectionError, OSError):
            pass
        finally:
            with self._sub_lock:
                self._subscribers = [s for s in self._subscribers if s is not conn]
            conn.close()

    def _dispatch(self, msg: dict, conn: socket.socket) -> Optional[dict]:
        cmd = msg.get("cmd", "")

        if cmd == "status":
            stats = self._engine.get_stats() if self._engine else {}
            peers = []
            if self._registry:
                peers = [p.to_dict() for p in self._registry.get_all()]
            return {
                "cmd": "status",
                "stats": stats,
                "peers": peers,
                "paused": self._paused,
                "ts": time.time(),
            }

        elif cmd == "peers":
            peers = []
            if self._registry:
                peers = [p.to_dict() for p in self._registry.get_all()]
            return {"cmd": "peers", "peers": peers}

        elif cmd == "transfers":
            transfers = self._engine.get_active_transfers() if self._engine else []
            return {"cmd": "transfers", "transfers": transfers}

        elif cmd == "conflicts":
            history = self._engine.get_conflict_history(20) if self._engine else []
            return {"cmd": "conflicts", "history": history}

        elif cmd == "pause":
            if self._watcher and not self._paused:
                self._watcher.stop()
                self._paused = True
                logger.info("[IPC] Sync paused")
            return {"cmd": "pause", "paused": True}

        elif cmd == "resume":
            if self._paused:
                if self._watcher:
                    self._watcher.start()
                self._paused = False
                logger.info("[IPC] Sync resumed")
            return {"cmd": "resume", "paused": False}

        elif cmd == "force_sync":
            if self._engine and not self._paused:
                threading.Thread(
                    target=self._do_force_sync,
                    daemon=True
                ).start()
                return {"cmd": "force_sync", "status": "started"}
            return {"cmd": "force_sync", "status": "skipped", "reason": "paused or no engine"}

        elif cmd == "add_watch":
            path = msg.get("path", "")
            if path and os.path.isdir(path) and self._watcher:
                self._watcher.add_directory(path)
                return {"cmd": "add_watch", "status": "ok", "path": path}
            return {"cmd": "add_watch", "status": "error", "reason": "invalid path or watcher not ready"}

        elif cmd == "subscribe":
            with self._sub_lock:
                self._subscribers.append(conn)
            # Ack the subscription
            conn.sendall(json.dumps({"event": "subscribed", "ts": time.time()}).encode() + b"\n")
            return None   # No immediate reply — stay open for push events

        elif cmd == "ping":
            return {"cmd": "pong", "ts": time.time()}

        else:
            return {"error": f"unknown command: {cmd!r}"}

    def _do_force_sync(self):
        """Queue all files in all watch dirs."""
        if not self._engine:
            return
        from core.watcher import ChangeEvent
        if not self._watcher:
            return
        for watch_dir in self._watcher.watch_dirs:
            for root, _, files in os.walk(watch_dir):
                for f in files:
                    path = os.path.join(root, f)
                    try:
                        ev = ChangeEvent("modified", path)
                        self._engine.on_file_change(ev)
                    except Exception:
                        pass

    def _broadcast(self, msg: dict):
        payload = json.dumps(msg).encode() + b"\n"
        dead = []
        with self._sub_lock:
            for sock in self._subscribers:
                try:
                    sock.sendall(payload)
                except (OSError, BrokenPipeError):
                    dead.append(sock)
            for s in dead:
                self._subscribers.remove(s)


# ── Singleton ─────────────────────────────────────────────────────────────────
_ipc_server = IPCServer()


def get_ipc_server() -> IPCServer:
    return _ipc_server
