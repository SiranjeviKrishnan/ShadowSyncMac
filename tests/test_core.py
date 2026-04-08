"""
ShadowSync - Full Test Suite
Covers: watcher, discovery, protocol, config, conflict, transport, delta sync, IPC, engine integration
"""

import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.watcher import ChangeEvent, _is_ignored, _file_hash
from core.discovery import Peer, PeerRegistry
from core.sync_engine import (
    encode_msg, decode_header, MsgType, HEADER_SIZE, _sha256,
    SyncEngine, TransferProgress, compute_block_list, compute_delta,
    DELTA_BLOCK_SIZE
)
from core.conflict import ConflictResolver, ConflictStrategy, IncomingFile
from core.transport import (
    TransportManager, _is_thunderbolt_ip, _is_tailscale_ip,
    _classify_link, LinkType, ThrottledSocket, LinkQuality
)
from core.ipc import IPCServer
from config.settings import SyncConfig


# ── Watcher ───────────────────────────────────────────────────────────────────

class TestIgnorePatterns(unittest.TestCase):
    def test_ds_store(self):   self.assertTrue(_is_ignored("/a/.DS_Store"))
    def test_swp(self):        self.assertTrue(_is_ignored("/a/file.swp"))
    def test_tmp(self):        self.assertTrue(_is_ignored("/a/file.tmp"))
    def test_git(self):        self.assertTrue(_is_ignored("/a/.git"))
    def test_normal_md(self):  self.assertFalse(_is_ignored("/a/notes.md"))
    def test_normal_pdf(self): self.assertFalse(_is_ignored("/a/report.pdf"))


