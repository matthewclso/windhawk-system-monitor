"""Synthetic Chromium cache fixtures; never reads real account/profile data."""

import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

DECODER = Path(__file__).resolve().parents[1] / "src/claude-reset-cache.cjs"


@unittest.skipUnless(shutil.which("node"), "Node needed for cache decoding tests")
class CacheReader(unittest.TestCase):
    def decode(
        self,
        organization="active",
        entry_organization="active",
        committed=True,
        compressed=False,
        surface=False,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "Cache/Cache_Data"
            cache.mkdir(parents=True)
            url = f"https://claude.ai/api/organizations/{entry_organization}/usage?cedar_ember=1"
            data = bytearray(8192 + 256)
            offset = 8192
            struct.pack_into("<I", data, offset + 20, 0 if committed else 1)
            key = url.encode()
            struct.pack_into("<I", data, offset + 32, len(key))
            data[offset + 96 : offset + 96 + len(key)] = key
            header = b"HTTP/1.1 200 OK\0date: Sat, 03 Oct 2026 12:00:00 GMT\0"
            body = json.dumps(
                {
                    "cedar_ember": {
                        "ineligible_reason": "surface" if surface else None,
                        "grants": [
                            {
                                "resets_left": 2,
                                "ends_at": "2030-01-01T00:00:00Z",
                                "secretId": "must-not-leave-decoder",
                            }
                        ],
                    }
                }
            ).encode()
            if compressed:
                body = subprocess.run(
                    [
                        "node",
                        "-e",
                        "process.stdout.write(require('node:zlib').zstdCompressSync(require('node:fs').readFileSync(0)))",
                    ],
                    input=body,
                    capture_output=True,
                    check=True,
                ).stdout
            struct.pack_into("<IIII", data, offset + 40, len(header), len(body), 0, 0)
            struct.pack_into("<II", data, offset + 56, 0x80000001, 0x80000002)
            (cache / "data_1").write_bytes(data)
            (cache / "f_000001").write_bytes(header)
            (cache / "f_000002").write_bytes(body)
            result = subprocess.run(
                ["node", str(DECODER)],
                input=json.dumps({"profile": temporary, "organization": organization}),
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)

    def test_active_organization_only_and_sanitized_fields(self):
        result = self.decode()
        self.assertEqual(result["grants"][0]["resets_left"], 2)
        self.assertNotIn("secretId", result["grants"][0])
        self.assertNotIn("organization", result)

    def test_other_organization_is_not_returned(self):
        self.assertIsNone(self.decode(entry_organization="other"))

    def test_uncommitted_entry_is_ignored(self):
        self.assertIsNone(self.decode(committed=False))

    def test_surface_ineligible_is_not_zero_resets(self):
        self.assertIsNone(self.decode(surface=True))

    def test_zstd_response_is_decoded(self):
        self.assertEqual(self.decode(compressed=True)["grants"][0]["resets_left"], 2)
