"""
ShadowSync - Transport Layer
Auto-negotiates the fastest available link:
  1. Thunderbolt Bridge (169.254.x.x link-local, ~40Gbit)
  2. LAN (standard TCP, ~1Gbit)
  3. Tailscale VPN (WireGuard, remote)

Link quality is probed via RTT ping and bandwidth is measured on first connect.
"""

import ipaddress
import logging
import socket
import struct
import time
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger("shadowsync.transport")

# Thunderbolt bridge uses link-local range 169.254.0.0/16
THUNDERBOLT_SUBNET = ipaddress.IPv4Network("169.254.0.0/16")
PROBE_TIMEOUT = 1.5   # seconds


class LinkType(Enum):
    THUNDERBOLT = auto()   # ~40 Gbit/s
    LAN = auto()           # ~1 Gbit/s
    WIFI = auto()          # ~300 Mbit/s
    TAILSCALE = auto()     # ~100 Mbit/s WireGuard
    UNKNOWN = auto()


@dataclass
class LinkQuality:
    link_type: LinkType = LinkType.UNKNOWN
    ip: str = ""
    port: int = 9000
    rtt_ms: float = 999.0
    estimated_bw_mbps: float = 0.0
    last_probed: float = field(default_factory=time.time)
    failures: int = 0

    @property
    def score(self) -> float:
        """Lower is better — used for link selection."""
        type_penalty = {
            LinkType.THUNDERBOLT: 0,
            LinkType.LAN: 100,
            LinkType.WIFI: 300,
            LinkType.TAILSCALE: 500,
            LinkType.UNKNOWN: 9999,
        }
        return type_penalty[self.link_type] + self.rtt_ms + (self.failures * 50)

    def __repr__(self):
        return (f"<LinkQuality {self.link_type.name} "
                f"ip={self.ip} rtt={self.rtt_ms:.1f}ms score={self.score:.0f}>")


def _is_thunderbolt_ip(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip) in THUNDERBOLT_SUBNET
    except ValueError:
        return False


def _is_tailscale_ip(ip: str) -> bool:
    """Tailscale uses 100.x.x.x (CGNAT range)."""
    try:
        addr = ipaddress.IPv4Address(ip)
        return addr in ipaddress.IPv4Network("100.64.0.0/10")
    except ValueError:
        return False


def _probe_rtt(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> Optional[float]:
    """Connect and measure round-trip time in ms. Returns None on failure."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.connect((ip, port))
        rtt = (time.perf_counter() - t0) * 1000
        # Send a tiny probe payload
        s.sendall(struct.pack("!4sHHI", b"SSNC", 2, 9, 0))  # PING msg
        s.close()
        return rtt
    except (socket.timeout, ConnectionRefusedError, OSError):
        return None


def _classify_link(ip: str) -> LinkType:
    if _is_thunderbolt_ip(ip):
        return LinkType.THUNDERBOLT
    if _is_tailscale_ip(ip):
        return LinkType.TAILSCALE
    # Distinguish LAN vs WiFi: WiFi typically has higher variance but we
    # can't determine this without OS APIs — default to LAN for wired guess
    return LinkType.LAN


def probe_peer_links(peer_ips: list[str], port: int) -> list[LinkQuality]:
    """
    Probe all known IPs for a peer and return sorted list of link qualities.
    Called once on peer discovery, refreshed every 60s.
    """
    results = []
    threads = []
    lock = threading.Lock()

    def probe(ip):
        rtt = _probe_rtt(ip, port)
        if rtt is not None:
            lq = LinkQuality(
                link_type=_classify_link(ip),
                ip=ip,
                port=port,
                rtt_ms=rtt,
            )
            with lock:
                results.append(lq)
                logger.info(f"[TRANSPORT] Link {ip}: {lq.link_type.name} rtt={rtt:.1f}ms")

    for ip in peer_ips:
        t = threading.Thread(target=probe, args=(ip,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=PROBE_TIMEOUT + 0.5)

    results.sort(key=lambda x: x.score)
    return results


class TransportManager:
    """
    Manages per-peer link selection.
    Automatically picks Thunderbolt > LAN > Tailscale.
    Falls back on consecutive failures.
    """

    def __init__(self):
        self._peer_links: dict[str, list[LinkQuality]] = {}   # peer_id -> [LinkQuality]
        self._lock = threading.RLock()

    def register_peer(self, peer_id: str, ips: list[str], port: int):
        """Called when a peer is discovered. Probes all its known IPs."""
        links = probe_peer_links(ips, port)
        with self._lock:
            self._peer_links[peer_id] = links
        if links:
            best = links[0]
            logger.info(f"[TRANSPORT] Best link for {peer_id}: {best}")
        else:
            logger.warning(f"[TRANSPORT] No reachable links for {peer_id}")

    def best_link(self, peer_id: str) -> Optional[LinkQuality]:
        with self._lock:
            links = self._peer_links.get(peer_id, [])
        # Filter out links with too many recent failures
        viable = [l for l in links if l.failures < 3]
        return viable[0] if viable else None

    def report_failure(self, peer_id: str, ip: str):
        with self._lock:
            for lq in self._peer_links.get(peer_id, []):
                if lq.ip == ip:
                    lq.failures += 1
                    logger.warning(f"[TRANSPORT] Failure #{lq.failures} on {ip}")
                    break

    def report_success(self, peer_id: str, ip: str):
        with self._lock:
            for lq in self._peer_links.get(peer_id, []):
                if lq.ip == ip:
                    lq.failures = 0
                    break

    def remove_peer(self, peer_id: str):
        with self._lock:
            self._peer_links.pop(peer_id, None)

    def get_link_summary(self) -> list[dict]:
        with self._lock:
            summary = []
            for peer_id, links in self._peer_links.items():
                for lq in links:
                    summary.append({
                        "peer_id": peer_id,
                        "ip": lq.ip,
                        "type": lq.link_type.name,
                        "rtt_ms": round(lq.rtt_ms, 1),
                        "score": round(lq.score, 1),
                        "failures": lq.failures,
                    })
            return summary


# ── Bandwidth throttle ────────────────────────────────────────────────────────

class ThrottledSocket:
    """
    Wraps a socket and enforces a bandwidth limit (bytes/sec).
    limit_bps=0 means unlimited.
    """

    def __init__(self, sock: socket.socket, limit_bps: int = 0):
        self._sock = sock
        self._limit_bps = limit_bps
        self._sent = 0
        self._window_start = time.monotonic()

    def sendall(self, data: bytes):
        if self._limit_bps == 0:
            self._sock.sendall(data)
            return

        offset = 0
        chunk = max(4096, self._limit_bps // 10)
        while offset < len(data):
            now = time.monotonic()
            elapsed = now - self._window_start
            if elapsed >= 1.0:
                self._sent = 0
                self._window_start = now

            if self._sent >= self._limit_bps:
                sleep_time = 1.0 - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                continue

            allowed = min(chunk, len(data) - offset, self._limit_bps - self._sent)
            self._sock.sendall(data[offset:offset + allowed])
            self._sent += allowed
            offset += allowed

    def recv(self, n: int) -> bytes:
        return self._sock.recv(n)

    def close(self):
        self._sock.close()

    def settimeout(self, t):
        self._sock.settimeout(t)

    def __getattr__(self, name):
        return getattr(self._sock, name)
