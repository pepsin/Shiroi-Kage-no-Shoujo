#!/usr/bin/env python3
"""Side-by-side compare of every layer of two savestates.

Renders each BG (with scroll, 4bpp/8bpp) and the OBJ layer from the gbAs chunk
so a JP reference state and a CN state can be lined up visually.

Usage:
  state_compare.py A.ss1 B.ss2 out.png
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_state as rs  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402


def layer_img(reg, name):
    if name.startswith('bg'):
        bg = int(name[2:])
        return rs.render_bg(reg, bg, None)
    return rs.render_obj(reg)


def to_img(grid):
    im = Image.new('RGB', (240, 160), (0, 0, 0))
    px = im.load()
    for y in range(160):
        for x in range(240):
            if grid[y][x]:
                px[x, y] = grid[y][x]
    return im


def main():
    a_path, b_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    ra, rb = rs.read_state(a_path), rs.read_state(b_path)
    names = ['bg0', 'bg1', 'bg2', 'bg3', 'obj']
    scale = 2
    cell_w, cell_h = 241, 161
    img = Image.new('RGB', (cell_w * len(names) * scale, cell_h * 2 * scale), (30, 30, 30))
    d = ImageDraw.Draw(img)
    for col, name in enumerate(names):
        for row, reg in enumerate((ra, rb)):
            im = to_img(layer_img(reg, name))
            im = im.resize((240 * scale, 160 * scale), Image.NEAREST)
            img.paste(im, (col * cell_w * scale, row * cell_h * scale))
            d.text((col * cell_w * scale + 4, row * cell_h * scale + 2),
                   f'{"A" if row == 0 else "B"} {name}'.replace('A ', a_path.split(".")[-1] + " ")
                   if False else name, fill=(255, 255, 0))
    for col in range(len(names) + 1):
        d.line([(col * cell_w * scale, 0), (col * cell_w * scale, img.height)], fill=(90, 90, 90))
    d.line([(0, cell_h * scale), (img.width, cell_h * scale)], fill=(90, 90, 90))
    img.save(out)
    print(f'{out} {img.size}')


if __name__ == '__main__':
    main()
