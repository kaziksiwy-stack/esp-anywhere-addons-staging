#!/usr/bin/env python3
"""Build an ordinary ESPHome YAML through an ephemeral ESP Anywhere wrapper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

import yaml
from esphome import yaml_util as esphome_yaml

COMPONENT_VERSION = "overlay-poc-v1"
SUPPORTED_DOMAINS = {"sensor", "binary_sensor", "switch", "text"}
KNOWN_ENTITY_DOMAINS = SUPPORTED_DOMAINS | {
    "button", "number", "select", "light", "fan", "cover", "climate",
    "alarm_control_panel", "media_player", "lock", "valve", "date", "time",
    "datetime", "event", "update",
}


def run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=3600, check=False)
    if result.returncode:
        # ESPHome may echo substitutions. Never propagate its full output.
        lines = [line for line in result.stdout.splitlines() if line.strip()][-12:]
        message = "\n".join(lines)
        message = re.sub(r"(?i)(password|token|secret)(\s*[:=]\s*)\S+", r"\1\2<redacted>", message)
        raise RuntimeError(message[:1600] if message else "ESPHome command failed")
    return result.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def esphome_version(esphome: str, cwd: Path) -> str:
    output = run([esphome, "version"], cwd).strip()
    prefix = "Version: "
    if not output.startswith(prefix) or not output[len(prefix):]:
        raise RuntimeError("Could not determine ESPHome version")
    return output[len(prefix):]


def render_overlay(user_file: str, component_path: Path, values: dict[str, str]) -> str:
    # Values are JSON-quoted, which is valid YAML and avoids hand-written escaping.
    q = lambda value: json.dumps(value)
    managed = values.get("mode") == "managed"
    wifi = """wifi:
  ap: {}

improv_serial:

web_server:
  port: 80
  version: 3
  include_internal: true
""" if managed else ""
    mqtt_credentials = f"""  broker: {q(values['relay_host'])}
  enable_on_boot: false
""" if managed else """  broker: !secret esp_anywhere_relay_host
  username: !secret esp_anywhere_device_username
  password: !secret esp_anywhere_device_token
"""
    claim_input = """text:
  - platform: template
    id: esp_anywhere_claim_input
    name: ESP Anywhere claim
    internal: true
    optimistic: true
    mode: text
    min_length: 16
    max_length: 128
    on_value:
      - lambda: id(esp_anywhere_bridge).provision_claim(x);
""" if managed else ""
    managed_config = f"""  managed_provisioning: true
  claim_url: {q(values['claim_url'])}
  relay_host: {q(values['relay_host'])}
""" if managed else ""
    return f"""packages:
  user_project: !include {q(user_file)}

esphome:
  project:
    name: esp-anywhere.device
    version: {q(values['firmware_version'])}

external_components:
  - source:
      type: local
      path: {q(str(component_path))}
    components: [esp_anywhere]

{wifi}

time:
  - platform: sntp
    id: esp_anywhere_time
    timezone: UTC

http_request:
  id: esp_anywhere_http
  timeout: 30s
  verify_ssl: true

mqtt:
  id: esp_anywhere_mqtt
{mqtt_credentials.rstrip()}
  port: 8883
  discovery: false
  discover_ip: false
  topic_prefix: esp-anywhere-internal/{values['device_id']}
  birth_message: null
  will_message: null
  shutdown_message: null
  log_topic: null
  reboot_timeout: 0s

{claim_input}

esp_anywhere:
  id: esp_anywhere_bridge
  mqtt_id: esp_anywhere_mqtt
  time_id: esp_anywhere_time
  http_request_id: esp_anywhere_http
  tenant_id: {q(values['installation_id'])}
  device_id: {q(values['device_id'])}
  friendly_name: {q(values['friendly_name'])}
  model: {q(values['model'])}
  firmware_version: {q(values['firmware_version'])}
  update_manifest_url: {q(values.get('update_manifest_url', ''))}
  auto_register_entities: true
{managed_config.rstrip()}
"""


class ProcessedConfigLoader(yaml.SafeLoader):
    """Load ESPHome's normalized output while retaining only secret references."""


