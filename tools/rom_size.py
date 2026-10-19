"""Final cartridge size for the expanded Chinese ROM.

gpSP's ROM mirroring can overlap EEPROM when the image is not a power of
two. The current layout fits in 16 MiB. Pad only after every resource has
been appended; never move data or replace the zero guards around resources.
"""
from pathlib import Path


ROM_SIZE = 16 * 1024 * 1024


def pad_rom(path):
    """Append erased-ROM bytes to 16 MiB; return the number added.

    Refuse oversized images instead of truncating them or silently changing
    the cartridge/EEPROM address layout to a 32 MiB cartridge.
    """
    path = Path(path)
    with path.open('r+b') as rom:
        size = rom.seek(0, 2)
        if not 0 < size <= ROM_SIZE:
            raise ValueError(f'ROM 大小 {size} 不在支持范围 1..{ROM_SIZE} 字节内')
        padding = ROM_SIZE - size
        rom.write(b'\xff' * padding)
    return padding
