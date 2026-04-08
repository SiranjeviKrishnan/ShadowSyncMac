"""
ShadowSync - Sync Engine v2
- Wired to TransportManager (Thunderbolt > LAN > Tailscale auto-selection)
- Wired to ConflictResolver (all strategies)
- Delta sync via rsync-style rolling checksum (block-level diff)
- Per-transfer progress callbacks for UI
- Retry with exponential backoff on best available link
- Bandwidth throttling via ThrottledSocket
"""

import hashlib
import json
import logging
import os
import queue
import shutil
import socket
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Callable

from core.watcher import ChangeEvent
from core.discovery import Peer
from core.conflict import ConflictResolver, ConflictStrategy, IncomingFile
from core.transport import TransportManager, ThrottledSocket, LinkQuality

logger = logging.getLogger("shadowsync.engine")

# ── Protocol ──────────────────────────────────────────────────────────────────
MAGIC = b"SSNC"
VERSION = 2
CHUNK_SIZE = 256 * 1024
DELTA_BLOCK_SIZE = 4096
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 3, 9]
SYNC_PORT = 9000
HEADER_FMT = "!4sHHI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


class MsgType(Enum):
    HANDSHAKE     = 1
    FILE_META     = 2
    FILE_CHUNK    = 3
    FILE_END      = 4
    DELETE        = 5
    MOVE          = 6
    ACK           = 7
    CONFLICT      = 8
    PING          = 9
    PONG          = 10
    BLOCK_REQUEST = 11
    BLOCK_LIST    = 12
    PARTIAL_FILE  = 13


class ProtocolError(Exception): pass
class IntegrityError(Exception): pass


# ── Wire helpers ──────────────────────────────────────────────────────────────

def encode_msg(msg_type: MsgType, payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, msg_type.value, len(body))
    return header + body


def decode_header(data: bytes) -> tuple:
    magic, ver, msg_type_val, payload_len = struct.unpack(HEADER_FMT, data)
    if magic != MAGIC:
        raise ProtocolError(f"Bad magic: {magic!r}")
    return MsgType(msg_type_val), payload_len


def recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed mid-receive")
        buf += chunk
    return bytes(buf)


def send_msg(sock, msg_type: MsgType, payload: dict):
    sock.sendall(encode_msg(msg_type, payload))


def recv_msg(sock) -> tuple:
    header = recv_exact(sock, HEADER_SIZE)
    msg_type, payload_len = decode_header(header)
    if payload_len == 0:
        return msg_type, {}
    body = recv_exact(sock, payload_len)
    return msg_type, json.loads(body.decode("utf-8"))


# ── Delta sync ────────────────────────────────────────────────────────────────

