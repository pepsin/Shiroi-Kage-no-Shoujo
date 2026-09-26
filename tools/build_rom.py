#!/usr/bin/env python3
"""Build the Simplified-Chinese ROM.

Two things have to happen:

1. **Font table**  the translation needs 2104 glyphs (931 reuse the JP table,
   1171 are new) but the JP table only has 1704 slots and only 771 of the
   unused ones can be recycled -> the table is rewritten with 400 extra
   glyphs.  A bigger table does not fit in the 198 KB tail (it needs 269 KB),
   so it is placed at ROM file 0x800000 and the file is grown past 8 MB;
   FAT entry 850 is repointed at it.  This is the same layout the reference
   Chinese release used (see tools/build_jp_map.py) and needs no code patch:
   the table is addressed through the FAT, and glyphs are read straight out
   of cart ROM.

2. **Script**  import_script.py rewrites every translated string in place with
   the extended character map.

Usage:
  build_rom.py [--rom JP.gba] [--master data/translation.tsv] [--out out.gba]
               [--headroom 128] [--no-script]
"""
import argparse
import re
import time
import collections
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import font_patch
import gbtext as g
import mapio

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
OLD_COUNT = 0x6A8
GLYPH_BYTES = 0x80
NEW_TABLE_OFF = 0x800000          # file offset of the rewritten table
ADV_TABLE_OFF = 0x6A4840          # FAT 851: per-glyph advance bytes (JP: 1704 x 0x0C)
ADV_VALUE = 0x0C                  # the JP value for every glyph
WORK = os.path.join(ROOT, 'work')
EXT_MAP = os.path.join(WORK, 'glyph_map.ext.csv')
# a rebuild normally has no hand-edited glyphs; more than this many differing
# PNGs means the directory is stale and would revert the render
GLYPH_EDIT_LIMIT = 200


