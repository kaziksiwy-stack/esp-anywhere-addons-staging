"""Build and verify deterministic Ed25519 secure OTA manifests."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ota_capabilities import OtaCapabilities

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
CHANNELS = {"stable", "beta", "recovery"}


def compact_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_signed_manifest(
    *, firmware: Path, firmware_url: str, version: str, build_id: str, chip_family: str,
    capabilities: OtaCapabilities, private_key: Path, key_id: str, channel: str,
    recovery: bool = False, min_external_component: str = "1.0.0", min_protocol: str = "1.0",
) -> bytes:
    if not SEMVER.fullmatch(version) or not SEMVER.fullmatch(min_external_component):
        raise ValueError("invalid semantic version")
    if channel not in CHANNELS or (recovery and channel != "recovery"):
        raise ValueError("invalid OTA channel/recovery policy")
    if not firmware_url.startswith("https://") or any(character in firmware_url for character in "?#@"):
        raise ValueError("firmware URL must be secret-free HTTPS")
    if capabilities.tier == "C" or not capabilities.image_fits:
        raise ValueError("build does not support remote secure OTA")
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("signing key must be Ed25519")
    image = firmware.read_bytes()
    payload = {
        "build_id": build_id,
        "channel": channel,
        "chip_family": chip_family,
        "compatibility": {
            "app_slot_count": capabilities.app_slot_count,
            "app_slot_size": capabilities.app_slot_size,
            "has_otadata": capabilities.has_otadata,
            "layout_sha256": capabilities.layout_sha256,
            "required_tier": capabilities.tier,
        },
        "downgrade_policy": "recovery_authorized" if recovery else "upgrade_only",
        "firmware": {
            "sha256": hashlib.sha256(image).hexdigest(),
            "size": len(image),
            "url": firmware_url,
        },
        "manifest_version": 2,
        "min_external_component_version": min_external_component,
        "min_protocol_version": min_protocol,
        "project": "esp-anywhere",
        "recovery": recovery,
        "version": version,
    }
    signed_payload = compact_json(payload)
    signature = key.sign(signed_payload)
    return compact_json({
        "schema_version": 2,
        "security": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "signature": base64.b64encode(signature).decode("ascii"),
        },
        "signed_payload": base64.b64encode(signed_payload).decode("ascii"),
    }) + b"\n"


def verify_signed_manifest(document: bytes, trusted_keys: dict[str, bytes]) -> dict[str, object]:
    """Host-side mirror used by differential tests and Builder validation."""
    outer = json.loads(document)
    if outer.get("schema_version") != 2 or outer.get("security", {}).get("algorithm") != "Ed25519":
        raise ValueError("manifest schema")
    key_id = outer["security"].get("key_id")
    if key_id not in trusted_keys:
        raise ValueError("untrusted key")
    try:
        payload = base64.b64decode(outer["signed_payload"], validate=True)
        signature = base64.b64decode(outer["security"]["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(trusted_keys[key_id]).verify(signature, payload)
    except (InvalidSignature, ValueError, TypeError) as error:
        raise ValueError("bad signature") from error
    result = json.loads(payload)
    if result.get("manifest_version") != 2 or result.get("project") != "esp-anywhere":
        raise ValueError("signed metadata")
    return result


def validate_manifest_compatibility(
    payload: dict[str, object], *, firmware: bytes, chip_family: str,
    capabilities: OtaCapabilities, current_version: str, recovery_requested: bool = False,
) -> None:
    """Host mirror of device policy for differential security tests."""
    compatibility = payload.get("compatibility", {})
    firmware_metadata = payload.get("firmware", {})
    if payload.get("chip_family") != chip_family or not isinstance(compatibility, dict):
        raise ValueError("incompatible target")
    expected = {
        "app_slot_count": capabilities.app_slot_count,
        "app_slot_size": capabilities.app_slot_size,
        "has_otadata": capabilities.has_otadata,
        "layout_sha256": capabilities.layout_sha256,
        "required_tier": capabilities.tier,
    }
    if any(compatibility.get(key) != value for key, value in expected.items()):
        raise ValueError("incompatible layout")
    if not isinstance(firmware_metadata, dict) or firmware_metadata.get("size") != len(firmware):
        raise ValueError("firmware size")
    if firmware_metadata.get("sha256") != hashlib.sha256(firmware).hexdigest():
        raise ValueError("firmware sha256")
    target = tuple(map(int, str(payload["version"]).split("-", 1)[0].split(".")))
    current = tuple(map(int, current_version.split("-", 1)[0].split(".")))
    authorized_recovery = (
        recovery_requested and payload.get("channel") == "recovery" and payload.get("recovery") is True
        and payload.get("downgrade_policy") == "recovery_authorized"
    )
    if target == current or (target < current and not authorized_recovery):
        raise ValueError("downgrade blocked")
