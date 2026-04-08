"""
ShadowSync - Conflict + Transport Tests
"""

import hashlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conflict import (
    ConflictResolver, ConflictStrategy, IncomingFile,
    ConflictReason, CONFLICT_DIR
)
from core.transport import (
    LinkType, LinkQuality, TransportManager,
    _is_thunderbolt_ip, _is_tailscale_ip, _classify_link, ThrottledSocket
)


# ── Conflict resolver tests ───────────────────────────────────────────────────

class TestConflictDetection(unittest.TestCase):

    def setUp(self):
        self.watch_root = tempfile.mkdtemp()
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.watch_root, ignore_errors=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_local(self, rel_path: str, content: bytes, mtime: float = None) -> str:
        full = os.path.join(self.watch_root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)
        if mtime:
            os.utime(full, (mtime, mtime))
        return full

    def _make_incoming(self, rel_path: str, content: bytes, mtime: float) -> IncomingFile:
        tmp = os.path.join(self.tmp_dir, "incoming_" + os.path.basename(rel_path))
        with open(tmp, "wb") as f:
            f.write(content)
        h = hashlib.sha256(content).hexdigest()
        return IncomingFile(
            rel_path=rel_path,
            tmp_path=tmp,
            remote_ip="192.168.1.42",
            remote_mtime=mtime,
            remote_hash=h,
            remote_size=len(content),
        )

    def test_newer_wins_remote_is_newer(self):
        self._make_local("doc.txt", b"local version", mtime=1000.0)
        incoming = self._make_incoming("doc.txt", b"remote newer version", mtime=2000.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.NEWER_WINS)
        applied = resolver.resolve(incoming)
        self.assertTrue(applied)
        with open(os.path.join(self.watch_root, "doc.txt"), "rb") as f:
            self.assertEqual(f.read(), b"remote newer version")

    def test_newer_wins_local_is_newer(self):
        self._make_local("doc.txt", b"local newer version", mtime=3000.0)
        incoming = self._make_incoming("doc.txt", b"remote old version", mtime=1000.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.NEWER_WINS)
        applied = resolver.resolve(incoming)
        self.assertFalse(applied)
        with open(os.path.join(self.watch_root, "doc.txt"), "rb") as f:
            self.assertEqual(f.read(), b"local newer version")

    def test_local_wins_always(self):
        self._make_local("doc.txt", b"local", mtime=500.0)
        incoming = self._make_incoming("doc.txt", b"remote newer", mtime=9999.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.LOCAL_WINS)
        applied = resolver.resolve(incoming)
        self.assertFalse(applied)

    def test_remote_wins_always(self):
        self._make_local("doc.txt", b"local", mtime=9999.0)
        incoming = self._make_incoming("doc.txt", b"remote older", mtime=500.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.REMOTE_WINS)
        applied = resolver.resolve(incoming)
        self.assertTrue(applied)

    def test_keep_both_creates_conflict_copy(self):
        self._make_local("doc.txt", b"local version", mtime=1000.0)
        incoming = self._make_incoming("doc.txt", b"remote version", mtime=2000.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.KEEP_BOTH)
        applied = resolver.resolve(incoming)
        self.assertFalse(applied)
        # Conflict copy should exist alongside original
        files = list(Path(self.watch_root).glob("*.txt"))
        self.assertEqual(len(files), 2)

    def test_no_conflict_same_hash(self):
        content = b"identical content"
        self._make_local("doc.txt", content)
        incoming = self._make_incoming("doc.txt", content, mtime=time.time())
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.NEWER_WINS)
        applied = resolver.resolve(incoming)
        self.assertFalse(applied)  # No conflict, skipped

    def test_no_local_file_just_applies(self):
        incoming = self._make_incoming("newfile.txt", b"brand new", mtime=time.time())
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.NEWER_WINS)
        applied = resolver.resolve(incoming)
        self.assertTrue(applied)
        self.assertTrue(os.path.exists(os.path.join(self.watch_root, "newfile.txt")))

    def test_larger_wins_remote_larger(self):
        self._make_local("doc.txt", b"small", mtime=1000.0)
        incoming = self._make_incoming("doc.txt", b"x" * 1000, mtime=500.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.LARGER_WINS)
        applied = resolver.resolve(incoming)
        self.assertTrue(applied)

    def test_conflict_history_recorded(self):
        self._make_local("a.txt", b"v1", mtime=1000.0)
        incoming = self._make_incoming("a.txt", b"v2", mtime=2000.0)
        resolver = ConflictResolver(self.watch_root, ConflictStrategy.NEWER_WINS)
        resolver.resolve(incoming)
        hist = resolver.get_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["rel_path"], "a.txt")
        self.assertEqual(hist[0]["strategy_applied"], "NEWER_WINS")

    def test_conflict_callback_fires(self):
        self._make_local("cb.txt", b"local", mtime=1000.0)
        incoming = self._make_incoming("cb.txt", b"remote", mtime=2000.0)
        fired = []
        resolver = ConflictResolver(
            self.watch_root, ConflictStrategy.NEWER_WINS,
            on_conflict=lambda r: fired.append(r)
        )
        resolver.resolve(incoming)
        self.assertEqual(len(fired), 1)
        self.assertTrue(fired[0].resolved)