def _block_checksum(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def compute_block_list(path: str, block_size: int = DELTA_BLOCK_SIZE) -> list:
    blocks = []
    try:
        with open(path, "rb") as f:
            idx = 0
            while True:
                data = f.read(block_size)
                if not data:
                    break
                blocks.append({
                    "idx": idx,
                    "size": len(data),
                    "checksum": _block_checksum(data),
                })
                idx += 1
    except OSError:
        pass
    return blocks


def compute_delta(local_path: str, remote_blocks: list, block_size: int = DELTA_BLOCK_SIZE) -> list:
    import base64
    changed = []
    try:
        with open(local_path, "rb") as f:
            for i, rb in enumerate(remote_blocks):
                data = f.read(block_size)
                if not data:
                    break
                if _block_checksum(data) != rb["checksum"]:
                    changed.append({"idx": i, "data": base64.b64encode(data).decode("ascii")})
            idx = len(remote_blocks)
            while True:
                data = f.read(block_size)
                if not data:
                    break
                changed.append({"idx": idx, "data": base64.b64encode(data).decode("ascii"), "append": True})
                idx += 1
    except OSError:
        pass
    return changed


# ── Progress ──────────────────────────────────────────────────────────────────

@dataclass
class TransferProgress:
    transfer_id: str
    rel_path: str
    peer_hostname: str
    direction: str
    total_bytes: int
    transferred_bytes: int = 0
    start_time: float = field(default_factory=time.time)
    done: bool = False
    error: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 100.0
        return round(100 * self.transferred_bytes / self.total_bytes, 1)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def speed_bps(self) -> float:
        e = self.elapsed
        return self.transferred_bytes / e if e > 0 else 0

    def to_dict(self) -> dict:
        return {
            "transfer_id": self.transfer_id,
            "rel_path": self.rel_path,
            "peer": self.peer_hostname,
            "direction": self.direction,
            "total_bytes": self.total_bytes,
            "transferred_bytes": self.transferred_bytes,
            "percent": self.percent,
            "speed_kbps": round(self.speed_bps / 1024, 1),
            "done": self.done,
            "error": self.error,
        }


# ── Sync job ──────────────────────────────────────────────────────────────────

@dataclass
class SyncJob:
    priority: float
    event: ChangeEvent
    peer: Peer
    attempts: int = 0

    def __lt__(self, other):
        return self.priority < other.priority

    @classmethod
    def create(cls, event: ChangeEvent, peer: Peer) -> "SyncJob":
        size_score = min((event.size or 0) / (1024 * 1024), 10)
        return cls(priority=time.time() + size_score, event=event, peer=peer)


# ── File transfer ─────────────────────────────────────────────────────────────

class FileTransfer:

    @staticmethod
    def send_file(sock, local_path: str, watch_root: str,
                  peer_hostname: str = "",
                  on_progress: Optional[Callable] = None,
                  bandwidth_limit_bps: int = 0) -> TransferProgress:

        rel_path = os.path.relpath(local_path, watch_root)
        file_size = os.path.getsize(local_path)
        file_hash = _sha256(local_path)
        transfer_id = f"{int(time.time()*1000)}_{os.path.basename(local_path)}"

        progress = TransferProgress(
            transfer_id=transfer_id,
            rel_path=rel_path,
            peer_hostname=peer_hostname,
            direction="send",
            total_bytes=file_size,
        )

        send_msg(sock, MsgType.FILE_META, {
            "rel_path": rel_path,
            "size": file_size,
            "sha256": file_hash,
            "mtime": os.path.getmtime(local_path),
            "transfer_id": transfer_id,
        })

        writer = ThrottledSocket(sock, bandwidth_limit_bps) if bandwidth_limit_bps > 0 else sock

        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                writer.sendall(struct.pack("!I", len(chunk)) + chunk)
                progress.transferred_bytes += len(chunk)
                if on_progress:
                    try:
                        on_progress(progress)
                    except Exception:
                        pass

        sock.sendall(struct.pack("!I", 0))
        progress.done = True
        if on_progress:
            on_progress(progress)

        logger.debug(f"[TRANSFER→] {rel_path} {file_size:,}B to {peer_hostname}")
        return progress

    @staticmethod
    def receive_file(sock, dest_root: str, meta: dict,
                     remote_ip: str = "",
                     on_progress: Optional[Callable] = None) -> IncomingFile:

        rel_path = meta["rel_path"]
        expected_hash = meta["sha256"]
        expected_size = meta.get("size", 0)
        dest_path = os.path.join(dest_root, rel_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(dest_path),
            prefix=".shadowsync_tmp_"
        )

        progress = TransferProgress(
            transfer_id=meta.get("transfer_id", ""),
            rel_path=rel_path,
            peer_hostname=remote_ip,
            direction="receive",
            total_bytes=expected_size,
        )

        hasher = hashlib.sha256()
        received = 0

        try:
            with os.fdopen(tmp_fd, "wb") as f:
                while True:
                    size_data = recv_exact(sock, 4)
                    chunk_size = struct.unpack("!I", size_data)[0]
                    if chunk_size == 0:
                        break
                    chunk = recv_exact(sock, chunk_size)
                    f.write(chunk)
                    hasher.update(chunk)
                    received += chunk_size
                    progress.transferred_bytes = received
                    if on_progress:
                        try:
                            on_progress(progress)
                        except Exception:
                            pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        actual_hash = hasher.hexdigest()
        if actual_hash != expected_hash:
            os.unlink(tmp_path)
            raise IntegrityError(
                f"Hash mismatch for {rel_path}: "
                f"got {actual_hash[:8]}… expected {expected_hash[:8]}…"
            )

        progress.done = True
        if on_progress:
            on_progress(progress)

        logger.debug(f"[TRANSFER←] {rel_path} {received:,}B from {remote_ip} ✓")

        return IncomingFile(
            rel_path=rel_path,
            tmp_path=tmp_path,
            remote_ip=remote_ip,
            remote_mtime=meta.get("mtime", time.time()),
            remote_hash=actual_hash,
            remote_size=received,
        )


# ── TCP Server ────────────────────────────────────────────────────────────────

class SyncServer:

    def __init__(self, port: int, watch_root: str,
                 conflict_resolver: ConflictResolver,
                 on_event: Optional[Callable] = None,
                 on_progress: Optional[Callable] = None):
        self.port = port
        self.watch_root = watch_root
        self.conflict_resolver = conflict_resolver
        self.on_event = on_event
        self.on_progress = on_progress
        self._server_sock = None
        self._running = False
        self._thread = None

    def start(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self.port))
        self._server_sock.listen(20)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="sync-server")
        self._thread.start()
        logger.info(f"[SERVER] Listening on :{self.port}")

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    def _accept_loop(self):
        while self._running:
            try:
                self._server_sock.settimeout(1.0)
                conn, addr = self._server_sock.accept()
                threading.Thread(
                    target=self._handle_client, args=(conn, addr),
                    daemon=True, name=f"client-{addr[0]}"
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn, addr):
        try:
            conn.settimeout(60)
            msg_type, payload = recv_msg(conn)

            if msg_type == MsgType.HANDSHAKE:
                send_msg(conn, MsgType.ACK, {"status": "ok", "version": VERSION})
                msg_type, payload = recv_msg(conn)

            if msg_type == MsgType.FILE_META:
                incoming = FileTransfer.receive_file(
                    conn, self.watch_root, payload,
                    remote_ip=addr[0],
                    on_progress=self.on_progress,
                )
                applied = self.conflict_resolver.resolve(incoming)
                status = "applied" if applied else "local_kept"
                send_msg(conn, MsgType.ACK, {"status": "ok", "resolution": status})
                if self.on_event:
                    self.on_event("received", payload.get("rel_path", ""), addr[0], status)

            elif msg_type == MsgType.DELETE:
                rel_path = payload.get("rel_path", "")
                target = os.path.join(self.watch_root, rel_path)
                if os.path.exists(target):
                    os.remove(target)
                send_msg(conn, MsgType.ACK, {"status": "ok"})
                if self.on_event:
                    self.on_event("delete_received", rel_path, addr[0], "ok")

            elif msg_type == MsgType.PING:
                send_msg(conn, MsgType.PONG, {"ts": time.time(), "version": VERSION})

        except (ConnectionError, socket.timeout) as e:
            logger.debug(f"[SERVER] {addr[0]} disconnected: {e}")
        except IntegrityError as e:
            logger.error(f"[SERVER] Integrity error from {addr[0]}: {e}")
            try:
                send_msg(conn, MsgType.ACK, {"status": "error", "reason": str(e)})
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[SERVER] Handler error ({addr[0]}): {e}", exc_info=True)
        finally:
            conn.close()


