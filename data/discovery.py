import socket
import threading
import json
from .sync_engine import sync_with_peer

BROADCAST_IP = '255.255.255.255'

def start_broadcast_sender(port):
    def send_broadcast():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        message = json.dumps({"port": port}).encode('utf-8')

        while True:
            sock.sendto(message, (BROADCAST_IP, port))
            threading.Event().wait(5)

    threading.Thread(target=send_broadcast, daemon=True).start()

def start_broadcast_listener(port):
    def listen():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('', port))
        print(f"[DISCOVERY] Listening for broadcasts on port {port}")

        while True:
            data, addr = sock.recvfrom(1024)
            try:
                info = json.loads(data.decode('utf-8'))
                peer_port = info.get("port", None)
                peer_ip = addr[0]

                if peer_ip != socket.gethostbyname(socket.gethostname()):
                    print(f"[DISCOVERY] Found peer {peer_ip}:{peer_port}")
                    sync_with_peer(peer_ip, peer_port)
            except Exception as e:
                print(f"[DISCOVERY] Invalid broadcast from {addr}: {e}")

    threading.Thread(target=listen, daemon=True).start()
