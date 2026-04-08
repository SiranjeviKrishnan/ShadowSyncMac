"""
ShadowSync - macOS Menu Bar App
Uses rumps for native macOS menu bar integration.
Sits in the system tray with live sync status, peer list, and quick actions.

Run: python menubar_app.py
"""

import os
import sys
import threading
import time
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent))

try:
    import rumps
    RUMPS_AVAILABLE = True
except ImportError:
    RUMPS_AVAILABLE = False
    print("rumps not installed. See requirements.txt for dependencies.")

from config.settings import SyncConfig
from core.watcher import FileWatcher
from core.discovery import UDPDiscovery, PeerRegistry
from core.sync_engine import SyncEngine
from core.transport import TransportManager


# ── Icons (using emoji as fallback — replace with .icns in production) ────────
ICON_IDLE = "☁"
ICON_SYNCING = "↕"
ICON_ERROR = "⚠"
ICON_PAUSED = "⏸"


class ShadowSyncMenuBar(rumps.App):
    """
    Native macOS menu bar app for ShadowSync.
    Shows sync status, connected peers, recent activity, and controls.
    """

    def __init__(self):
        super().__init__("ShadowSync", title=ICON_IDLE)

        self.config = SyncConfig.load()
        self._syncing = False
        self._paused = False
        self._peers: list = []
        self._recent_events: list[str] = []
        self._stats = {"sent": 0, "received": 0, "errors": 0}

        # Build initial menu
        self._build_menu()

        # Start backend in background
        self._engine: SyncEngine = None
        self._watcher: FileWatcher = None
        self._discovery: UDPDiscovery = None
        self._registry: PeerRegistry = None
        self._transport: TransportManager = None

        threading.Thread(target=self._start_backend, daemon=True).start()

    # ── Menu construction ─────────────────────────────────────────────────────

    def _build_menu(self):
        self.menu = [
            rumps.MenuItem("ShadowSync", callback=None),   # Title (non-clickable)
            None,  # separator
            rumps.MenuItem("Status: Starting…", callback=None),
            None,
            rumps.MenuItem("Peers (0)", callback=None),
            rumps.separator,
            rumps.MenuItem("Recent Activity", callback=None),
            rumps.separator,
            rumps.MenuItem("⏸  Pause Sync", callback=self.toggle_pause),
            rumps.MenuItem("🔄  Force Sync Now", callback=self.force_sync),
            rumps.MenuItem("📁  Open Watch Folder", callback=self.open_folder),
            rumps.MenuItem("📊  Open Dashboard", callback=self.open_dashboard),
            None,
            rumps.MenuItem("⚙  Preferences…", callback=self.open_preferences),
            rumps.MenuItem("ℹ  About ShadowSync", callback=self.show_about),
            None,
            rumps.MenuItem("Quit ShadowSync", callback=self.quit_app),
        ]

    # ── Backend startup ───────────────────────────────────────────────────────

    def _start_backend(self):
        try:
            self._registry = PeerRegistry()
            self._transport = TransportManager()

            self._engine = SyncEngine(
                watch_root=self.config.watch_dirs[0],
                port=self.config.sync_port,
                on_status=self._on_sync_event,
            )

            self._registry.register_callback(self._on_peer_event)
            self._registry.register_callback(self._engine.on_peer_discovered)

            self._discovery = UDPDiscovery(self.config.sync_port, self._registry)
            self._watcher = FileWatcher(self.config.watch_dirs, self._engine.on_file_change)

            self._engine.start(worker_count=4)
            self._discovery.start()
            self._watcher.start()

            self._update_status("Active — watching %d folder(s)" % len(self.config.watch_dirs))
            self.title = ICON_IDLE

        except Exception as e:
            self._update_status(f"Error: {e}")
            self.title = ICON_ERROR

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_sync_event(self, event_type: str, detail: str):
        # Update icon briefly
        self.title = ICON_SYNCING
        self._stats = self._engine.get_stats() if self._engine else self._stats

        # Add to recent activity
        ts = time.strftime("%H:%M:%S")
        icon = {"sync_ok": "↑", "received": "↓", "delete_ok": "✕", "peer_connected": "◈"}.get(event_type, "·")
        entry = f"{icon} {ts}  {detail}"
        self._recent_events.insert(0, entry)
        self._recent_events = self._recent_events[:5]

        self._refresh_menu()

        # Restore idle icon after 1.5s
        threading.Timer(1.5, self._restore_icon).start()

        # macOS notification
        if self.config.show_notifications and event_type in ("sync_ok", "received"):
            rumps.notification(
                title="ShadowSync",
                subtitle=event_type.replace("_", " ").title(),
                message=detail,
                sound=False,
            )

    def _on_peer_event(self, event: str, peer):
        self._peers = self._registry.get_all()
        self._refresh_menu()
        if event == "discovered" and self.config.show_notifications:
            rumps.notification(
                title="ShadowSync",
                subtitle="Peer Connected",
                message=f"{peer.hostname} joined your sync network",
                sound=False,
            )

    def _restore_icon(self):
        if not self._syncing:
            self.title = ICON_PAUSED if self._paused else ICON_IDLE

    # ── Menu refresh ──────────────────────────────────────────────────────────

    def _update_status(self, text: str):
        if "Status:" in str(self.menu.get("Status:", "")):
            pass
        try:
            self.menu["Status: Starting…"].title = f"Status: {text}"
        except Exception:
            pass

    def _refresh_menu(self):
        try:
            peers = self._peers
            peer_count = len(peers)

            # Peer count header
            peer_menu = self.menu.get("Peers (0)") or self.menu.get(f"Peers ({peer_count})")
            if peer_menu:
                peer_menu.title = f"Peers ({peer_count})"
                peer_menu.clear()
                if peers:
                    for p in peers:
                        peer_menu.add(rumps.MenuItem(f"  {p.hostname}  ({p.ip})"))
                else:
                    peer_menu.add(rumps.MenuItem("  No peers discovered"))

            # Recent activity
            activity_menu = self.menu.get("Recent Activity")
            if activity_menu:
                activity_menu.clear()
                if self._recent_events:
                    for e in self._recent_events:
                        activity_menu.add(rumps.MenuItem(e))
                else:
                    activity_menu.add(rumps.MenuItem("  No recent activity"))

        except Exception as e:
            pass  # Menu updates can race; ignore silently

    # ── Actions ───────────────────────────────────────────────────────────────

    @rumps.clicked("⏸  Pause Sync")
    def toggle_pause(self, sender):
        self._paused = not self._paused
        if self._paused:
            sender.title = "▶  Resume Sync"
            self.title = ICON_PAUSED
            self._update_status("Paused")
            if self._watcher:
                self._watcher.stop()
        else:
            sender.title = "⏸  Pause Sync"
            self.title = ICON_IDLE
            self._update_status("Active")
            if self._watcher:
                self._watcher = FileWatcher(self.config.watch_dirs, self._engine.on_file_change)
                self._watcher.start()

    @rumps.clicked("🔄  Force Sync Now")
    def force_sync(self, sender):
        if not self._engine or self._paused:
            return
        rumps.notification("ShadowSync", "Force Sync", "Triggering full directory scan…", sound=False)
        threading.Thread(target=self._do_force_sync, daemon=True).start()

    def _do_force_sync(self):
        """Walk the watch dir and queue all files for sync."""
        from core.watcher import ChangeEvent
        for watch_dir in self.config.watch_dirs:
            for root, _, files in os.walk(watch_dir):
                for f in files:
                    path = os.path.join(root, f)
                    ev = ChangeEvent("modified", path)
                    self._engine.on_file_change(ev)

    @rumps.clicked("📁  Open Watch Folder")
    def open_folder(self, sender):
        folder = self.config.watch_dirs[0] if self.config.watch_dirs else str(Path.home())
        os.system(f'open "{folder}"')

    @rumps.clicked("📊  Open Dashboard")
    def open_dashboard(self, sender):
        dashboard = Path(__file__).parent / "ui" / "dashboard.html"
        os.system(f'open "{dashboard}"')

    @rumps.clicked("⚙  Preferences…")
    def open_preferences(self, sender):
        # In a full app, this opens a SwiftUI/AppKit preferences window.
        # For now, open the config file in the default editor.
        from config.settings import CONFIG_FILE
        os.system(f'open "{CONFIG_FILE}"')

    @rumps.clicked("ℹ  About ShadowSync")
    def show_about(self, sender):
        rumps.alert(
            title="ShadowSync v2.0",
            message=(
                "Real-time Mac-to-Mac file sync.\n\n"
                "Zero config. Peer-to-peer.\n"
                "Thunderbolt · LAN · Tailscale\n\n"
                "github.com/SiranjeviKrishnan/ShadowSyncMac"
            ),
        )

    @rumps.clicked("Quit ShadowSync")
    def quit_app(self, sender):
        if self._watcher:
            self._watcher.stop()
        if self._discovery:
            self._discovery.stop()
        if self._engine:
            self._engine.stop()
        rumps.quit_application()


def main():
    if not RUMPS_AVAILABLE:
        print("rumps is not installed. See requirements.txt for dependencies.")
        sys.exit(1)
    app = ShadowSyncMenuBar()
    app.run()


if __name__ == "__main__":
    main()