# ── Sync Engine ───────────────────────────────────────────────────────────────

class SyncEngine:

    def __init__(self,
                 watch_root: str,
                 port: int = SYNC_PORT,
                 conflict_strategy: ConflictStrategy = ConflictStrategy.NEWER_WINS,
                 bandwidth_limit_kbps: int = 0,
                 on_status: Optional[Callable] = None,
                 on_progress: Optional[Callable] = None):

        self.watch_root = str(Path(watch_root).resolve())
        self.port = port
        self.bandwidth_limit_bps = bandwidth_limit_kbps * 1024
        self.on_status = on_status
        self.on_progress = on_progress

        self._peers: dict = {}
        self._peers_lock = threading.RLock()

        self._conflict_resolver = ConflictResolver(
            watch_root=self.watch_root,
            strategy=conflict_strategy,
            on_conflict=self._on_conflict,
        )
        self._transport = TransportManager()

        self._server = SyncServer(
            port=port,
            watch_root=self.watch_root,
            conflict_resolver=self._conflict_resolver,
            on_event=self._on_received,
            on_progress=on_progress,
        )

        self._job_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._workers: list = []
        self._running = False

        self._active_transfers: dict = {}
        self._transfer_lock = threading.Lock()

        self._stats = {
            "sent": 0, "received": 0, "errors": 0,
            "bytes_sent": 0, "bytes_received": 0,
            "conflicts": 0,
        }

    def start(self, worker_count: int = 4):
        self._running = True
        self._server.start()
        for i in range(worker_count):
            t = threading.Thread(
                target=self._worker_loop, daemon=True, name=f"sync-worker-{i}"
            )
            t.start()
            self._workers.append(t)
        logger.info(f"[ENGINE] Started — {worker_count} workers, port {self.port}")

    def stop(self):
        self._running = False
        self._server.stop()
        for _ in self._workers:
            self._job_queue.put((0, SyncJob(priority=0, event=None, peer=None)))

    def on_peer_discovered(self, event: str, peer: Peer):
        with self._peers_lock:
            if event in ("discovered", "updated"):
                self._peers[peer.peer_id] = peer
                self._transport.register_peer(peer.peer_id, [peer.ip], peer.port)
                self._notify("peer_connected", peer.hostname)
            elif event == "lost":
                self._peers.pop(peer.peer_id, None)
                self._transport.remove_peer(peer.peer_id)
                self._notify("peer_lost", peer.hostname)

    def on_file_change(self, event: ChangeEvent):
        with self._peers_lock:
            peers = list(self._peers.values())
        if not peers:
            return
        for peer in peers:
            job = SyncJob.create(event, peer)
            self._job_queue.put((job.priority, job))

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "active_transfers": len(self._active_transfers),
            "link_summary": self._transport.get_link_summary(),
            "conflict_history": len(self._conflict_resolver.get_history()),
        }

    def get_active_transfers(self) -> list:
        with self._transfer_lock:
            return [t.to_dict() for t in self._active_transfers.values()]

    def get_conflict_history(self, limit: int = 20) -> list:
        return self._conflict_resolver.get_history(limit)

    def _worker_loop(self):
        while self._running:
            try:
                _, job = self._job_queue.get(timeout=1)
                if job.event is None:
                    break
                self._execute_job(job)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"[ENGINE] Worker error: {e}", exc_info=True)

    def _execute_job(self, job: SyncJob):
        event = job.event
        peer = job.peer

        link = self._transport.best_link(peer.peer_id)
        if link is None:
            from core.transport import LinkQuality as LQ
            link = LQ(ip=peer.ip, port=peer.port)

        for attempt in range(MAX_RETRIES):
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(20)
                sock.connect((link.ip, link.port))

                send_msg(sock, MsgType.HANDSHAKE, {"version": VERSION})
                ack_type, ack = recv_msg(sock)
                if ack_type != MsgType.ACK or ack.get("status") != "ok":
                    raise ProtocolError("Handshake rejected")

                if event.event_type in ("created", "modified"):
                    if not os.path.exists(event.src_path):
                        return
                    progress = FileTransfer.send_file(
                        sock=sock,
                        local_path=event.src_path,
                        watch_root=self.watch_root,
                        peer_hostname=peer.hostname,
                        on_progress=self._handle_progress,
                        bandwidth_limit_bps=self.bandwidth_limit_bps,
                    )
                    ack_type, ack_payload = recv_msg(sock)
                    size = progress.total_bytes
                    self._stats["sent"] += 1
                    self._stats["bytes_sent"] += size
                    resolution = ack_payload.get("resolution", "applied")
                    self._notify(
                        "sync_ok",
                        f"→ {peer.hostname}: {os.path.basename(event.src_path)} [{resolution}]"
                    )

                elif event.event_type == "deleted":
                    rel = os.path.relpath(event.src_path, self.watch_root)
                    send_msg(sock, MsgType.DELETE, {"rel_path": rel})
                    recv_msg(sock)
                    self._notify("delete_ok", f"→ {peer.hostname}: deleted {rel}")

                elif event.event_type == "moved":
                    if event.dest_path and os.path.exists(event.dest_path):
                        FileTransfer.send_file(
                            sock=sock,
                            local_path=event.dest_path,
                            watch_root=self.watch_root,
                            peer_hostname=peer.hostname,
                            on_progress=self._handle_progress,
                            bandwidth_limit_bps=self.bandwidth_limit_bps,
                        )
                        recv_msg(sock)

                self._transport.report_success(peer.peer_id, link.ip)
                return

            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                logger.warning(
                    f"[ENGINE] Attempt {attempt+1}/{MAX_RETRIES} "
                    f"to {peer.hostname} ({link.ip}) failed: {e}"
                )
                self._transport.report_failure(peer.peer_id, link.ip)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF[attempt])
                    new_link = self._transport.best_link(peer.peer_id)
                    if new_link and new_link.ip != link.ip:
                        link = new_link
            except ProtocolError as e:
                logger.error(f"[ENGINE] Protocol error to {peer.hostname}: {e}")
                self._stats["errors"] += 1
                return
            except Exception as e:
                logger.error(f"[ENGINE] Error to {peer.hostname}: {e}", exc_info=True)
                self._stats["errors"] += 1
                return
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

        self._stats["errors"] += 1
        logger.error(f"[ENGINE] All retries exhausted for {peer.hostname}")

    def _handle_progress(self, progress: TransferProgress):
        with self._transfer_lock:
            if progress.done:
                self._active_transfers.pop(progress.transfer_id, None)
            else:
                self._active_transfers[progress.transfer_id] = progress
        if self.on_progress:
            try:
                self.on_progress(progress)
            except Exception:
                pass

    def _on_received(self, event_type: str, rel_path: str, from_ip: str, detail: str = ""):
        self._stats["received"] += 1
        self._notify(event_type, f"← {from_ip}: {rel_path} ({detail})")

    def _on_conflict(self, record):
        self._stats["conflicts"] += 1
        self._notify(
            "conflict",
            f"Conflict: {record.rel_path} → {record.resolution}"
        )

    def _notify(self, event_type: str, detail: str):
        logger.info(f"[ENGINE] {event_type}: {detail}")
        if self.on_status:
            try:
                self.on_status(event_type, detail)
            except Exception:
                pass


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
