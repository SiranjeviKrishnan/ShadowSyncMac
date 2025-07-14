import socket
import threading
import os
from .utils import create_snapshot, load_snapshot, save_snapshot, get_diff_snapshot, copy_files

SYNC_FOLDER = os.path.expanduser("~/Documents")
TCP_PORT = 9000

active_connections = []
shutdown_event = threading.Event()

def receive_file(client_sock, dest_folder):
    # For simplicity, we receive a tar archive of changed files
    # (You can improve this with real protocols)
    import tarfile
    import io

    try:
        data = b''
        while True:
            packet = client_sock.recv(4096)
            if not packet:
                break
            data += packet
        tar_stream = io.BytesIO(data)
        with tarfile.open(fileobj=tar_stream, mode='r:') as tar:
            tar.extractall(path=dest_folder)
        print(f"[SYNC] Received files extracted to {dest_folder}")
    except Exception as e:
        print(f"[SYNC] Error receiving files: {e}")
    finally:
        client_sock.close()

def send_file(sock, folder_path, changed_files):
    import tarfile
    import io

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        for rel_path in changed_files:
            full_path = os.path.join(folder_path, rel_path)
            tar.add(full_path, arcname=rel_path)
    tar_stream.seek(0)
    try:
        sock.sendall(tar_stream.read())
        print(f"[SYNC] Sent {len(changed_files)} files")
    except Exception as e:
        print(f"[SYNC] Error sending files: {e}")

def start_tcp_server(tcp_port=TCP_PORT):
    def run_server():
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(('', tcp_port))
            server_sock.listen(5)
            print(f"[TCP] Server listening on port {tcp_port}")
            active_connections.append(server_sock)

            while not shutdown_event.is_set():
                server_sock.settimeout(1.0)
                try:
                    client_sock, addr = server_sock.accept()
                    print(f"[TCP] Connection from {addr}")
                    threading.Thread(target=receive_file, args=(client_sock, SYNC_FOLDER), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"[TCP] Server error: {e}")

        except OSError as e:
            print(f"[TCP ERROR] Could not bind port {tcp_port}: {e}")
        finally:
            server_sock.close()

    threading.Thread(target=run_server, daemon=True).start()

def stop_tcp_server():
    shutdown_event.set()
    for sock in active_connections:
        try:
            sock.close()
        except Exception:
            pass

def sync_with_peer(peer_ip, peer_port=TCP_PORT):
    """
    Connect to peer, compare snapshots, send changed files
    """
    try:
        old_snapshot = load_snapshot()
        new_snapshot = create_snapshot(SYNC_FOLDER)
        changed_files = get_diff_snapshot(old_snapshot, new_snapshot)
        if not changed_files:
            print("[SYNC] No changes to sync.")
            return

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((peer_ip, peer_port))
            print(f"[SYNC] Connected to peer {peer_ip}:{peer_port}")
            send_file(sock, SYNC_FOLDER, changed_files)

        # After successful send, save new snapshot
        save_snapshot(new_snapshot)
    except Exception as e:
        print(f"[SYNC] Sync failed with peer {peer_ip}:{peer_port} - {e}")