def _construct_processed_tag(loader: ProcessedConfigLoader, node):
    """Preserve normalized ESPHome tagged values as inert YAML data."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    raise RuntimeError("Unsupported node in ESPHome normalized configuration")


ProcessedConfigLoader.add_constructor(
    "!secret", lambda loader, node: {"secret_ref": loader.construct_scalar(node)}
)
ProcessedConfigLoader.add_constructor(None, _construct_processed_tag)


def parse_processed_config(config_output: str) -> dict[str, Any]:
    start = config_output.find("esphome:\n")
    if start < 0:
        raise RuntimeError("ESPHome did not emit normalized configuration")
    end_marker = "\nINFO Configuration is valid!"
    end = config_output.find(end_marker, start)
    document = config_output[start:end if end >= 0 else None]
    config = yaml.load(document, Loader=ProcessedConfigLoader)
    if not isinstance(config, dict):
        raise RuntimeError("ESPHome normalized configuration is not a mapping")
    return config


def find_unsupported(config_output: str) -> list[str]:
    config = parse_processed_config(config_output)
    return sorted(domain for domain in config if domain in KNOWN_ENTITY_DOMAINS and domain not in SUPPORTED_DOMAINS)


def parse_target(config_output: str) -> dict[str, str]:
    config = parse_processed_config(config_output)
    esp32 = config.get("esp32") if isinstance(config, dict) else None
    if not isinstance(esp32, dict):
        raise RuntimeError("PoC capability matrix currently supports only ESP32 targets")
    framework = esp32.get("framework", {})
    return {
        "board": str(esp32["board"]),
        "chip_family": str(esp32["variant"]),
        "framework": str(framework["type"]),
        "framework_version": str(framework["version"]),
        "flash_size": str(esp32["flash_size"]),
    }


def build(source: Path, output: Path, values: dict[str, str], esphome: str) -> dict[str, Any]:
    source = source.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    before = sha256(source)
    actual_esphome_version = esphome_version(esphome, source.parent)
    source_config = run([esphome, "config", str(source)], source.parent)
    target = parse_target(source_config)
    unsupported = find_unsupported(source_config)
    component_path = Path(__file__).parents[1] / "esp-anywhere-esphome" / "components"

    with tempfile.TemporaryDirectory(prefix="esp-anywhere-overlay-") as raw_temp:
        temp = Path(raw_temp)
        shutil.copytree(source.parent, temp / "project", ignore=shutil.ignore_patterns(".esphome", "build", ".git"))
        project = temp / "project"
        included_user_file = source.name
        if values.get("mode") == "managed":
            copied_source = project / source.name
            managed_config = esphome_yaml.load_yaml(copied_source)
            if not isinstance(managed_config, dict):
                raise RuntimeError("User ESPHome YAML must contain a mapping")
            managed_config.pop("wifi", None)
            included_user_file = f"managed-{source.name}"
            (project / included_user_file).write_text(esphome_yaml.dump(managed_config))
        wrapper = project / "esp-anywhere-overlay.yaml"
        overlay = render_overlay(included_user_file, component_path.resolve(), values)
        wrapper.write_text(overlay)
        if values.get("mode") == "portable":
            secrets_path = project / "secrets.yaml"
            secrets = yaml.safe_load(secrets_path.read_text()) if secrets_path.exists() else {}
            if not isinstance(secrets, dict):
                raise RuntimeError("Project secrets.yaml must contain a mapping")
            secrets.update({
                "esp_anywhere_relay_host": os.environ["ESP_ANYWHERE_RELAY_HOST"],
                "esp_anywhere_device_username": os.environ["ESP_ANYWHERE_DEVICE_USERNAME"],
                "esp_anywhere_device_token": os.environ["ESP_ANYWHERE_DEVICE_TOKEN"],
            })
            # Only the temporary copy is rewritten; values are never logged or exported.
            secrets_path.write_text(yaml.safe_dump(secrets, sort_keys=True))
        run([esphome, "config", str(wrapper)], project)
        run([esphome, "compile", str(wrapper)], project)
        factories = list((project / ".esphome" / "build").rglob("firmware.factory.bin"))
        otas = list((project / ".esphome" / "build").rglob("firmware.ota.bin"))
        if len(factories) != 1 or len(otas) != 1:
            raise RuntimeError("ESPHome did not produce exactly one factory and OTA image")
        factory = output / f"{values['device_id']}.factory.bin"
        ota = output / f"{values['device_id']}.ota.bin"
        shutil.copy2(factories[0], factory)
        shutil.copy2(otas[0], ota)
        (output / f"{values['device_id']}.overlay.yaml").write_text(overlay)

    if sha256(source) != before:
        raise RuntimeError("source YAML changed during build")
    manifest = {
        "name": values["friendly_name"],
        "version": values["firmware_version"],
        "new_install_prompt_erase": False,
        "builds": [{"chipFamily": target["chip_family"], "parts": [{
            "path": factory.name, "offset": 0,
        }]}],
    }
    (output / f"{values['device_id']}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    metadata = {
        "builder": "esp-anywhere-overlay-poc",
        "esphome_version": actual_esphome_version,
        "external_component_version": COMPONENT_VERSION,
        "source": source.name,
        "source_sha256": before,
        **target,
        "registry": "runtime App.get_*",
        "provisioning_mode": values["mode"],
        "supported_entity_domains": sorted(SUPPORTED_DOMAINS),
        "unsupported_entity_domains": unsupported,
        "artifacts": {
            "factory": {"file": factory.name, "size": factory.stat().st_size, "sha256": sha256(factory)},
            "ota": {"file": ota.name, "size": ota.stat().st_size, "sha256": sha256(ota)},
        },
    }
    (output / f"{values['device_id']}.build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--installation-id", required=True)
    parser.add_argument("--friendly-name", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--firmware-version", required=True)
    parser.add_argument("--mode", choices=("portable", "managed"), default="portable")
    parser.add_argument("--claim-url", default="")
    parser.add_argument("--relay-host", default="")
    parser.add_argument("--esphome", default="esphome")
    args = parser.parse_args()
    values = vars(args).copy()
    values.pop("source"); values.pop("output"); values.pop("esphome")
    build(args.source, args.output, values, args.esphome)


if __name__ == "__main__":
    main()
