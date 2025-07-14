# ShadowSync

ShadowSync is a real-time synchronization tool designed to automatically detect and sync changes (added, removed, modified files) between two Macs on the same network. It currently monitors specified directories for file changes, logs these events, and lays the groundwork for full syncing functionality. Future plans include support for Thunderbolt direct connection and Tailscale VPN integration.

---

## Project Structure

- `app.py`  
  Main application entry point.

- `/data`  
  Core Python modules:  
  - `watch_changes.py` — Monitors directories for file changes and logs changes  
  - `sync_engine.py` — Sync engine: handles syncing logic and communication  
  - `discovery.py` — Peer discovery via UDP broadcast and listening  
  - Other supporting scripts

- `/logs`  
  Log files generated at runtime.

- `README.md`  
  This documentation file.

---

## Features

- Real-time detection of file system changes: additions, deletions, modifications.  
- Detailed logging of file events with timestamps.  
- Network peer discovery via UDP broadcast for automatic detection of other devices running ShadowSync.  
- Basic TCP server and client framework for communication.  
- Modular design to support future extensions like GUI, automatic syncing, and multi-network support.

---

## Getting Started

### Prerequisites

- Python 3.11 (recommended)  
- `watchdog` Python package (for file system monitoring)  
- Network access between Macs (same LAN or direct connection)

### Installation

1. Clone this repository:  
   ```bash
   git clone https://github.com/yourusername/ShadowSync.git
   cd ShadowSync
   ```
2. Install dependencies:
   ```bash
   pip install watchdog
   ```

### Running
Run the main application:

```bash
python3 app.py
```

This will start monitoring the configured directories and begin peer discovery on the network.

### Testing
For initial testing, run the app on two Macs connected to the same network.

Make file changes in the monitored folders to see logs of added, removed, or modified files in real-time.

Logs will be saved under the /logs directory for review.

### Docker
Docker support is planned for easier deployment. Future updates will include Dockerfile and images for quick setup.

---

## TODO
- Implement Thunderbolt direct connection for faster syncing (priority).
- Integrate Tailscale VPN support for secure syncing across remote devices.
- Develop a graphical user interface (GUI) for easier user interaction.
- Add conflict detection and resolution mechanisms.
- Automate sync actions based on user confirmation.
- Enhance peer discovery with user/device identification without manual naming.
- Add Docker support for cross-platform deployment.

---

## License
This project is licensed under the MIT License.

---

## Contact
For questions or contributions, please open an issue or submit a pull request on GitHub.

Thank you for checking out ShadowSync!