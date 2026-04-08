"""
ShadowSync v2.0 — Main Entry Point
Wires: FileWatcher → SyncEngine (conflict + transport) → IPC server → Dashboard/MenuBar
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import SyncConfig, LOG_DIR
from core.watcher import FileWatcher
from core.discovery import UDPDiscovery, PeerRegistry
from core.sync_engine import SyncEngine
from core.ipc import get_ipc_server


def setup_logging(config: SyncConfig) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "shadowsync.log"
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    return logging.getLogger("shadowsync.app")


def main():
    print("\033[1m\033[94m")
    print("  ╔════════════════════════════════╗")
    print("  ║   ShadowSync  v2.0             ║")
    print("  ║   Real-time Mac-to-Mac Sync    ║")
    print("  ╚════════════════════════════════╝")
    print("\033[0m")

    config = SyncConfig.load()
    logger = setup_logging(config)
    logger.info("ShadowSync starting up…")

    errors = config.validate()
    if errors:
        for e in errors:
            logger.error(f"Config: {e}")
        sys.exit(1)

    ipc = get_ipc_server()

    def on_status(event_type: str, detail: str):
        print(f"\033[96m  ▸ {event_type}: {detail}\033[0m")
        ipc.broadcast_event(event_type, detail)

    def on_progress(progress):
        ipc.broadcast_event("progress", progress.rel_path, data=progress.to_dict())

    registry = PeerRegistry()

    engine = SyncEngine(
        watch_root=config.watch_dirs[0],
        port=config.sync_port,
        bandwidth_limit_kbps=config.bandwidth_limit_kbps,
        on_status=on_status,
        on_progress=on_progress,
    )

    registry.register_callback(engine.on_peer_discovered)

    discovery = UDPDiscovery(config.sync_port, registry)
    watcher = FileWatcher(config.watch_dirs, engine.on_file_change)

    # Wire IPC
    ipc.attach(engine, watcher, registry)

    def shutdown(sig, frame):
        logger.info("Shutting down…")
        print("\n\033[93m[ShadowSync] Shutting down…\033[0m")
        watcher.stop()
        discovery.stop()
        engine.stop()
        ipc.stop()
        stats = engine.get_stats()
        print(f"\033[93m[ShadowSync] Session: sent={stats['sent']} "
              f"received={stats['received']} errors={stats['errors']} "
              f"conflicts={stats['conflicts']}\033[0m")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    engine.start(worker_count=4)
    discovery.start()
    watcher.start()
    ipc.start()

    logger.info(f"Watching: {config.watch_dirs}")
    logger.info(f"Sync port: {config.sync_port} | IPC: /tmp/shadowsync.sock")
    print(f"\033[92m[ShadowSync] Running — {len(config.watch_dirs)} folder(s), "
          f"IPC at /tmp/shadowsync.sock\033[0m\n")

    while True:
        time.sleep(10)
        peers = registry.get_all()
        if peers:
            logger.debug(f"Active peers: {[p.hostname for p in peers]}")


if __name__ == "__main__":
    main()
