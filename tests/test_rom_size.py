"""Cartridge padding must preserve all resources and their zero guards."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from rom_size import ROM_SIZE, pad_rom


class RomSizeTests(unittest.TestCase):
    def test_preserves_payload_and_zero_guard_and_is_idempotent(self):
        payload = bytes(range(256)) * 33 + bytes(32)
        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / 'test.gba'
            rom.write_bytes(payload)
            self.assertEqual(pad_rom(rom), ROM_SIZE - len(payload))
            result = rom.read_bytes()
            self.assertEqual(len(result), ROM_SIZE)
            self.assertEqual(result[:len(payload)], payload)
            self.assertEqual(result[len(payload):], b'\xff' * (ROM_SIZE - len(payload)))
            self.assertEqual(pad_rom(rom), 0)
            self.assertEqual(rom.read_bytes(), result)

    def test_rejects_empty_and_oversized_images_without_modifying_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / 'test.gba'
            for size in (0, ROM_SIZE + 1):
                with rom.open('wb') as f:
                    f.truncate(size)
                before = rom.read_bytes()
                with self.assertRaises(ValueError):
                    pad_rom(rom)
                self.assertEqual(rom.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