class TestFileHash(unittest.TestCase):
    def test_consistent(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello shadowsync"); path = f.name
        try:
            self.assertEqual(_file_hash(path), _file_hash(path))
        finally:
            os.unlink(path)

    def test_missing(self):
        self.assertIsNone(_file_hash("/nonexistent/x.txt"))

    def test_different_content(self):
        with tempfile.NamedTemporaryFile(delete=False) as f1, \
             tempfile.NamedTemporaryFile(delete=False) as f2:
            f1.write(b"aaa"); f2.write(b"bbb")
            p1, p2 = f1.name, f2.name
        try:
            self.assertNotEqual(_file_hash(p1), _file_hash(p2))
        finally:
            os.unlink(p1); os.unlink(p2)


class TestChangeEvent(unittest.TestCase):
    def test_created(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test"); path = f.name
        try:
            ev = ChangeEvent("created", path)
            self.assertIsNotNone(ev.file_hash)
            self.assertGreater(ev.size, 0)
            self.assertIn("file_hash", ev.to_dict())
        finally:
            os.unlink(path)

    def test_deleted_no_hash(self):
        ev = ChangeEvent("deleted", "/gone.txt")
        self.assertIsNone(ev.file_hash)
        self.assertEqual(ev.size, 0)


# ── Discovery ─────────────────────────────────────────────────────────────────

class TestPeerRegistry(unittest.TestCase):
    def setUp(self): self.reg = PeerRegistry()

    def test_add_fires_discovered(self):
        events = []
        self.reg.register_callback(lambda e, p: events.append(e))
        self.reg.add_or_update(Peer("id1", "Mac", "1.1.1.1", 9000))
        self.assertEqual(events[0], "discovered")

    def test_update_fires_updated(self):
        events = []
        self.reg.register_callback(lambda e, p: events.append(e))
        p = Peer("id1", "Mac", "1.1.1.1", 9000)
        self.reg.add_or_update(p); self.reg.add_or_update(p)
        self.assertIn("updated", events)

    def test_remove(self):
        self.reg.add_or_update(Peer("id1", "Mac", "1.1.1.1", 9000))
        self.reg.remove("id1")
        self.assertEqual(self.reg.get_all(), [])

    def test_stale_pruned(self):
        p = Peer("id1", "Mac", "1.1.1.1", 9000)
        p.last_seen = time.time() - 9999
        self.reg._peers["id1"] = p
        self.reg.prune_stale()
        self.assertEqual(self.reg.get_all(), [])

    def test_peer_alive(self):
        p = Peer("id1", "Mac", "1.1.1.1", 9000)
        self.assertTrue(p.is_alive())
        p.last_seen = time.time() - 9999
        self.assertFalse(p.is_alive())

    def test_get(self):
        self.reg.add_or_update(Peer("id1", "Mac", "1.1.1.1", 9000))
        self.assertIsNotNone(self.reg.get("id1"))
        self.assertIsNone(self.reg.get("nope"))


# ── Protocol ──────────────────────────────────────────────────────────────────

class TestProtocol(unittest.TestCase):
    def test_roundtrip(self):
        payload = {"rel_path": "a/b.txt", "size": 1024, "sha256": "abc"}
        msg = encode_msg(MsgType.FILE_META, payload)
        mt, pl = decode_header(msg[:HEADER_SIZE])
        self.assertEqual(mt, MsgType.FILE_META)
        body = json.loads(msg[HEADER_SIZE:])
        self.assertEqual(body["rel_path"], "a/b.txt")

    def test_all_types_encode(self):
        for mt in MsgType:
            msg = encode_msg(mt, {})
            decoded, _ = decode_header(msg[:HEADER_SIZE])
            self.assertEqual(decoded, mt)

    def test_bad_magic(self):
        from core.sync_engine import ProtocolError
        with self.assertRaises(Exception):
            decode_header(b"\x00" * HEADER_SIZE)


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        c = SyncConfig()
        self.assertEqual(c.sync_port, 9000)
        self.assertTrue(c.auto_sync)

    def test_roundtrip(self):
        c = SyncConfig(); c.sync_port = 8888
        c2 = SyncConfig.from_dict(c.to_dict())
        self.assertEqual(c2.sync_port, 8888)

    def test_invalid_port(self):
        c = SyncConfig(); c.sync_port = 80; c.watch_dirs = ["/tmp"]
        self.assertTrue(any("port" in e for e in c.validate()))

    def test_invalid_strategy(self):
        c = SyncConfig(); c.conflict_strategy = "banana"; c.watch_dirs = ["/tmp"]
        self.assertTrue(any("conflict" in e for e in c.validate()))


# ── Conflict ──────────────────────────────────────────────────────────────────

class TestConflictResolver(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _resolver(self, strategy=ConflictStrategy.NEWER_WINS):
        return ConflictResolver(watch_root=self.tmpdir, strategy=strategy)

    def _local(self, name, content, mtime=None):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f: f.write(content)
        if mtime: os.utime(path, (mtime, mtime))
        return path

    def _incoming(self, name, content, mtime=None):
        fd, tmp = tempfile.mkstemp(dir=self.tmpdir)
        with os.fdopen(fd, "wb") as f: f.write(content)
        return IncomingFile(
            rel_path=name, tmp_path=tmp, remote_ip="1.2.3.4",
            remote_mtime=mtime or time.time(),
            remote_hash=hashlib.sha256(content).hexdigest(),
            remote_size=len(content),
        )

    def test_no_local_applies(self):
        r = self._resolver()
        self.assertTrue(r.resolve(self._incoming("new.txt", b"remote")))
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "new.txt")))

    def test_same_hash_skips(self):
        content = b"identical"
        self._local("same.txt", content)
        r = self._resolver()
        self.assertFalse(r.resolve(self._incoming("same.txt", content)))

    def test_newer_wins_remote_newer(self):
        now = time.time()
        self._local("f.txt", b"old", mtime=now - 100)
        r = self._resolver(ConflictStrategy.NEWER_WINS)
        self.assertTrue(r.resolve(self._incoming("f.txt", b"new", mtime=now)))

    def test_newer_wins_local_newer(self):
        now = time.time()
        self._local("f.txt", b"local", mtime=now)
        r = self._resolver(ConflictStrategy.NEWER_WINS)
        self.assertFalse(r.resolve(self._incoming("f.txt", b"old", mtime=now - 200)))

    def test_local_wins(self):
        self._local("f.txt", b"local")
        r = self._resolver(ConflictStrategy.LOCAL_WINS)
        self.assertFalse(r.resolve(self._incoming("f.txt", b"remote")))

    def test_remote_wins(self):
        self._local("f.txt", b"local")
        r = self._resolver(ConflictStrategy.REMOTE_WINS)
        self.assertTrue(r.resolve(self._incoming("f.txt", b"remote")))

    def test_keep_both_creates_conflict_copy(self):
        self._local("f.txt", b"local")
        r = self._resolver(ConflictStrategy.KEEP_BOTH)
        self.assertFalse(r.resolve(self._incoming("f.txt", b"remote")))
        conflict_files = [x for x in os.listdir(self.tmpdir) if "conflict" in x]
        self.assertEqual(len(conflict_files), 1)

    def test_callback_fires(self):
        conflicts = []
        self._local("f.txt", b"local")
        r = self._resolver()
        r.on_conflict = lambda rec: conflicts.append(rec)
        r.resolve(self._incoming("f.txt", b"remote", mtime=time.time() + 999))
        self.assertEqual(len(conflicts), 1)

    def test_history(self):
        self._local("f.txt", b"local")
        r = self._resolver()
        r.resolve(self._incoming("f.txt", b"remote", mtime=time.time() + 999))
        self.assertEqual(len(r.get_history()), 1)


