"""Start ESP Anywhere Builder from Home Assistant add-on options."""

from __future__ import annotations

import json
import os
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")
DATA_DIR = Path("/data")
SECRET_DIR = Path("/data/secrets")


def required(options: dict[str, object], name: str, minimum: int = 1) -> str:
    value = options.get(name)
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise SystemExit(f"Missing or invalid required add-on option: {name}")
    return value.strip()


def write_secret(name: str, value: str) -> Path:
    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = SECRET_DIR / name
    path.write_text(value.rstrip() + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def main() -> None:
    options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(options, dict):
        raise SystemExit("Home Assistant add-on options must be an object")

    worker_token = write_secret("worker_ha_token", required(options, "worker_ha_token", 32))
    builder_token = write_secret("builder_token", required(options, "builder_token", 32))
    signing_key = write_secret("firmware_signing_key", required(options, "firmware_signing_key", 64))

    environment = {
        "BUILDER_PORT": "8099",
        "CONFIG_DIR": "/homeassistant/esphome",
        "WORK_DIR": str(DATA_DIR / "work"),
        "STATE_DIR": str(DATA_DIR / "state"),
        "ARTIFACT_DIR": str(DATA_DIR / "artifacts"),
        "ARTIFACT_PUBLISHER": "worker",
        "WORKER_URL": required(options, "worker_url"),
        "PUBLIC_ARTIFACT_BASE_URL": required(options, "public_artifact_base_url"),
        "INSTALLATION_ID": required(options, "installation_id"),
        "WORKER_HA_TOKEN_FILE": str(worker_token),
        "BUILDER_TOKEN_FILE": str(builder_token),
        "SIGNING_KEY": str(signing_key),
        "SIGNING_KEY_ID": required(options, "signing_key_id"),
        "PUBLISH_BOOTSTRAP_POINTER": "false",
        "HOME": str(DATA_DIR / "home"),
        "PLATFORMIO_CORE_DIR": str(DATA_DIR / "platformio"),
    }
    for path in (DATA_DIR / name for name in ("work", "state", "artifacts", "home", "platformio")):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.update(environment)
    os.chdir("/app/esp-anywhere-builder")
    os.execvpe("python3", ["python3", "app.py"], os.environ)


if __name__ == "__main__":
    main()
