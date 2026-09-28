#!/usr/bin/env python3
"""Import translations and rebuild a ROM.

Filling in a `translation` column of data/translation.tsv and
running this tool rewrites the affected FAT entries:

  * the string pool is rebuilt from the translated strings (lengths change),
  * the entry's bytecode stays put; the offset table only has to be rewritten
    when the table no longer sits immediately before the pool,
  * each entry is re-compressed with the game's LZ77 and written back; if the
    new compressed block does not fit, it is appended and the FAT is repointed.

Usage:
  import_script.py --master data/translation.tsv \
                   --rom "Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba" \
                   --out out.gba [--report-only]
"""
import argparse
import time
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g
import export_script as ex
import lz77

TEXTDIR = os.path.join(ROOT, 'data')


APPEND_GUARD = 32          # zero bytes after each appended entry


def load_maps(path=None):
    import mapio
    return mapio.load_map(path or os.path.join(TEXTDIR, 'glyph_map.csv')), None


def reverse_map(m, kind='jp'):
    """character -> the code to write for it (inverts the ROM's decode rule).

    Verified against the JP ROM bitmaps and script codes:
        index 0x08B = を  and the script writes 0x08C for を
        index 0x020 = ×   and the script writes 0x021 for ×
    so  index = code - 1  (code >= 0x20), i.e.  code = index + 1.
    Codes below 0x20 are written directly (code = index). The engine draws
    index = code - 1 for EVERY code, so that low-code branch is one glyph off -
    exactly matching jp_decode() in export_script.py. Keep both sides in sync;
    changing only one shifts all punctuation/digits. See docs/技术说明.md
    section 2 and section 10 item 2.

    A few characters exist at two indices; prefer the higher index because that
    is the one the game's text actually uses for the dakuten-form glyphs.
    """
    rev = {}
    for idx, ch in sorted(m.items(), reverse=True):
        if not ch:
            continue
        code = idx if (kind != 'jp' or idx < 0x20) else idx + 1
        if code >= 0:
            rev[ch] = code
    return rev


def encode_text(text, rev):
    """Encode a translated string to u16 codes. Returns (codes, missing)."""
    codes = []
    missing = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\n':
            codes.append(0x0000 if kind == 'jp' else 0x000D)
            i += 1
            continue
        if ch == '\\' and i + 6 <= len(text) and text[i + 1] == 'x':
            try:
                codes.append(int(text[i + 2:i + 6], 16))
                i += 6
                continue
            except ValueError:
                pass
        code = rev.get(ch)
        if code is None:
            missing.append(ch)
            code = 0x0000   # placeholder; caller reports
        codes.append(code)
        i += 1
    return codes, missing


def rebuild_entry(d, translations, rev, kind):
    """Rewrite strings in place, preserving the bytecode, offset table and every
    offset value.  A translation that does not fit the original slot is
    reported and skipped (padding fills the remainder).

    translations: {string_index: new_text}
    """
    info = ex.analyse(d)
    if not info:
        return None, 'no script structure', []
    toff, base = info['table_off'], info['base']
    vals = []
    p = toff
    while p + 4 <= len(d):
        x = struct.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d):
            break
        if vals and x <= vals[-1]:
            break
        vals.append(x)
        p += 4
    out = bytearray(d)
    missing_all = []
    too_long = []
    for idx, text in sorted(translations.items()):
        if idx >= len(vals):
            too_long.append((idx, 'no such string slot'))
            continue
        off = base + vals[idx]
        if off + 2 > len(d):
            too_long.append((idx, 'offset out of range'))
            continue
        # original slot: codes plus the 0x0000 terminator
        n = 0
        while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
            n += 1
        slot_words = n + 1
        new_codes, miss = encode_text(text, rev)
        missing_all += miss
        if any(c == 0 for c in new_codes):
            too_long.append((idx, 'contains unmapped character'))
            continue
        if len(new_codes) + 1 > slot_words:
            too_long.append((idx, f'needs {len(new_codes) + 1} words, slot has {slot_words}'))
            continue
        # write codes, then pad with 0x0000 to the end of the slot
        pos = off
        for c in new_codes:
            struct.pack_into('<H', out, pos, c)
            pos += 2
        while pos < off + 2 * slot_words:
            struct.pack_into('<H', out, pos, 0)
            pos += 2
    if len(out) != len(d):
        return None, 'internal error: size changed', []
    return bytes(out), None, too_long


