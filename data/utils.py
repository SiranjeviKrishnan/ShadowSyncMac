import os
import hashlib
import json
import shutil

SNAPSHOT_FILE = 'data/snapshot.json'

def hash_file(filepath):
    """Return SHA256 hash of a file"""
    sha = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    return sha.hexdigest()

def create_snapshot(folder_path):
    """
    Create a snapshot dict of all files under folder_path:
    { relative_path: hash, ... }
    """
    snapshot = {}
    for root, _, files in os.walk(folder_path):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, folder_path)
            snapshot[rel_path] = hash_file(full_path)
    return snapshot

def load_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    with open(SNAPSHOT_FILE, 'r') as f:
        return json.load(f)

def save_snapshot(snapshot):
    os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
    with open(SNAPSHOT_FILE, 'w') as f:
        json.dump(snapshot, f, indent=2)

def get_diff_snapshot(old_snapshot, new_snapshot):
    """
    Return list of relative file paths that are new or changed
    """
    changed_files = []
    for path, new_hash in new_snapshot.items():
        old_hash = old_snapshot.get(path)
        if old_hash != new_hash:
            changed_files.append(path)
    return changed_files

def copy_files(folder_path, files_to_copy, dest_path):
    """
    Copy only files listed in files_to_copy from folder_path to dest_path,
    preserving folder structure.
    """
    for rel_path in files_to_copy:
        src_file = os.path.join(folder_path, rel_path)
        dest_file = os.path.join(dest_path, rel_path)
        dest_dir = os.path.dirname(dest_file)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src_file, dest_file)
