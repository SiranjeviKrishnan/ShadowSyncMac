"""
ShadowSync - Conflict Resolution
Detects, logs, and resolves sync conflicts with multiple strategies.
Conflict copies are preserved in ~/.shadowsync_conflicts/
"""

import logging
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("shadowsync.conflict")

CONFLICT_DIR = Path.home() / ".shadowsync_conflicts"


class ConflictStrategy(Enum):
    NEWER_WINS = auto()
    LARGER_WINS = auto()
    LOCAL_WINS = auto()
    REMOTE_WINS = auto()
    KEEP_BOTH = auto()


class ConflictReason(Enum):
    BOTH_MODIFIED = "both_modified"
    REMOTE_MODIFIED_LOCAL_DELETED = "remote_modified_local_deleted"
    LOCAL_MODIFIED_REMOTE_DELETED = "local_modified_remote_deleted"
    HASH_MISMATCH_SAME_MTIME = "hash_mismatch_same_mtime"


@dataclass
class ConflictRecord:
    conflict_id: str
    rel_path: str
    local_path: str
    remote_ip: str
    local_mtime: float
    remote_mtime: float
    local_hash: str
    remote_hash: str
    local_size: int
    remote_size: int
    reason: str
    strategy_applied: str
    resolution: str        # "local_kept" | "remote_applied" | "both_kept" | "pending"
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IncomingFile:
    rel_path: str
    tmp_path: str          # temp path where remote file was written
    remote_ip: str
    remote_mtime: float
    remote_hash: str
    remote_size: int


class ConflictResolver:
    """
    Detects conflicts between local and incoming remote files.
    Applies the configured strategy and keeps an audit log.
    """

    def __init__(self,
                 watch_root: str,
                 strategy: ConflictStrategy = ConflictStrategy.NEWER_WINS,
                 on_conflict: Optional[Callable[[ConflictRecord], None]] = None):
        self.watch_root = Path(watch_root)
        self.strategy = strategy
        self.on_conflict = on_conflict
        self._history: list[ConflictRecord] = []
        CONFLICT_DIR.mkdir(parents=True, exist_ok=True)

    def resolve(self, incoming: IncomingFile) -> bool:
        """
        Decide what to do with an incoming file that conflicts with local.
        Returns True if remote file was applied, False if local was kept.
        Handles all bookkeeping.
        """
        local_path = self.watch_root / incoming.rel_path

        # No local file → no conflict, just apply
        if not local_path.exists():
            self._apply_remote(incoming, local_path)
            return True

        local_stat = local_path.stat()
        local_hash = self._hash(str(local_path))

        # Identical content → skip, no real conflict
        if local_hash == incoming.remote_hash:
            logger.debug(f"[CONFLICT] Same hash, skipping: {incoming.rel_path}")
            os.unlink(incoming.tmp_path)
            return False

        # Detect reason
        reason = self._detect_reason(local_stat, incoming)

        record = ConflictRecord(
            conflict_id=f"{int(time.time()*1000)}_{os.path.basename(incoming.rel_path)}",
            rel_path=incoming.rel_path,
            local_path=str(local_path),
            remote_ip=incoming.remote_ip,
            local_mtime=local_stat.st_mtime,
            remote_mtime=incoming.remote_mtime,
            local_hash=local_hash,
            remote_hash=incoming.remote_hash,
            local_size=local_stat.st_size,
            remote_size=incoming.remote_size,
            reason=reason.value,
            strategy_applied=self.strategy.name,
            resolution="pending",
        )

        logger.warning(
            f"[CONFLICT] {incoming.rel_path} | "
            f"reason={reason.value} | strategy={self.strategy.name} | "
            f"local_mtime={local_stat.st_mtime:.0f} remote_mtime={incoming.remote_mtime:.0f}"
        )

        applied = self._apply_strategy(record, incoming, local_path)
        record.resolved = True
        self._history.append(record)

        if self.on_conflict:
            try:
                self.on_conflict(record)
            except Exception as e:
                logger.error(f"[CONFLICT] Callback error: {e}")

        return applied

    # ── Strategy dispatch ─────────────────────────────────────────────────────

    def _apply_strategy(self, record: ConflictRecord,
                        incoming: IncomingFile, local_path: Path) -> bool:
        s = self.strategy

        if s == ConflictStrategy.NEWER_WINS:
            if incoming.remote_mtime > record.local_mtime:
                self._save_conflict_copy(local_path, record.conflict_id, "local")
                self._apply_remote(incoming, local_path)
                record.resolution = "remote_applied"
                return True
            else:
                os.unlink(incoming.tmp_path)
                record.resolution = "local_kept"
                return False

        elif s == ConflictStrategy.LARGER_WINS:
            if incoming.remote_size > record.local_size:
                self._save_conflict_copy(local_path, record.conflict_id, "local")
                self._apply_remote(incoming, local_path)
                record.resolution = "remote_applied"
                return True
            else:
                os.unlink(incoming.tmp_path)
                record.resolution = "local_kept"
                return False

        elif s == ConflictStrategy.LOCAL_WINS:
            os.unlink(incoming.tmp_path)
            record.resolution = "local_kept"
            return False

        elif s == ConflictStrategy.REMOTE_WINS:
            self._save_conflict_copy(local_path, record.conflict_id, "local")
            self._apply_remote(incoming, local_path)
            record.resolution = "remote_applied"
            return True

        elif s == ConflictStrategy.KEEP_BOTH:
            # Save remote with a conflict suffix
            stem = Path(incoming.rel_path).stem
            suffix = Path(incoming.rel_path).suffix
            conflict_name = f"{stem} (conflict from {incoming.remote_ip}){suffix}"
            conflict_dest = local_path.parent / conflict_name
            shutil.move(incoming.tmp_path, str(conflict_dest))
            record.resolution = "both_kept"
            logger.info(f"[CONFLICT] Kept both: {conflict_dest}")
            return False

        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _apply_remote(self, incoming: IncomingFile, local_path: Path):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(incoming.tmp_path, str(local_path))
        if incoming.remote_mtime:
            os.utime(str(local_path), (incoming.remote_mtime, incoming.remote_mtime))
        logger.info(f"[CONFLICT] Applied remote: {incoming.rel_path}")

    def _save_conflict_copy(self, path: Path, conflict_id: str, label: str):
        """Archive the losing version before overwriting."""
        dest = CONFLICT_DIR / conflict_id / f"{label}_{path.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(dest))
        logger.info(f"[CONFLICT] Archived {label} copy → {dest}")

    @staticmethod
    def _detect_reason(local_stat, incoming: IncomingFile) -> ConflictReason:
        if abs(local_stat.st_mtime - incoming.remote_mtime) < 2:
            return ConflictReason.HASH_MISMATCH_SAME_MTIME
        return ConflictReason.BOTH_MODIFIED

    @staticmethod
    def _hash(path: str) -> str:
        import hashlib
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except OSError:
            pass
        return h.hexdigest()

    def get_history(self, limit: int = 50) -> list[dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def get_unresolved(self) -> list[ConflictRecord]:
        return [r for r in self._history if r.resolution == "pending"]

    def clear_history(self):
        self._history.clear()
