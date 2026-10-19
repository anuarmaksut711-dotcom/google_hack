"""Reproduce literal v1 byte vectors using stdlib only, never the product codec.

Run from any directory with Python 3.12. No audio device or network is used.
The explicit hex below is the normative byte oracle, not a codec round-trip.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PCM_HEX = "0080ffff00000100ff7f3412cced2a00"
VECTORS = [
    {
        "file": "input-v1.bin", "kind": "input", "connection_generation": 16909060,
        "generation": 0, "sequence": 168496141, "sample_rate": 16000,
        "hex": "435056410101001801020304000000000a0b0c0d00000010" + PCM_HEX,
    },
    {
        "file": "output-v1.bin", "kind": "output", "connection_generation": 16909060,
        "generation": 287454020, "sequence": 168496141, "sample_rate": 24000,
        "hex": "435056410102001801020304112233440a0b0c0d00000010" + PCM_HEX,
    },
]


def generate():
    vectors = []
    for vector in VECTORS:
        data = bytes.fromhex(vector["hex"])
        (ROOT / vector["file"]).write_bytes(data)
        vectors.append({**vector, "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "version": "1", "header_bytes": 24, "header_byte_order": "big",
        "pcm_byte_order": "little", "pcm_hex": PCM_HEX,
        "samples": [-32768, -1, 0, 1, 32767, 4660, -4660, 42],
        "vectors": vectors,
    }
    (ROOT / "golden.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    generate()
