#!/usr/bin/env python3
"""Provision a locally connected ESP Anywhere device without exposing secrets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import urllib.request

import serial
import yaml


def api(url: str, token: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def wait_for_port(path: Path, deadline: float) -> None:
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.25)
    raise TimeoutError(f"Serial port did not return: {path}")


def transmit(path: Path, packet: dict, deadline: float) -> bool:
    wait_for_port(path, deadline)
    payload = (json.dumps(packet, separators=(",", ":")) + "\n").encode()
    with serial.Serial(str(path), 115200, timeout=0.25, write_timeout=5) as port:
        port.dtr = False
        port.rts = False
        time.sleep(1)
        port.reset_input_buffer()
        for offset in range(0, len(payload), 64):
            port.write(payload[offset : offset + 64])
            port.flush()
            time.sleep(0.02)
        buffer = b""
        while time.monotonic() < deadline:
            chunk = port.read(256)
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                stage = event.get("stage")
                status = event.get("status")
                if stage:
                    print(f"{stage}: {status}", flush=True)
                if status == "error":
                    raise RuntimeError(event.get("error") or f"{stage} failed")
                if stage == "discovery_sent" and status == "ok":
                    return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--device-name", required=True)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--builder-url", default="http://127.0.0.1:8787")
    parser.add_argument("--builder-token-file", type=Path, required=True)
    parser.add_argument("--secrets-file", type=Path, required=True)
    args = parser.parse_args()

    secrets = yaml.safe_load(args.secrets_file.read_text()) or {}
    ssid = str(secrets.get("wifi_ssid", ""))
    password = str(secrets.get("wifi_password", ""))
    if not ssid or not password:
        raise SystemExit("wifi_ssid and wifi_password are required in secrets file")
    token = args.builder_token_file.read_text().strip()
    config = api(args.builder_url, token, "/v1/provision", {
        "project_id": args.project, "build_id": args.build, "device_name": args.device_name,
    })
    packet = {
        "type": "provision", "wifi_ssid": ssid, "wifi_password": password,
        "relay_url": config["relay_url"], "installation_id": config["installation_id"],
        "activation_code": config["activation_code"], "device_id": config["device_id"],
        "device_name": config["device_name"],
    }
    deadline = time.monotonic() + 120
    for attempt in range(1, 5):
        try:
            if transmit(Path(args.port), packet, deadline):
                print(f"ready: {config['device_id']}")
                return
        except (OSError, serial.SerialException) as error:
            print(f"serial reconnect {attempt}: {type(error).__name__}", flush=True)
        time.sleep(1)
    raise SystemExit("Provisioning did not complete before timeout")


if __name__ == "__main__":
    main()
