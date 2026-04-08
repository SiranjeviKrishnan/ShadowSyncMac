"""
ShadowSync - Configuration
Persistent config stored in ~/Library/Application Support/ShadowSync/config.json
"""

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List

logger = logging.getLogger("shadowsync.config")

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "ShadowSync"
CONFIG_FILE = APP_SUPPORT / "config.json"
LOG_DIR = APP_SUPPORT / "logs"


@dataclass
class SyncConfig:
    version: int = 2
    watch_dirs: List[str] = field(default_factory=lambda: [str(Path.home() / "Documents")])
    sync_port: int = 9000
    discovery_port: int = 9001
    conflict_strategy: str = "newer_wins"   # newer_wins | larger_wins | local_wins | keep_both
    auto_sync: bool = True
    sync_deletions: bool = True
    excluded_patterns: List[str] = field(default_factory=lambda: [
        ".DS_Store", ".git", "node_modules", "__pycache__", "*.tmp", "~*"
    ])
    max_file_size_mb: int = 500
    bandwidth_limit_kbps: int = 0   # 0 = unlimited
    log_level: str = "INFO"
    show_notifications: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SyncConfig":
        valid_keys = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)

    def save(self):
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.debug(f"[CONFIG] Saved to {CONFIG_FILE}")

    @classmethod
    def load(cls) -> "SyncConfig":
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                config = cls.from_dict(data)
                logger.debug(f"[CONFIG] Loaded from {CONFIG_FILE}")
                return config
            except Exception as e:
                logger.warning(f"[CONFIG] Failed to load config, using defaults: {e}")
        config = cls()
        config.save()
        return config

    def validate(self) -> list[str]:
        errors = []
        for d in self.watch_dirs:
            if not os.path.isdir(d):
                errors.append(f"Watch directory not found: {d}")
        if not (1024 <= self.sync_port <= 65535):
            errors.append(f"Invalid sync port: {self.sync_port}")
        if self.conflict_strategy not in ("newer_wins", "larger_wins", "local_wins", "keep_both"):
            errors.append(f"Unknown conflict strategy: {self.conflict_strategy}")
        return errors
