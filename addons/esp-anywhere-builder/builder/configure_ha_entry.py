#!/usr/bin/env python3
"""Atomically add local builder settings to an existing HA config entry."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile

if len(sys.argv) != 3:
    raise SystemExit("usage: configure_ha_entry.py CORE_CONFIG_ENTRIES TOKEN_FILE")
storage = Path(sys.argv[1])
token_file = Path(sys.argv[2])
token = token_file.read_text().strip()
if len(token) < 32:
    raise SystemExit("builder token is invalid")
document = json.loads(storage.read_text())
entries = [
    entry
    for entry in document.get("data", {}).get("entries", [])
    if entry.get("domain") == "esp_anywhere"
]
if len(entries) != 1:
    raise SystemExit(f"expected exactly one ESP Anywhere entry, found {len(entries)}")
entry_data = entries[0].setdefault("data", {})
entry_data["builder_url"] = "http://127.0.0.1:8787"
entry_data["builder_token"] = token
stat = storage.stat()
fd, temporary = tempfile.mkstemp(prefix=".core.config_entries.", dir=storage.parent)
try:
    with os.fdopen(fd, "w") as output:
        json.dump(document, output, ensure_ascii=False, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.chmod(temporary, stat.st_mode & 0o777)
    os.replace(temporary, storage)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print("ESP Anywhere builder settings installed")