# ── Transport ─────────────────────────────────────────────────────────────────

class TestTransport(unittest.TestCase):
    def test_thunderbolt(self):
        self.assertTrue(_is_thunderbolt_ip("169.254.1.1"))
        self.assertFalse(_is_thunderbolt_ip("192.168.1.1"))

    def test_tailscale(self):
        self.assertTrue(_is_tailscale_ip("100.100.1.1"))
        self.assertFalse(_is_tailscale_ip("192.168.1.1"))

    def test_classify(self):
        self.assertEqual(_classify_link("169.254.0.1"), LinkType.THUNDERBOLT)
        self.assertEqual(_classify_link("100.80.10.5"), LinkType.TAILSCALE)
        self.assertEqual(_classify_link("192.168.1.5"), LinkType.LAN)

    def test_score_order(self):
        tb = LinkQuality(link_type=LinkType.THUNDERBOLT, ip="169.254.1.1", port=9000, rtt_ms=1)
        lan = LinkQuality(link_type=LinkType.LAN, ip="192.168.1.1", port=9000, rtt_ms=1)
        self.assertLess(tb.score, lan.score)

    def test_failure_tracking(self):
        tm = TransportManager()
        lq = LinkQuality(link_type=LinkType.LAN, ip="1.1.1.1", port=9000, rtt_ms=5)
        tm._peer_links["p1"] = [lq]
        for _ in range(3):
            tm.report_failure("p1", "1.1.1.1")
        self.assertIsNone(tm.best_link("p1"))

    def test_success_resets_failures(self):
        tm = TransportManager()
        lq = LinkQuality(link_type=LinkType.LAN, ip="1.1.1.1", port=9000, rtt_ms=5)
        lq.failures = 3
        tm._peer_links["p1"] = [lq]
        tm.report_success("p1", "1.1.1.1")
        self.assertEqual(lq.failures, 0)

    def test_throttled_socket_unlimited(self):
        mock = MagicMock()
        ts = ThrottledSocket(mock, 0)
        ts.sendall(b"data")
        mock.sendall.assert_called_once_with(b"data")


# ── Delta sync ────────────────────────────────────────────────────────────────

class TestDeltaSync(unittest.TestCase):
    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            self.assertEqual(compute_block_list(path), [])
        finally:
            os.unlink(path)

    def test_single_block(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100); path = f.name
        try:
            blocks = compute_block_list(path)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["idx"], 0)
        finally:
            os.unlink(path)

    def test_multiple_blocks(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"a" * DELTA_BLOCK_SIZE * 3); path = f.name
        try:
            self.assertEqual(len(compute_block_list(path)), 3)
        finally:
            os.unlink(path)

    def test_no_changes(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"same content " * 100); path = f.name
        try:
            blocks = compute_block_list(path)
            self.assertEqual(compute_delta(path, blocks), [])
        finally:
            os.unlink(path)

    def test_detects_change(self):
        with tempfile.NamedTemporaryFile(delete=False) as f1, \
             tempfile.NamedTemporaryFile(delete=False) as f2:
            f1.write(b"original " * 1000); f2.write(b"MODIFIED " * 1000)
            p1, p2 = f1.name, f2.name
        try:
            self.assertGreater(len(compute_delta(p2, compute_block_list(p1))), 0)
        finally:
            os.unlink(p1); os.unlink(p2)

    def test_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"shadowsync"); path = f.name
        try:
            self.assertEqual(_sha256(path), hashlib.sha256(b"shadowsync").hexdigest())
        finally:
            os.unlink(path)


