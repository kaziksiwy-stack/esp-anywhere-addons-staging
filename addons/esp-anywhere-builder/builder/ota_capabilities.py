"""Derive secure OTA capabilities from an actual ESPHome build."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import struct

PARTITION_MAGIC = 0x50AA
PARTITION_ENTRY = struct.Struct("<HBBII16sI")
TYPE_APP = 0x00
TYPE_DATA = 0x01
SUBTYPE_OTA_DATA = 0x00
OTA_APP_SUBTYPE_MIN = 0x10
OTA_APP_SUBTYPE_MAX = 0x1F


@dataclass(frozen=True)
class Partition:
    type: int
    subtype: int
    offset: int
    size: int
    label: str


@dataclass(frozen=True)
class OtaCapabilities:
    tier: str
    app_slot_count: int
    app_slot_size: int
    has_otadata: bool
    automatic_rollback: bool
    image_fits: bool
    layout_sha256: str
    partitions: tuple[Partition, ...]

    def metadata(self) -> dict[str, object]:
        value = asdict(self)
        value["partitions"] = [asdict(item) for item in self.partitions]
        return value


def _candidate_table(data: bytes, start: int) -> tuple[Partition, ...]:
    entries: list[Partition] = []
    cursor = start
    while cursor + PARTITION_ENTRY.size <= len(data):
        magic, part_type, subtype, offset, size, raw_label, _flags = PARTITION_ENTRY.unpack_from(data, cursor)
        if magic != PARTITION_MAGIC:
            break
        label = raw_label.split(b"\0", 1)[0].decode("ascii", errors="strict")
        if not label or size <= 0 or offset <= 0:
            return ()
        entries.append(Partition(part_type, subtype, offset, size, label))
        cursor += PARTITION_ENTRY.size
    return tuple(entries) if len(entries) >= 2 else ()


def read_partition_table(factory_image: Path) -> tuple[Partition, ...]:
    """Find and parse the partition table without assuming its flash offset."""
    data = factory_image.read_bytes()
    marker = struct.pack("<H", PARTITION_MAGIC)
    start = 0
    while True:
        position = data.find(marker, start, min(len(data), 256 * 1024))
        if position < 0:
            raise ValueError("partition table not found in factory image")
        candidate = _candidate_table(data, position)
        if candidate and any(item.type == TYPE_APP for item in candidate):
            return candidate
        start = position + 1


def layout_descriptor(partitions: tuple[Partition, ...]) -> str:
    relevant = [
        item for item in partitions
        if (item.type == TYPE_APP and OTA_APP_SUBTYPE_MIN <= item.subtype <= OTA_APP_SUBTYPE_MAX)
        or (item.type == TYPE_DATA and item.subtype == SUBTYPE_OTA_DATA)
    ]
    relevant.sort(key=lambda item: (item.offset, item.type, item.subtype))
    return ";".join(
        f"{item.type:02x}:{item.subtype:02x}:{item.offset:08x}:{item.size:08x}" for item in relevant
    )


def detect_ota_capabilities(factory_image: Path, ota_image: Path, *, rollback_enabled: bool) -> OtaCapabilities:
    partitions = read_partition_table(factory_image)
    slots = tuple(
        item for item in partitions
        if item.type == TYPE_APP and OTA_APP_SUBTYPE_MIN <= item.subtype <= OTA_APP_SUBTYPE_MAX
    )
    has_otadata = any(item.type == TYPE_DATA and item.subtype == SUBTYPE_OTA_DATA for item in partitions)
    slot_size = min((item.size for item in slots), default=0)
    image_fits = bool(slot_size and ota_image.stat().st_size <= slot_size)
    if len(slots) >= 2 and has_otadata and image_fits:
        tier = "A" if rollback_enabled else "B"
    else:
        tier = "C"
    descriptor = layout_descriptor(partitions)
    return OtaCapabilities(
        tier=tier,
        app_slot_count=len(slots),
        app_slot_size=slot_size,
        has_otadata=has_otadata,
        automatic_rollback=tier == "A",
        image_fits=image_fits,
        layout_sha256=hashlib.sha256(descriptor.encode("ascii")).hexdigest(),
        partitions=partitions,
    )


def rollback_enabled_from_build(build_root: Path) -> bool:
    """Read the compiled SDK configuration, never the source board name."""
    candidates = list(build_root.rglob("sdkconfig*"))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        text = candidate.read_text(errors="ignore")
        if "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in text:
            return True
    return False
