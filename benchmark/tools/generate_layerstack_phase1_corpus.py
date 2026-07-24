#!/usr/bin/env python3
"""Generate the immutable Stage00 v1 LayerStack tiny corpus."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "fixtures/layerstack-phase1-v1"
SEED = 20260724
MIB = 1024 * 1024
ALPHABET = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def deterministic_ascii(label: str, size: int) -> bytes:
    result = bytearray()
    counter = 0
    prefix = f"eos-stage00:{SEED}:{label}:".encode()
    while len(result) < size:
        digest = hashlib.sha256(prefix + counter.to_bytes(8, "big")).digest()
        result.extend(ALPHABET[value % len(ALPHABET)] for value in digest)
        counter += 1
    return bytes(result[:size])


def sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def write_member(name: str, content: bytes) -> dict[str, str | int]:
    compressed = gzip.compress(content, compresslevel=9, mtime=0)
    path = ROOT / f"{name}.gz"
    path.write_bytes(compressed)
    return {
        "path": path.name,
        "gzip_sha256": sha256(compressed),
        "content_sha256": sha256(content),
        "content_bytes": len(content),
    }


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    marker_offset = MIB // 2
    marker_bytes = 1024
    localized_base = bytearray(b"a" * MIB)
    localized_base[marker_offset : marker_offset + marker_bytes] = b"B" * marker_bytes
    history_payloads = [
        deterministic_ascii(f"history-{revision:02d}", 4096).decode()
        for revision in range(32)
    ]
    members = {
        "localized_base": bytes(localized_base),
        "incompressible": deterministic_ascii("incompressible", MIB),
        "small_source": deterministic_ascii("small-source", MIB),
        "history_payloads": json.dumps(
            history_payloads, sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    manifest = {
        "schema_version": 1,
        "corpus_version": "v1",
        "seed": SEED,
        "localized_marker_offset": marker_offset,
        "localized_marker_bytes": marker_bytes,
        "small_file_count": 256,
        "small_file_bytes": 4096,
        "history_depths": [1, 8, 32],
        "files": {name: write_member(name, content) for name, content in members.items()},
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
