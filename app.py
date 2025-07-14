from data.sync_engine import start_tcp_server, stop_tcp_server, sync_with_peer
from data.discovery import start_broadcast_listener, start_broadcast_sender

import signal
import sys
import time

TCP_PORT = 9000
BROADCAST_PORT = 9001

def signal_handler(sig, frame):
    print("\n\033[91m[APP] Shutting down...\033[0m")
    stop_tcp_server()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
print("\033[91m[APP] Press Ctrl+C to stop the application.\033[0m")

def main():
    start_tcp_server(TCP_PORT)
    start_broadcast_listener(BROADCAST_PORT)
    start_broadcast_sender(BROADCAST_PORT)

    print("\033[91m[APP] Running. Press Ctrl+C to stop.\033[0m")

    # Just loop forever, syncing can be triggered on peer discovery
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