def rebuild_pool_entry(d, items, rev):
    """Rewrite NUL-terminated strings that live in an entry's string pool
    (entries with no offset table, see tools/extract_pool.py).

    items: [(absolute_offset, text, slot_words)].  Each string is written back
    to its own offset and padded with 0x0000 to the end of its original slot,
    so every reference the bytecode holds stays valid.
    """
    out = bytearray(d)
    too_long = []
    for off, text, slot in items:
        new_codes, miss = encode_text(text, rev)
        if any(c == 0 for c in new_codes):
            too_long.append((off, 'contains unmapped character'))
            continue
        if len(new_codes) + 1 > slot:
            too_long.append((off, f'needs {len(new_codes) + 1} words, slot has {slot}'))
            continue
        pos = off
        for c in new_codes:
            struct.pack_into('<H', out, pos, c)
            pos += 2
        while pos < off + 2 * slot:
            struct.pack_into('<H', out, pos, 0)
            pos += 2
    return bytes(out), None, too_long


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(TEXTDIR, 'translation.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--out', default='')
    ap.add_argument('--report-only', action='store_true')
    ap.add_argument('--map', default='', help='glyph map CSV (default data/glyph_map.csv)')
    a = ap.parse_args()
    jp_map, _ = load_maps(a.map or None)
    kind = g.which(a.rom)
    rev = reverse_map(jp_map, kind)
    rows = list(csv.DictReader(open(a.master, encoding='utf-8'), delimiter='\t',
                               restkey='extra'))
    todo = {}
    pool = {}
    empty = 0
    for r in rows:
        tr = (r.get('translation') or '').strip()
        if not tr:
            empty += 1
            continue
        eid = int(r['entry'])
        if (r.get('extra') or [''])[0] == 'pool':
            pool.setdefault(eid, []).append(
                (int(r['offset']), tr, int(r['n_codes']) + 1))
        else:
            todo.setdefault(eid, {})[int(r['idx'])] = tr
    print(f'master rows: {len(rows)}; with translation: {len(rows) - empty}; '
          f'entries touched: {len(todo)} table + {len(pool)} pool')
    if not todo and not pool:
        print('nothing to import (translation column empty)')
        return
    rom = bytearray(open(a.rom, 'rb').read())
    # Free space per entry = the distance to the next FAT entry in file order.
    # Some code paths read an entry from its ORIGINAL hard-coded ROM address
    # instead of through the FAT (e.g. the in-game status bar's scene name),
    # so an entry that has to grow should still be rewritten *in place* when the
    # following gap can take it - otherwise the hard-coded reader keeps seeing
    # the old (Japanese) bytes.
    _ents = []
    for _e in range(1500):
        _o, _s = struct.unpack_from('<2I', rom, g.FAT + _e * 8)
        if _o or _s:
            _ents.append((_o, _e))
    _ents.sort()
    nxt_off = {_e: (_ents[i + 1][0] if i + 1 < len(_ents) else None)
               for i, (_o, _e) in enumerate(_ents)}
    nfit = napp = 0
    miss_total = 0
    eids = sorted(set(todo) | set(pool))
    t_start = time.time()
    print(f'importing {len(eids)} entries (table rows for {len(todo)}, '
          f'pool rows for {len(pool)}); progress below', flush=True)
    for n_done, eid in enumerate(eids, 1):
        t_entry = time.time()
        d = g.load_entry(bytes(rom), eid)
        if d is None:
            print(f'  [{n_done}/{len(eids)}] e{eid:04d}: missing'); continue
        nd, too_long = d, []
        if eid in todo:
            nd, err, tl = rebuild_entry(nd, todo[eid], rev, kind)
            if nd is None:
                print(f'  [{n_done}/{len(eids)}] e{eid:04d}: {err}'); continue
            too_long += tl
        if eid in pool:
            nd, err, tl = rebuild_pool_entry(nd, pool[eid], rev)
            if nd is None:
                print(f'  [{n_done}/{len(eids)}] e{eid:04d}: {err}'); continue
            too_long += tl
        n_skip = len(too_long)
        enc = lz77.compress(nd)
        off, size = struct.unpack_from('<2I', rom, g.FAT + eid * 8)
        if len(enc) <= size:
            where = 'in place'
        else:
            where = 'appended'
        if n_skip:
            print(f'  [{n_done}/{len(eids)}] e{eid:04d}: {n_skip} strings skipped, '
                  f'e.g. {too_long[:2]}')
        gap = None
        if nxt_off.get(eid) is not None:
            gap = nxt_off[eid] - off          # bytes before the next entry
        if len(enc) <= size or (gap is not None and len(enc) <= gap):
            rom[g.BASE + off:g.BASE + off + len(enc)] = enc
            struct.pack_into('<II', rom, g.FAT + eid * 8, off, len(enc))
            nfit += 1
        else:
            # The game decompresses entries with the BIOS LZ77 SWI, whose
            # source pointer must be **word aligned**.  The original ROM keeps
            # every entry aligned (the ones that are not are never passed to
            # the SWI); appending at an arbitrary offset silently broke that
            # contract - e.g. the scene-name table e417 ended up at 0xBEC5D6
            # (mod 4 == 2), the game failed to decompress it, the notebook /
            # save-screen name line then drew an uninitialised VRAM buffer and
            # showed the "flower" garble.
            while len(rom) % 4:
                rom.append(0)
            new_off = len(rom) - g.BASE
            rom += enc
            # The game reads a little past the end of an entry's compressed
            # data.  In the JP ROM the next entry's bytes sit there (harmless);
            # for an appended entry it would be our next entry's data, and a
            # stray read out of that killed the game (EWRAM wiped to zero,
            # DISPCNT forced blank).  A small zero guard keeps it benign.
            rom += b'\x00' * APPEND_GUARD
            struct.pack_into('<II', rom, g.FAT + eid * 8, new_off, len(enc))
            napp += 1
        dt = time.time() - t_entry
        detail = f'{len(d):6d} -> {len(enc):6d} bytes {where:8s}'
        if n_skip:
            detail += f' ({n_skip} skipped)'
        slow = '  <-- slow' if dt > 2.0 else ''
        print(f'  [{n_done:3d}/{len(eids)}] e{eid:04d} {detail} '
              f'{dt:5.2f}s total {time.time() - t_start:6.1f}s{slow}', flush=True)
    print(f'entries patched in place: {nfit}; appended: {napp}; unmapped chars: {miss_total}; '
          f'{time.time() - t_start:.1f}s')
    if a.report_only:
        return
    if not a.out:
        raise SystemExit('--out is required unless --report-only')
    open(a.out, 'wb').write(rom)
    print(f'wrote {a.out} ({len(rom)} bytes)')


if __name__ == '__main__':
    main()
