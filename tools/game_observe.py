"""Read visible sprite text from mGBA snapshots; never changes game state."""
from collections import defaultdict
import struct


def monochrome(data):
    """Highlighting recolors glyph pixels; compare their occupied pixel mask."""
    return bytes((0x10 if b & 0xf0 else 0) | (1 if b & 15 else 0) for b in data)


class SpriteReader:
    def __init__(self, font, glyph_map, rom=None):
        self.glyphs = {}
        self.commands = {}
        if rom is not None:
            labels = ('抽烟', '洋子', '春菜', '周围', '查看', '持有物',
                      '交谈', '移动', '推理', '搜索', '搭档')
            for bank in (0x66C400, 0x66DC40):
                for number, label in enumerate(labels):
                    offset = bank + number * 0x200
                    self.commands[rom[offset:offset+256]] = label
        for number in range(len(font) // 128):
            block = font[number * 128:(number + 1) * 128]
            tiles = [block[i*32:(i+1)*32] for i in range(4)]
            for order in ((0, 1, 2, 3), (0, 2, 1, 3)):
                pattern = monochrome(b''.join(tiles[i] for i in order))
                self.glyphs.setdefault(pattern, glyph_map.get(number, '?'))

    def rows(self, state):
        rows = defaultdict(list)
        for i in range(128):
            a, b, c, _ = struct.unpack_from('<4H', state['oam'], i*8)
            if a & 0x300 or ((a >> 14) & 3, (b >> 14) & 3) != (0, 1):
                continue
            x, y = b & 511, a & 255
            if x >= 240 or y >= 160:
                continue
            offset = 0x10000 + (c & 1023) * 32
            char = self.glyphs.get(monochrome(state['vram'][offset:offset+128]), '?')
            rows[y].append((x, char))
        return {y: sorted(row) for y, row in sorted(rows.items())}

    def text(self, state):
        return {y: ''.join(c for _, c in row) for y, row in self.rows(state).items()}

    def selected_command(self, state):
        # Highlighted 64x16 command plate comprises two 32x16 sprites.
        # Its left half is at x=4,y=60; other visible menu plates are gray.
        for i in range(128):
            a, b, c, _ = struct.unpack_from('<4H', state['oam'], i*8)
            if a & 0x300 or ((a >> 14) & 3, (b >> 14) & 3) != (1, 2):
                continue
            if (b & 511, a & 255) != (4, 60):
                continue
            offset = 0x10000 + (c & 1023) * 32
            return self.commands.get(state['vram'][offset:offset+256])
        return None
