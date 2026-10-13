"""Legacy BEC configuration files (``.bec``).

A BEC file is XML: ``<bec fileversion="2" configId="99900-0001-0000-01" configName="Config A"
customerKeySignature="…"><block content="01 89 …" version="2"/>…</bec>``. Every block is sent
to the reader with ``Sys.CfgLoadBlock(Version, Data)``; the first block carries version 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .errors import BecFormatError

CONFIG_ID_RE = re.compile(r"^\d{5}-\d{4}-\d{4}-(\d{2}|DRAFT)$")


@dataclass(frozen=True)
class BecBlock:
    version: int
    data: bytes


@dataclass(frozen=True)
class BecFile:
    path: Path
    file_version: int
    config_id: str
    config_name: str
    customer_key_signature: int | None
    blocks: tuple[BecBlock, ...]

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def payload_size(self) -> int:
        return sum(len(b.data) for b in self.blocks)

    @property
    def is_draft(self) -> bool:
        return self.config_id.endswith("DRAFT")

    def matches(self, reader_config_id: str | None) -> bool:
        """True when the reader already reports exactly this configuration (id incl. version)."""
        return reader_config_id is not None and reader_config_id.strip() == self.config_id


def load_bec(path: str | Path) -> BecFile:
    path = Path(path)
    if not path.is_file():
        raise BecFormatError(f"configuration file not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise BecFormatError(f"{path.name}: invalid XML: {e}") from None
    if root.tag != "bec":
        raise BecFormatError(f"{path.name}: root element must be <bec>, found <{root.tag}>")

    file_version_text = root.attrib.get("fileversion", "")
    if file_version_text not in ("1", "2"):
        raise BecFormatError(f"{path.name}: unsupported fileversion {file_version_text!r}")
    file_version = int(file_version_text)
    if file_version == 1:
        config_id, config_name = "99999-9999-9999-99", "unknown"
    else:
        config_id = root.attrib.get("configId", "").strip()
        config_name = root.attrib.get("configName", "").strip()
        if not config_id or not config_name:
            raise BecFormatError(f"{path.name}: fileversion 2 requires configId and configName")
        if not CONFIG_ID_RE.match(config_id):
            raise BecFormatError(f"{path.name}: configId {config_id!r} has not the form 12345-1234-1234-12")

    signature: int | None = None
    signature_text = root.attrib.get("customerKeySignature")
    if signature_text is not None:
        try:
            signature = int(signature_text)
        except ValueError:
            raise BecFormatError(f"{path.name}: customerKeySignature must be an integer") from None

    blocks: list[BecBlock] = []
    for i, node in enumerate(root.findall("block"), start=1):
        content = node.attrib.get("content", "")
        try:
            data = bytes.fromhex("".join(content.split()))
        except ValueError:
            raise BecFormatError(f"{path.name}: block {i} has invalid hex content") from None
        if not data:
            raise BecFormatError(f"{path.name}: block {i} is empty")
        version_text = node.attrib.get("version", "0").strip()
        if not version_text.isdigit():
            raise BecFormatError(f"{path.name}: block {i} has invalid version {version_text!r}")
        blocks.append(BecBlock(int(version_text), data))
    if not blocks:
        raise BecFormatError(f"{path.name}: no <block> elements")
    return BecFile(path, file_version, config_id, config_name, signature, tuple(blocks))
