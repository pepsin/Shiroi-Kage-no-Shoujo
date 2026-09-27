#!/usr/bin/env python3
"""Collect the translatable strings that carry raw control codes and add them.

Why they were missed
--------------------
`export_script.read_string` rejects any string containing a code above
GLYPH_MAX (0x6A8), and `extract_pool.scan_pools` stops at the first code it
cannot decode and only keeps hiragana-heavy runs.  Both filters exist because
bitmap data decodes to garbage.  The side effect is that *every* displayable
string built around a control code silently disappeared from the master:

  * 0xFFC2 / 0xFFC0  highlight brackets - the four-letter passwords painted on
    walls and paintings, and the labels of the character-file ("profile")
    screens (`氏名`, `職業`, `年齢`, `誕生日`, `血液型`),
  * 0xFFA2 + 0xFE03   the date/place/time "scene cards" shown when a scene
    starts,
  * 0xFE02 / 0xFE04   timing/position codes inside otherwise plain punctuation,
  * 0xFFF2 / 0xFFF3   the name-highlight pair (already handled for e314).

This tool walks each entry the way the game does - offset table plus
NUL-terminated runs - keeps every string that contains one of the *real*
control codes, drops the bitmap-noise signatures (`0xFFFF`, `0xFFFE`, `0xFF00`,
`0xFF01`, `0xFFF8`, `0xFFF0`, `0xFE9E`, `0xFE01`), and appends a master row for
each one that is not already covered.  Translations come from
`data/ctrl_translations.tsv` (`jp_text<TAB>translation`, control codes written
as `\\xXXXX`); a candidate without a translation aborts the run so nothing is
silently dropped.

Usage:
  add_ctrl_rows.py --dry-run      # report candidates / coverage / slot fit
  add_ctrl_rows.py                # append the rows to data/translation.tsv
  add_ctrl_rows.py --report out.tsv   # write the candidate list
"""
import argparse
import collections
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402
import mapio                # noqa: E402

CTRL_TSV = os.path.join(ROOT, 'data', 'ctrl_translations.tsv')

# control codes the game actually uses inside text
GOOD = {0xFFA2, 0xFFC0, 0xFFC2, 0xFFC3, 0xFFF2, 0xFFF3,
        0xFE02, 0xFE03, 0xFE04, 0xFE05}
# codes that only ever show up in bitmap/table noise
BAD = {0xFFFF, 0xFFFE, 0xFF00, 0xFF01, 0xFFF8, 0xFFF0, 0xFFF1,
       0xFE9E, 0xFE01}
# entries that are not script at all (fonts, dictionaries, bitmap pools)
SKIP = {850, 851, 852, 605, 547, 571, 611, 636, 1489}
ESC = re.compile(r'\\x([0-9A-Fa-f]{4})')


def load_text_table():
    """code -> character, applying the ROM rule `index = code - 1` (code >= 0x20)."""
    gmap = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    return {idx if idx < 0x20 else idx + 1: ch
            for idx, ch in gmap.items() if ch}


