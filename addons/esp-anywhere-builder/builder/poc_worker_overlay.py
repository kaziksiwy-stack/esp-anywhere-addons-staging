#!/usr/bin/env python3
"""Build an ordinary ESPHome project with an ephemeral v0.3 WSS overlay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile

from esphome import yaml_util as esphome_yaml

from ota_capabilities import detect_ota_capabilities, rollback_enabled_from_build
from ota_manifest import build_signed_manifest
from poc_overlay import KNOWN_ENTITY_DOMAINS, esphome_version, parse_processed_config, parse_target, run, sha256

COMPONENT_VERSION = "external-component-v03-secure-ota-1"
SUPPORTED_DOMAINS = {"switch", "sensor", "binary_sensor", "button", "number", "text"}


def validate_transport_target(target: dict[str, str]) -> None:
    """Reject frameworks for which the current WSS transport cannot compile."""
    framework = target.get("framework", "")
    if framework != "arduino":
        raise RuntimeError(
            "ESP Anywhere v0.3 external component currently supports the Arduino "
            f"framework; framework {framework or '<unknown>'} requires an ESP-IDF-native "
            "HTTPS/WSS transport"
        )


def report_entity_domains(config_output: str) -> tuple[list[str], list[dict[str, object]]]:
    """Return supported domains and explicit unsupported-domain diagnostics."""
    config = parse_processed_config(config_output)
    supported = sorted(domain for domain in config if domain in SUPPORTED_DOMAINS)
    unsupported = []
    for domain in sorted(domain for domain in config if domain in KNOWN_ENTITY_DOMAINS - SUPPORTED_DOMAINS):
        raw_entries = config[domain]
        entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
        internal = sum(isinstance(item, dict) and item.get("internal") is True for item in entries)
        unsupported.append({
            "domain": domain,
            "count": len(entries),
            "internal_count": internal,
            "warning": f"ESP Anywhere does not expose {domain}; local ESPHome behavior is unchanged",
        })
    return supported, unsupported


def render_overlay(user_file: str, component_path: Path, values: dict[str, str]) -> str:
    q = lambda value: json.dumps(value)
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
    components: [esp_anywhere_v03]

wifi:
  ap: {{}}
  power_save_mode: none

http_request:
  id: esp_anywhere_http
  timeout: 30s
  verify_ssl: true

esp_anywhere_v03:
  id: esp_anywhere_bridge
  http_request_id: esp_anywhere_http
  friendly_name: {q(values['friendly_name'])}
  model: {q(values['model'])}
  firmware_version: {q(values['firmware_version'])}
  ota_base_url: {q(values['ota_base_url'])}
  auto_register_entities: true
"""


def build(source: Path, output: Path, values: dict[str, str], esphome: str) -> dict:
    source = source.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    before = sha256(source)
    version = esphome_version(esphome, source.parent)
    source_config = run([esphome, "config", str(source)], source.parent)
    target = parse_target(source_config)
    validate_transport_target(target)
    supported_domains, unsupported_entities = report_entity_domains(source_config)
    component_path = Path(__file__).parents[1] / "esp-anywhere-esphome" / "components"
    with tempfile.TemporaryDirectory(prefix="esp-anywhere-v03-overlay-") as raw_temp:
        project = Path(raw_temp) / "project"
        shutil.copytree(source.parent, project, ignore=shutil.ignore_patterns(".esphome", "build", ".git"))
        config = esphome_yaml.load_yaml(project / source.name)
        if not isinstance(config, dict):
            raise RuntimeError("User ESPHome YAML must contain a mapping")
        config.pop("wifi", None)
        included = f"runtime-{source.name}"
        (project / included).write_text(esphome_yaml.dump(config))
        overlay = render_overlay(included, component_path.resolve(), values)
        wrapper = project / "esp-anywhere-v03-overlay.yaml"
        wrapper.write_text(overlay)
        run([esphome, "config", str(wrapper)], project)
        compile_output = run([esphome, "compile", str(wrapper)], project)
        factories = list((project / ".esphome" / "build").rglob("firmware.factory.bin"))
        otas = list((project / ".esphome" / "build").rglob("firmware.ota.bin"))
        if len(factories) != 1 or len(otas) != 1:
            raise RuntimeError("ESPHome did not produce exactly one factory and OTA image")
        factory = output / f"{values['artifact_name']}.factory.bin"
        ota = output / f"{values['artifact_name']}.ota.bin"
        shutil.copy2(factories[0], factory)
        shutil.copy2(otas[0], ota)
        capabilities = detect_ota_capabilities(
            factory,
            ota,
            rollback_enabled=rollback_enabled_from_build(project / ".esphome" / "build"),
        )
        (output / f"{values['artifact_name']}.overlay.yaml").write_text(overlay)
    if sha256(source) != before:
        raise RuntimeError("Source YAML changed during build")
    metadata = {
        "builder": "esp-anywhere-v03-overlay-poc",
        "esphome_version": version,
        "external_component_version": COMPONENT_VERSION,
        "source": source.name,
        "source_sha256": before,
        **target,
        "transport": "claim-https+wss-worker-v0.3",
        "mqtt": False,
        "registry": "codegen explicit entity table",
        "supported_entity_domains": supported_domains,
        "unsupported_entities": unsupported_entities,
        "ota_capabilities": capabilities.metadata(),
        "artifacts": {
            "factory": {"file": factory.name, "size": factory.stat().st_size, "sha256": sha256(factory)},
            "ota": {"file": ota.name, "size": ota.stat().st_size, "sha256": sha256(ota)},
        },
        "compile_summary": [line for line in compile_output.splitlines() if "RAM:" in line or "Flash:" in line][-4:],
    }
    (output / f"{values['artifact_name']}.build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    ota_manifest = build_signed_manifest(
        firmware=ota,
        firmware_url=values["ota_firmware_url"],
        version=values["firmware_version"],
        build_id=values["build_id"],
        chip_family=target["chip_family"],
        capabilities=capabilities,
        private_key=Path(values["ota_private_key"]),
        key_id=values["ota_key_id"],
        channel=values["ota_channel"],
        recovery=values["ota_recovery"],
    )
    (output / f"{values['artifact_name']}.ota-manifest.json").write_bytes(ota_manifest)
    manifest = {"name": values["friendly_name"], "version": values["firmware_version"],
                "new_install_prompt_erase": False,
                "builds": [{"chipFamily": target["chip_family"], "parts": [{"path": factory.name, "offset": 0}]}]}
    (output / f"{values['artifact_name']}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--friendly-name", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--firmware-version", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--ota-private-key", type=Path, required=True)
    parser.add_argument("--ota-key-id", required=True)
    parser.add_argument("--ota-firmware-url", required=True)
    parser.add_argument("--ota-base-url", required=True)
    parser.add_argument("--ota-channel", choices=("stable", "beta", "recovery"), default="stable")
    parser.add_argument("--ota-recovery", action="store_true")
    parser.add_argument("--esphome", default="esphome")
    args = parser.parse_args()
    values = vars(args).copy()
    values.pop("source"); values.pop("output"); values.pop("esphome")
    metadata = build(args.source, args.output, values, args.esphome)
    for item in metadata["unsupported_entities"]:
        print(f"WARNING: {item['warning']} (count={item['count']}, internal={item['internal_count']})")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