# ── TransferProgress ──────────────────────────────────────────────────────────

class TestTransferProgress(unittest.TestCase):
    def test_percent(self):
        p = TransferProgress("t1", "f.txt", "mac", "send", total_bytes=1000)
        p.transferred_bytes = 500
        self.assertEqual(p.percent, 50.0)

    def test_zero_total(self):
        p = TransferProgress("t1", "f.txt", "mac", "send", total_bytes=0)
        self.assertEqual(p.percent, 100.0)

    def test_to_dict_keys(self):
        p = TransferProgress("t1", "f.txt", "mac", "send", total_bytes=100)
        d = p.to_dict()
        for key in ("percent", "speed_kbps", "direction", "done"):
            self.assertIn(key, d)


# ── IPC Server ────────────────────────────────────────────────────────────────

class TestIPCServer(unittest.TestCase):
    def setUp(self):
        import core.ipc as ipc_module
        self.sock_path = f"/tmp/test_ss_{os.getpid()}.sock"
        ipc_module.IPC_SOCKET_PATH = self.sock_path
        self.ipc = IPCServer()
        self.ipc.start()
        time.sleep(0.1)

    def tearDown(self):
        self.ipc.stop()
        try: os.unlink(self.sock_path)
        except OSError: pass

    def _cmd(self, cmd: dict) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock_path)
        s.sendall(json.dumps(cmd).encode() + b"\n")
        s.settimeout(2.0)
        data = b""
        while b"\n" not in data:
            data += s.recv(4096)
        s.close()
        return json.loads(data.split(b"\n")[0])

    def test_ping(self):
        r = self._cmd({"cmd": "ping"})
        self.assertEqual(r["cmd"], "pong")

    def test_status(self):
        r = self._cmd({"cmd": "status"})
        self.assertIn("paused", r)

    def test_peers_empty(self):
        r = self._cmd({"cmd": "peers"})
        self.assertEqual(r["peers"], [])

    def test_transfers(self):
        r = self._cmd({"cmd": "transfers"})
        self.assertIn("transfers", r)

    def test_unknown_cmd(self):
        r = self._cmd({"cmd": "banana"})
        self.assertIn("error", r)

    def test_broadcast_to_subscriber(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock_path)
        s.sendall(json.dumps({"cmd": "subscribe"}).encode() + b"\n")
        time.sleep(0.05)
        self.ipc.broadcast_event("test_evt", "hello")
        s.settimeout(1.0)
        data = b""
        for _ in range(3):
            try: data += s.recv(4096)
            except socket.timeout: break
        s.close()
        self.assertIn(b"test_evt", data)


# ── Engine integration ────────────────────────────────────────────────────────

class TestEngineIntegration(unittest.TestCase):
    def setUp(self): self.tmpdir = tempfile.mkdtemp()
    def tearDown(self): shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_watch_root_resolved(self):
        e = SyncEngine(watch_root=self.tmpdir, port=19999)
        self.assertEqual(e.watch_root, str(Path(self.tmpdir).resolve()))

    def test_initial_stats(self):
        e = SyncEngine(watch_root=self.tmpdir, port=19998)
        s = e.get_stats()
        for k in ("sent", "received", "errors", "conflicts"):
            self.assertEqual(s[k], 0)

    def test_change_no_peers_silent(self):
        e = SyncEngine(watch_root=self.tmpdir, port=19997)
        with tempfile.NamedTemporaryFile(dir=self.tmpdir, delete=False) as f:
            f.write(b"x"); path = f.name
        e.on_file_change(ChangeEvent("created", path))  # Should not raise

    def test_peer_register_unregister(self):
        e = SyncEngine(watch_root=self.tmpdir, port=19996)
        p = Peer("p1", "Mac", "127.0.0.1", 19996)
        e.on_peer_discovered("discovered", p)
        with e._peers_lock:
            self.assertIn("p1", e._peers)
        e.on_peer_discovered("lost", p)
        with e._peers_lock:
            self.assertNotIn("p1", e._peers)

    def test_status_callback_fires(self):
        events = []
        e = SyncEngine(watch_root=self.tmpdir, port=19994,
                       on_status=lambda t, d: events.append(t))
        p = Peer("p1", "Mac", "127.0.0.1", 19994)
        e.on_peer_discovered("discovered", p)
        self.assertIn("peer_connected", events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