# ── Transport tests ───────────────────────────────────────────────────────────

class TestLinkClassification(unittest.TestCase):

    def test_thunderbolt_ip(self):
        self.assertTrue(_is_thunderbolt_ip("169.254.0.1"))
        self.assertTrue(_is_thunderbolt_ip("169.254.255.254"))
        self.assertFalse(_is_thunderbolt_ip("192.168.1.1"))
        self.assertFalse(_is_thunderbolt_ip("10.0.0.1"))

    def test_tailscale_ip(self):
        self.assertTrue(_is_tailscale_ip("100.64.0.1"))
        self.assertTrue(_is_tailscale_ip("100.100.100.100"))
        self.assertFalse(_is_tailscale_ip("192.168.1.1"))
        self.assertFalse(_is_tailscale_ip("169.254.0.1"))

    def test_classify_thunderbolt(self):
        self.assertEqual(_classify_link("169.254.10.1"), LinkType.THUNDERBOLT)

    def test_classify_tailscale(self):
        self.assertEqual(_classify_link("100.80.5.2"), LinkType.TAILSCALE)

    def test_classify_lan(self):
        self.assertEqual(_classify_link("192.168.1.5"), LinkType.LAN)


class TestLinkQualityScore(unittest.TestCase):

    def test_thunderbolt_beats_lan(self):
        tb = LinkQuality(LinkType.THUNDERBOLT, "169.254.0.1", 9000, rtt_ms=5.0)
        lan = LinkQuality(LinkType.LAN, "192.168.1.1", 9000, rtt_ms=1.0)
        self.assertLess(tb.score, lan.score)

    def test_lan_beats_tailscale(self):
        lan = LinkQuality(LinkType.LAN, "192.168.1.1", 9000, rtt_ms=2.0)
        ts = LinkQuality(LinkType.TAILSCALE, "100.80.0.1", 9000, rtt_ms=2.0)
        self.assertLess(lan.score, ts.score)

    def test_failures_increase_score(self):
        lq = LinkQuality(LinkType.LAN, "192.168.1.1", 9000, rtt_ms=2.0)
        score_before = lq.score
        lq.failures = 3
        self.assertGreater(lq.score, score_before)


class TestTransportManager(unittest.TestCase):

    def test_register_and_remove(self):
        tm = TransportManager()
        # Can't actually probe in CI, but register should not crash
        # Patch _probe_rtt to return None (unreachable)
        with patch("core.transport._probe_rtt", return_value=None):
            tm.register_peer("peer1", ["192.168.1.1"], 9000)
        best = tm.best_link("peer1")
        self.assertIsNone(best)   # No reachable links
        tm.remove_peer("peer1")

    def test_failure_tracking(self):
        tm = TransportManager()
        lq = LinkQuality(LinkType.LAN, "192.168.1.1", 9000, rtt_ms=5.0)
        tm._peer_links["p1"] = [lq]
        tm.report_failure("p1", "192.168.1.1")
        tm.report_failure("p1", "192.168.1.1")
        self.assertEqual(lq.failures, 2)
        tm.report_success("p1", "192.168.1.1")
        self.assertEqual(lq.failures, 0)

    def test_best_link_filters_failed(self):
        tm = TransportManager()
        lq = LinkQuality(LinkType.LAN, "192.168.1.1", 9000, rtt_ms=5.0)
        lq.failures = 5   # Too many failures
        tm._peer_links["p1"] = [lq]
        self.assertIsNone(tm.best_link("p1"))


class TestThrottledSocket(unittest.TestCase):

    def test_passthrough_unlimited(self):
        mock_sock = MagicMock()
        ts = ThrottledSocket(mock_sock, limit_bps=0)
        ts.sendall(b"hello")
        mock_sock.sendall.assert_called_once_with(b"hello")

    def test_throttle_does_not_corrupt(self):
        """Throttled sends should call sendall in chunks that sum to full data."""
        sent_data = []
        mock_sock = MagicMock()
        mock_sock.sendall.side_effect = lambda d: sent_data.append(bytes(d))

        ts = ThrottledSocket(mock_sock, limit_bps=1024 * 1024)  # 1 MB/s
        data = b"x" * 4096
        ts.sendall(data)
        total = b"".join(sent_data)
        self.assertEqual(total, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
