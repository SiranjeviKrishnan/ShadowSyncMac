"""
ShadowSync - Peer Discovery
Uses mDNS/Bonjour for Mac-native zero-config discovery (like AirDrop).
Falls back to UDP broadcast for non-Bonjour environments.
"""

import socket
import threading
import time
import json
import uuid
import logging
import platform
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, Optional

logger = logging.getLogger("shadowsync.discovery")

SHADOWSYNC_SERVICE = "_shadowsync._tcp.local."
UDP_BROADCAST_PORT = 9001
UDP_BROADCAST_INTERVAL = 5
PEER_TIMEOUT = 30   # seconds before a peer is considered gone


@dataclass
class Peer:
    peer_id: str
    hostname: str
    ip: str
    port: int
    version: str = "2.0"
    last_seen: float = field(default_factory=time.time)
    capabilities: list = field(default_factory=lambda: ["sync", "delta"])

    def is_alive(self) -> bool:
        return (time.time() - self.last_seen) < PEER_TIMEOUT

    def to_dict(self) -> dict:
        return asdict(self)


class PeerRegistry:
    """Thread-safe registry of discovered peers."""

    def __init__(self):
        self._peers: Dict[str, Peer] = {}
        self._lock = threading.RLock()
        self._callbacks: list[Callable[[str, Peer], None]] = []  # (event, peer)

    def register_callback(self, cb: Callable[[str, Peer], None]):
        self._callbacks.append(cb)

    def add_or_update(self, peer: Peer):
        with self._lock:
            is_new = peer.peer_id not in self._peers
            peer.last_seen = time.time()
            self._peers[peer.peer_id] = peer
        event = "discovered" if is_new else "updated"
        logger.info(f"[DISCOVERY] Peer {event}: {peer.hostname} @ {peer.ip}:{peer.port}")
        for cb in self._callbacks:
            try:
                cb(event, peer)
            except Exception as e:
                logger.error(f"[DISCOVERY] Callback error: {e}")

    def remove(self, peer_id: str):
        with self._lock:
            peer = self._peers.pop(peer_id, None)
        if peer:
            logger.info(f"[DISCOVERY] Peer lost: {peer.hostname}")
            for cb in self._callbacks:
                try:
                    cb("lost", peer)
                except Exception:
                    pass

    def get_all(self) -> list[Peer]:
        with self._lock:
            return [p for p in self._peers.values() if p.is_alive()]

    def get(self, peer_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(peer_id)

    def prune_stale(self):
        with self._lock:
            stale = [pid for pid, p in self._peers.items() if not p.is_alive()]
        for pid in stale:
            self.remove(pid)


class UDPDiscovery:
    """
    UDP broadcast discovery — works on local LAN without mDNS.
    Broadcasts presence and listens for peers.
    """

    def __init__(self, tcp_port: int, registry: PeerRegistry):
        self.tcp_port = tcp_port
        self.registry = registry
        self.my_id = str(uuid.uuid4())
        self.hostname = socket.gethostname()
        self._running = False
        self._threads: list[threading.Thread] = []

    def _beacon_payload(self) -> bytes:
        return json.dumps({
            "peer_id": self.my_id,
            "hostname": self.hostname,
            "port": self.tcp_port,
            "version": "2.0",
            "service": "shadowsync",
            "capabilities": ["sync", "delta", "conflict_resolution"],
        }).encode("utf-8")

    def _sender_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        payload = self._beacon_payload()
        logger.info(f"[DISCOVERY] Broadcasting presence on port {UDP_BROADCAST_PORT}")
        while self._running:
            try:
                sock.sendto(payload, ("<broadcast>", UDP_BROADCAST_PORT))
            except OSError as e:
                logger.debug(f"[DISCOVERY] Broadcast send error: {e}")
            time.sleep(UDP_BROADCAST_INTERVAL)
        sock.close()

    def _listener_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if platform.system() == "Darwin":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("", UDP_BROADCAST_PORT))
        except OSError as e:
            logger.error(f"[DISCOVERY] Cannot bind broadcast listener: {e}")
            return
        sock.settimeout(2.0)
        logger.info(f"[DISCOVERY] Listening for peers on port {UDP_BROADCAST_PORT}")
        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("service") != "shadowsync":
                    continue
                if msg.get("peer_id") == self.my_id:
                    continue   # Our own broadcast
                peer = Peer(
                    peer_id=msg["peer_id"],
                    hostname=msg.get("hostname", addr[0]),
                    ip=addr[0],
                    port=msg.get("port", 9000),
                    version=msg.get("version", "1.0"),
                    capabilities=msg.get("capabilities", []),
                )
                self.registry.add_or_update(peer)
            except socket.timeout:
                self.registry.prune_stale()
            except (json.JSONDecodeError, KeyError) as e:
                logger.debug(f"[DISCOVERY] Malformed beacon: {e}")
            except Exception as e:
                logger.error(f"[DISCOVERY] Listener error: {e}")
        sock.close()

    def start(self):
        self._running = True
        t1 = threading.Thread(target=self._sender_loop, daemon=True, name="discovery-sender")
        t2 = threading.Thread(target=self._listener_loop, daemon=True, name="discovery-listener")
        self._threads = [t1, t2]
        t1.start()
        t2.start()

    def stop(self):
        self._running = False
        for t in self._threads:
            t.join(timeout=3)
        logger.info("[DISCOVERY] Stopped.")
