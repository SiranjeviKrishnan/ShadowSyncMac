import os
import time
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime

WATCH_DIRECTORY = os.path.expanduser("~/Documents")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "fs_log.json")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

class ChangeHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.changes = []

    def on_any_event(self, event):
        if event.is_directory:
            return

        action = None
        if event.event_type == 'created':
            action = 'created'
        elif event.event_type == 'modified':
            action = 'modified'
        elif event.event_type == 'deleted':
            action = 'deleted'
        elif event.event_type == 'moved':
            action = 'moved'

        if action:
            change = {
                'timestamp': datetime.now().isoformat(),
                'event': action,
                'src_path': event.src_path
            }
            if hasattr(event, 'dest_path'):
                change['dest_path'] = event.dest_path
            self.changes.append(change)
            print(f"[{action.upper()}] {event.src_path}")
            self.write_log()
            trigger_sync(change)

    def write_log(self):
        with open(LOG_FILE, 'w') as logf:
            json.dump(self.changes, logf, indent=4)

def trigger_sync(change):
    print("[SYNC] Triggering sync for:", change)

if __name__ == "__main__":
    print(f"[WATCHER] Monitoring directory: {WATCH_DIRECTORY}")
    event_handler = ChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIRECTORY, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()