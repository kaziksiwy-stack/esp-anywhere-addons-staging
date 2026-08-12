"""Start ESP Anywhere Builder with zero-copy staging onboarding."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.request

DATA_DIR = Path("/data")
HA_ENTRIES = Path("/homeassistant/.storage/core.config_entries")
SECRET_DIR = DATA_DIR / "secrets"
DEFAULT_WORKER_URL = "https://esp-anywhere-worker-staging.esp-anywhere-worker.workers.dev"
SIGNING_KEY_ID = "staging-esphome-2026-08"


def write_secret(name: str, value: str) -> Path:
    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = SECRET_DIR / name
    path.write_text(value.rstrip() + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def managed_installation(document: dict[str, object], worker_url: str = DEFAULT_WORKER_URL) -> dict[str, str]:
    entries = document.get("data", {}).get("entries", [])
    matches: list[dict[str, str]] = []
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict) or raw.get("domain") != "esp_anywhere":
            continue
        data = raw.get("data")
        if not isinstance(data, dict):
            continue
        values = (data.get("installation_id"), data.get("token"), data.get("relay_url"))
        if all(isinstance(value, str) and value for value in values) and values[2].rstrip("/") == worker_url:
            matches.append({
                "installation_id": values[0], "token": values[1],
                "relay_url": values[2].rstrip("/"),
            })
    if len(matches) != 1:
        raise SystemExit(
            "Setup requires exactly one staging ESP Anywhere integration in Home Assistant; "
            f"found {len(matches)} for {worker_url}"
        )
    return matches[0]


def provision_signing_key(worker_url: str, installation_id: str, token: str) -> tuple[str, str]:
    request = urllib.request.Request(
        f"{worker_url}/ha/builder-bootstrap", method="POST",
        data=json.dumps({"installation_id": installation_id}, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "ESP-Anywhere-Builder-Addon/0.2"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise SystemExit("Cannot securely provision Builder signing credentials") from error
    key_id, private_key = payload.get("key_id"), payload.get("private_key")
    if key_id != SIGNING_KEY_ID or not isinstance(private_key, str) or "PRIVATE KEY" not in private_key:
        raise SystemExit("Worker returned invalid Builder signing credentials")
    return key_id, private_key


def main() -> None:
    installation = managed_installation(json.loads(HA_ENTRIES.read_text(encoding="utf-8")))
    worker_url = installation["relay_url"]

    worker_token = write_secret("worker_ha_token", installation["token"])
    builder_token_path = SECRET_DIR / "builder_token"
    if not builder_token_path.is_file():
        write_secret("builder_token", secrets.token_urlsafe(48))
    key_path = SECRET_DIR / "firmware_signing_key"
    if not key_path.is_file():
        key_id, private_key = provision_signing_key(
            worker_url, installation["installation_id"], installation["token"]
        )
        write_secret("firmware_signing_key", private_key)
        write_secret("signing_key_id", key_id)
    key_id = (SECRET_DIR / "signing_key_id").read_text(encoding="utf-8").strip()
    if key_id != SIGNING_KEY_ID:
        raise SystemExit("Stored signing key ID is not trusted by staging firmware")

    environment = {
        "BUILDER_PORT": "8099", "CONFIG_DIR": "/homeassistant/esphome",
        "WORK_DIR": str(DATA_DIR / "work"), "STATE_DIR": str(DATA_DIR / "state"), "BUILDER_TMP_DIR": str(DATA_DIR / "work" / "tmp"),
        "ARTIFACT_DIR": str(DATA_DIR / "artifacts"), "ARTIFACT_PUBLISHER": "worker",
        "WORKER_URL": worker_url, "PUBLIC_ARTIFACT_BASE_URL": f"{worker_url}/artifacts",
        "INSTALLATION_ID": installation["installation_id"],
        "WORKER_HA_TOKEN_FILE": str(worker_token),
        "BUILDER_TOKEN_FILE": str(builder_token_path), "SIGNING_KEY": str(key_path),
        "SIGNING_KEY_ID": key_id, "PUBLISH_BOOTSTRAP_POINTER": "false",
        "HOME": str(DATA_DIR / "home"), "PLATFORMIO_CORE_DIR": str(DATA_DIR / "platformio"),
    }
    for path in (DATA_DIR / name for name in ("work", "state", "artifacts", "home", "platformio")):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.update(environment)
    os.chdir("/app/esp-anywhere-builder")
    os.execvpe("python3", ["python3", "app.py"], os.environ)


if __name__ == "__main__":
    main()
