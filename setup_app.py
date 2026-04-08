"""
ShadowSync macOS App Bundle
Build with: python setup_app.py py2app

Produces: dist/ShadowSync.app — distributable macOS .app
Requires:  pip install py2app rumps pyobjc-framework-Cocoa watchdog
"""

from setuptools import setup

APP = ["menubar_app.py"]
DATA_FILES = [
    ("ui", ["ui/dashboard.html"]),
    ("assets", ["assets/logo.jpeg"]),  # icon source
]

OPTIONS = {
    "argv_emulation": True,
    "plist": {
        "CFBundleName": "ShadowSync",
        "CFBundleDisplayName": "ShadowSync",
        "CFBundleIdentifier": "com.shadowsync.mac",
        "CFBundleVersion": "2.0.0",
        "CFBundleShortVersionString": "2.0",
        "LSUIElement": True,               # Menu bar only — no Dock icon
        "LSMinimumSystemVersion": "12.0",  # macOS Monterey+
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # Support dark mode
        "NSLocalNetworkUsageDescription": (
            "ShadowSync uses your local network to discover "
            "and sync files with other Macs."
        ),
        "NSDesktopFolderUsageDescription": "ShadowSync needs access to sync your Desktop.",
        "NSDocumentsFolderUsageDescription": "ShadowSync needs access to sync your Documents.",
        "NSDownloadsFolderUsageDescription": "ShadowSync needs access to sync your Downloads.",
    },
    "packages": ["watchdog", "rumps"],
    "includes": [
        "core.watcher",
        "core.discovery",
        "core.sync_engine",
        "core.transport",
        "core.conflict",
        "config.settings",
    ],
    "iconfile": "assets/ShadowSync.icns",   # generate from logo.jpeg using iconutil
    "strip": False,
    "optimize": 2,
}

setup(
    name="ShadowSync",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