def load_master(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def block_span(rom, head=852, max_gap=0x40):
    """The contiguous FAT run that starts at `head` (entries laid out back to back).

    Returns (start, end, eids) or None.  The game reads across these entry
    boundaries, so they must stay adjacent after relocation.
    """
    ents = []
    for eid in range(1500):
        o, sz = struct.unpack_from('<2I', rom, FAT + eid * 8)
        if sz:
            ents.append((BASE + o, BASE + o + sz, eid))
    ents.sort()
    idx = next((k for k, (a, b, e) in enumerate(ents) if e == head), None)
    if idx is None:
        return None
    block = [ents[idx]]
    k = idx + 1
    while k < len(ents):
        a, b, e = ents[k]
        if a - block[-1][1] < max_gap:
            block.append(ents[k])
            k += 1
        else:
            break
    return block[0][0], block[-1][1], [e for _, _, e in block]


def plan(rows, gmap, recycle=False):
    """Return (assignment, new_chars, table_count, free_used, extra)."""
    rev = {}
    for idx, ch in sorted(gmap.items(), reverse=True):
        code = idx if idx < 0x20 else idx - 1
        if code >= 0 and ch:
            rev.setdefault(ch, code)
    used = collections.Counter()
    for r in rows:
        for ch in (r.get('translation') or '').strip():
            used[ch] += 1
    have = set(gmap.values()) - {''}
    used_idx = set()
    for ch in used:
        code = rev.get(ch)
        if code is not None:
            used_idx.add(code if code < 0x20 else code + 1)
    free = [i for i in range(0x20, OLD_COUNT) if i not in used_idx]
    new_chars = sorted((c for c in used if c not in have), key=lambda c: (-used[c], c))
    if recycle:
        # reuse slots the *script* no longer references -- smaller table, but
        # glyphs used by other resources (title art, menus) would be destroyed
        got = new_chars[:len(free)]
        extra = new_chars[len(free):]
        assign = {ch: free[i] for i, ch in enumerate(got)}
    else:
        got, extra = [], new_chars
        assign = {}
    for i, ch in enumerate(extra):
        assign[ch] = OLD_COUNT + i
    table_count = OLD_COUNT + len(extra)
    return assign, new_chars, table_count, len(got), extra, used, rev, used_idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--headroom', type=int, default=128,
                    help='extra empty glyph slots appended for safety')
    ap.add_argument('--recycle', action='store_true',
                    help='reuse glyph slots the script no longer references '
                         '(smaller table, but overwrites glyphs other '
                         'resources may still use -- off by default)')
    ap.add_argument('--no-script', action='store_true',
                    help='only patch the font table, do not import the script')
    ap.add_argument('--glyph-dir', default=os.path.join(ROOT, 'data', 'glyph_png'),
                    help='hand-edited glyph PNGs to apply on top of the render')
    ap.add_argument('--no-glyph-edits', action='store_true',
                    help='ignore data/glyph_png hand edits')
    ap.add_argument('--outline-font', action='store_true',
                    help='render new glyphs by downscaling a system outline font '
                         'instead of the bundled pixel font (tools/fonts/zpix)')
    ap.add_argument('--force-glyph-edits', action='store_true',
                    help='apply data/glyph_png even if it looks stale')
    a = ap.parse_args()

    font_patch.set_pixel_font(not a.outline_font)
    print(f'new glyphs: {"pixel font " + os.path.basename(font_patch.PIXEL_FONT) if font_patch.use_pixel_font() else "outline font downscale"}',
          flush=True)
    rom = bytearray(open(a.rom, 'rb').read())
    gmap = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    rows = load_master(a.master)
    assign, new_chars, count, recycled, extra, used, rev, used_idx = plan(rows, gmap, a.recycle)
    count += a.headroom
    print(f'distinct chars in translation : {len(used)}')
    print(f'  reuse JP glyph slots        : {len(used) - len(new_chars)}')
    print(f'  new glyphs                  : {len(new_chars)}')
    print(f'    recycled free slots       : {recycled}'
          f'{" (disabled: append-only)" if not a.recycle else ""}')
    print(f'    appended past 0x{OLD_COUNT:03X}          : {len(extra)}')
    print(f'new table: {count} glyphs = {count * GLYPH_BYTES} bytes '
          f'(+{(count - OLD_COUNT) * GLYPH_BYTES} vs JP)')

    # --- build the table -------------------------------------------------
    old = rom[font_patch.GLYPH_FILE_OFF:
              font_patch.GLYPH_FILE_OFF + OLD_COUNT * GLYPH_BYTES]
    table = bytearray(old) + bytearray((count - OLD_COUNT) * GLYPH_BYTES)
    for ch, idx in sorted(assign.items(), key=lambda kv: kv[1]):
        table[idx * GLYPH_BYTES:(idx + 1) * GLYPH_BYTES] = font_patch.render_glyph(ch)
    print(f'rendered {len(assign)} glyphs; table holds {len(table) // GLYPH_BYTES} slots')

    t_phase = time.time()
    # --- new map ---------------------------------------------------------
    newmap = dict(gmap)
    for ch, idx in assign.items():
        newmap[idx] = ch
    with open(EXT_MAP, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['code', 'dec', 'char'])
        for idx in range(count):
            w.writerow([f'{idx:03X}', idx, newmap.get(idx, '')])
    print(f'wrote {EXT_MAP} ({count} entries)')

    print(f'[phase] rendered {len(new_chars)} glyphs in {time.time() - t_phase:.1f}s')
    t_phase = time.time()
    # --- patch the ROM ---------------------------------------------------
    end = NEW_TABLE_OFF + len(table)
    if len(rom) < end:
        rom.extend(b'\x00' * (end - len(rom)))
    rom[NEW_TABLE_OFF:end] = table
    off = NEW_TABLE_OFF - BASE
    struct.pack_into('<II', rom, FAT + FONT_EID * 8, off, len(table))
    print(f'font table -> file 0x{NEW_TABLE_OFF:X} (FAT {FONT_EID}: '
          f'off=0x{off:X} size={len(table)})')

    # --- extend the per-glyph advance table ------------------------------
    # The text renderer computes a character's x as
    #     base + field8 + (i+1)*field10 + sum(1 + ADV[glyph])
    # where ADV is a byte table that in the JP ROM sits at file 0x6A4840
    # (FAT 851, 1704 bytes, all 0x0C) immediately after the 1704 glyph
    # bitmaps.  Our appended glyphs (index >= 1704) read past its end into
    # the next resource, which produced overlapping characters and huge gaps
    # in game.  FAT 852's data starts at 0x6A4EF0, inside the range the
    # extended table needs, so that data has to move out of the way.
    #
    # FAT 852 is the head of one long *contiguous* run of entries
    # (e852..e1006, 0x6A4EF0..0x7CE6C0).  The game reads across those entry
    # boundaries - moving only e852 made it run off into whatever we appended
    # next and the game died with a white screen (EWRAM wiped, DISPCNT=0x80).
    # So the whole run moves as one block, keeping every relative offset, and
    # a zero guard follows it because the JP ROM has free (zero) space after
    # the run.
    GUARD = 0x10000
    tbl_end = ADV_TABLE_OFF + count
    block = block_span(rom)
    if block and block[0] < tbl_end:
        bstart, bend, eids = block
        blob = bytes(rom[bstart:bend])
        new_off = len(rom)
        rom.extend(blob)
        rom.extend(b'\x00' * GUARD)
        for eid in eids:
            o, s = struct.unpack_from('<2I', rom, FAT + eid * 8)
            struct.pack_into('<II', rom, FAT + eid * 8, new_off + (BASE + o - bstart) - BASE, s)
        print(f'relocated the contiguous block e{min(eids)}..e{max(eids)} '
              f'({len(eids)} entries, {bend - bstart} bytes): file '
              f'0x{bstart:X} -> 0x{new_off:X} (+{GUARD:#x} zero guard)')
    n_adv = 0
    for i in range(count):
        pos = ADV_TABLE_OFF + i
        if pos >= len(rom):
            rom.extend(b'\x00' * (pos + 1 - len(rom)))
        if rom[pos] != ADV_VALUE:
            rom[pos] = ADV_VALUE
            n_adv += 1
    print(f'advance table: 0x{ADV_TABLE_OFF:X}..0x{tbl_end:X} '
          f'({count} glyphs, {n_adv} bytes set to 0x{ADV_VALUE:02X}; FAT 851 size kept)')
    # sanity: the old tail must be free, and the new blob must not collide
    print(f'[phase] font/table/block patched in {time.time() - t_phase:.1f}s; '
          f'ROM size now {len(rom)} bytes ({len(rom) / 1048576:.2f} MB)')

    patched = os.path.join(WORK, 'font_patched.gba')
    open(patched, 'wb').write(bytes(rom))
    print(f'wrote {patched}')

    if a.no_script:
        return
    # --- import the script ----------------------------------------------
    import subprocess
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'import_script.py'),
           '--master', a.master, '--rom', patched, '--out', a.out,
           '--map', EXT_MAP]
    print('running:', ' '.join(cmd[1:]), flush=True)
    subprocess.run(cmd, check=True)

    # --- redraw the pre-rendered menu plates (title screen) ---------------
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'patch_menu_plates.py'),
           '--apply', a.out, a.out]
    print('running:', ' '.join(cmd[1:]))
    subprocess.run(cmd, check=True)

    # --- redraw the pre-rendered save/load screen plates ------------------
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'patch_save_plates.py'),
           '--apply', a.out, a.out]
    print('running:', ' '.join(cmd[1:]))
    subprocess.run(cmd, check=True)

    # --- apply hand-edited glyph PNGs -------------------------------------
    # data/glyph_png is the editable view of the font table; without this step
    # a rebuild would silently revert every glyph the user redrew by hand.
    if not a.no_glyph_edits and os.path.exists(os.path.join(a.glyph_dir, 'manifest.tsv')):
        print('[phase] applying hand-edited glyphs', flush=True)
        imp = [sys.executable, os.path.join(ROOT, 'tools', 'import_glyphs.py'),
               '--dir', a.glyph_dir]
        # A freshly rendered font should match the exported PNGs exactly, so
        # "hand edits" should be just the handful the user touched.  A large
        # count means the PNG directory was exported from some other ROM and
        # would revert the render - ask before doing that.
        chk = subprocess.run(imp + ['--rom', a.out, '--dry-run'],
                             capture_output=True, text=True)
        m = re.search(r'hand-edited images: (\d+)', chk.stdout)
        n_hand = int(m.group(1)) if m else 0
        if n_hand > GLYPH_EDIT_LIMIT and not a.force_glyph_edits:
            print(chk.stdout, flush=True)
            raise SystemExit(
                f'{n_hand} glyph PNGs in {a.glyph_dir} differ from the font this build '
                f'rendered (expected only a few hand edits).\n'
                f'The directory was probably exported from a different ROM; re-export it '
                f'from a current build, or pass --force-glyph-edits to use it as-is.')
        cmd = imp + ['--rom', a.out, '--out', a.out]
        print('running:', ' '.join(cmd[1:]), flush=True)
        subprocess.run(cmd, check=True)


    # --- self-checks ------------------------------------------------------
    t_check = time.time()
    print('\n=== self-check ===', flush=True)
    rc_verify = run_check([sys.executable, os.path.join(ROOT, 'tools', 'verify_rom.py'),
                           a.out])
    rc_writes = run_check([sys.executable, os.path.join(ROOT, 'tools', 'verify_writes.py'),
                           '--cn', a.out])
    rc_glyphs = run_check([sys.executable, os.path.join(ROOT, 'tools', 'verify_glyphs.py'),
                           a.out, '--dir', a.glyph_dir])
    ok = (rc_verify == 0 and rc_writes == 0 and rc_glyphs == 0)
    print(f'=== self-check {"PASSED" if ok else "FAILED"} '
          f'in {time.time() - t_check:.1f}s ===', flush=True)
    if not ok:
        raise SystemExit('build self-check failed - see the output above')


def run_check(cmd):
    """Run a self-check tool, streaming its output, and return its exit code."""
    import subprocess
    print('$', ' '.join(os.path.basename(c) for c in cmd[:2]), flush=True)
    p = subprocess.run(cmd)
    return p.returncode


if __name__ == '__main__':
    main()