def load_catalog():
    """entry -> (table_off, base) for every entry with an offset table."""
    cat = {}
    path = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    for r in csv.DictReader(open(path, encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = (int(r['table_off']), int(r['base']))
    return cat


def load_master(path):
    """Return the raw rows plus the (entry, idx) / (entry, offset) coverage sets."""
    raw = open(path, encoding='utf-8', newline='').read()
    lines = raw.split('\r\n')
    while lines and lines[-1] == '':
        lines.pop()
    have_t, have_p, known = set(), set(), collections.defaultdict(set)
    max_idx = collections.defaultdict(int)
    for line in lines[1:]:
        p = line.split('\t')
        if len(p) < 7:
            continue
        eid, idx = int(p[0]), int(p[1])
        known[eid].add(p[5])
        max_idx[eid] = max(max_idx[eid], idx)
        if len(p) > 7 and p[7] == 'pool':
            have_p.add((eid, int(p[2])))
        else:
            have_t.add((eid, idx))
    return lines, have_t, have_p, known, max_idx


def offset_vals(d, toff):
    vals, p = [], toff
    while p + 4 <= len(d):
        x = struct.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d) or (vals and x <= vals[-1]):
            break
        vals.append(x)
        p += 4
    return vals


def read_codes(d, off):
    """Codes of the NUL-terminated string at `off` (no terminator), or None."""
    if off + 2 > len(d):
        return None
    n = 0
    while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
        n += 1
    if n == 0:
        return None
    return list(struct.unpack_from('<%dH' % n, d, off))


def scan(rom, table, cat, have_t, have_p, known):
    """Yield (eid, kind, idx, offset, n_codes, text, slot_words)."""
    found = {}
    for eid in range(1500):
        if eid in SKIP:
            continue
        d = g.load_entry(rom, eid)
        if not d or len(d) > 0x40000:
            continue
        vals = offset_vals(d, cat[eid][0]) if eid in cat else []
        base = cat[eid][1] if eid in cat else 0x7A0
        slots = []
        for o in vals:
            codes = read_codes(d, base + o)
            if codes:
                slots.append((base + o, base + o + 2 * (len(codes) + 1)))
        # --- offset-table slots ------------------------------------------
        for i, o in enumerate(vals):
            off = base + o
            if (eid, i) in have_t:
                continue
            codes = read_codes(d, off)
            if not codes:
                continue
            cs = set(codes)
            if not (cs & GOOD) or (cs & BAD):
                continue
            text = ''.join(table.get(c, '[%04X]' % c) for c in codes)
            found[(eid, off)] = ('tab', i, o, len(codes), text, len(codes) + 1)
        # --- NUL-terminated runs outside the offset table -----------------
        for par in (0, 1):
            i = par
            while i + 2 < len(d):
                if struct.unpack_from('<H', d, i)[0] != 0:
                    i += 2
                    continue
                start, buf, j = i + 2, [], i + 2
                while j + 1 < len(d):
                    c = struct.unpack_from('<H', d, j)[0]
                    if c == 0:
                        break
                    if c in table:
                        buf.append(table[c])
                    elif c >= 0xFE00:
                        buf.append('[%04X]' % c)
                    else:
                        break
                    j += 2
                ok = (j + 1 < len(d)
                      and struct.unpack_from('<H', d, j)[0] == 0
                      and len(buf) >= 2)
                text = ''.join(buf)
                cs = set(struct.unpack_from('<%dH' % len(buf), d, start)) if ok else set()
                if (ok and (cs & GOOD) and not (cs & BAD)
                        and not any(s <= start < e for s, e in slots)
                        and start not in have_p
                        and not any(text in k or k in text for k in known[eid])
                        and text not in known[eid]):
                    found.setdefault((eid, start),
                                     ('pool', None, start, len(buf), text, len(buf) + 1))
                i = j if j > i else i + 2
    for (eid, _), row in sorted(found.items()):
        yield (eid,) + row


def load_translations(path):
    tr = {}
    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        if not line:
            continue
        jp, zh = line.split('\t', 1)
        tr[jp] = zh
    return tr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--translations', default=CTRL_TSV)
    ap.add_argument('--report', default='', help='write the candidate list here')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    rom = open(a.rom, 'rb').read()
    table = load_text_table()
    cat = load_catalog()
    lines, have_t, have_p, known, max_idx = load_master(a.master)
    tr = load_translations(a.translations)

    rows = list(scan(rom, table, cat, have_t, have_p, known))
    print(f'candidates: {len(rows)} '
          f'({sum(1 for r in rows if r[1] == "tab")} table + '
          f'{sum(1 for r in rows if r[1] == "pool")} pool)')

    untranslated = sorted({r[5] for r in rows if r[5] not in tr})
    if untranslated:
        print(f'ERROR: {len(untranslated)} candidate(s) have no translation in '
              f'{os.path.relpath(a.translations, ROOT)}:')
        for t in untranslated[:40]:
            print('   ', t)
        return 1

    if a.report:
        with open(a.report, 'w', encoding='utf-8') as f:
            f.write('entry\tkind\tidx\toffset\tn_codes\tjp_text\ttranslation\n')
            for eid, kind, idx, off, n, text, slot in rows:
                f.write(f'{eid}\t{kind}\t{idx if idx is not None else ""}'
                        f'\t{off}\t{n}\t{text}\t{tr[text]}\n')
        print(f'wrote {a.report}')

    # --- slot fit + escape sanity ----------------------------------------
    bad = []
    for eid, kind, idx, off, n, text, slot in rows:
        zh = tr[text]
        # every literal character is one word, and so is each \xXXXX escape
        parts = ESC.split(zh)
        n_codes = len(ESC.findall(zh)) + sum(len(p) for p in parts[::2])
        if n_codes + 1 > slot:
            bad.append((eid, kind, idx, off, n_codes, slot, text))
    if bad:
        print(f'ERROR: {len(bad)} translation(s) do not fit their slot:')
        for b in bad[:20]:
            print('   ', b)
        return 1
    print('slot fit: OK for every candidate')

    new_idx = dict(max_idx)
    out = []
    added_slots = collections.defaultdict(list)   # eid -> [(abs_start, abs_end)]
    for eid, kind, idx, off, n, text, slot in rows:
        if kind == 'tab':
            abs_off = cat[eid][1] + off
            added_slots[eid].append((abs_off, abs_off + 2 * slot))
            out.append([str(eid), str(idx), str(off), str(n), str(2 * (n + 1)),
                        text, tr[text]])
        else:
            new_idx[eid] += 1
            out.append([str(eid), str(new_idx[eid]), str(off), str(n),
                        str(2 * (n + 1)), text, tr[text], 'pool'])
    # A pre-existing pool row may point *inside* an offset-table slot (the pool
    # scanner used to record a suffix of a table string as its own entry).
    # Writing both would let the suffix overwrite the freshly written table
    # string, so once the table slot itself is translated the pool row is
    # redundant: drop it.
    drop = set()
    for i, line in enumerate(lines[1:], start=1):
        p = line.split('\t')
        if len(p) > 7 and p[7] == 'pool':
            eid, off = int(p[0]), int(p[2])
            if any(s <= off < e for s, e in added_slots.get(eid, ())):
                drop.add(i)
                print(f'  dropping redundant pool row e{eid}@{off} '
                      f'(inside a newly translated table slot) {p[5]!r}')
    print(f'rows to append: {len(out)}; redundant pool rows to drop: {len(drop)}')
    if a.dry_run:
        for r in out[:10]:
            print('   ', '\t'.join(r))
        return 0
    kept = [l for i, l in enumerate(lines) if i not in drop]
    with open(a.master, 'w', encoding='utf-8', newline='') as f:
        f.write('\r\n'.join(kept) + '\r\n')
        for r in out:
            f.write('\t'.join(r) + '\r\n')
    print(f'appended {len(out)} rows to {a.master}, dropped {len(drop)} redundant rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
