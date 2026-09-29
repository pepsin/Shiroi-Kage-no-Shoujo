#!/usr/bin/env python3
"""Verify a built Chinese ROM against the master translation table.

For every translated row it reads the string back out of the ROM (using the
ROM's own FAT/string structure) and compares it with data/translation.tsv.
It also checks that every code the script uses has a glyph in the ROM's font
table.

Usage:
  verify_rom.py <rom> [--master data/translation.tsv] [--map work/glyph_map.ext.csv]
"""
import argparse
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g
import export_script as ex
import mapio

# The JP-only tools cap codes at the original table size; the Chinese build
# appends glyphs past it, so widen the accepted range before analysing.  The
# cap must clear the raw control codes as well (0xFE02/0xFFC2/0xFFF2 ...):
# read_string() rejects any code above it, which made every control-code
# string look untranslated ("got None").
ex.GLYPH_MAX = 0x10000

# Codes at or above this are text-engine control codes, never glyphs.
CTRL_MIN = 0xFE00


def norm_tr(t):
    r"""Translations may embed raw control codes as \xXXXX escapes (e.g. the
    \xFFF2/\xFFF3 name-highlight pair in e314 memo strings); the ROM side
    decodes them as [FFF2].  Normalize the translation to the same shape."""
    return re.sub(r'\\x([0-9A-Fa-f]{4})', lambda m: '[' + m.group(1).upper() + ']', t)


def code_len(s):
    """码位数——与 import_script.encode_text 同一约定（\\xNNNN 记 1 个）。"""
    return len(re.sub(r'\\x[0-9A-Fa-f]{4}', 'X', s))


def why(row, want, got):
    """一句话说清这一行为什么和主表对不上（给人看，不参与判定）。

    打包只把译文写回**原槽位**（n_codes + 1 个 u16，含结尾 00）：装不下就跳过、
    保留日文原文。所以「ROM 里还是日文」这件事，几乎总是译文超长——直接说明白。
    """
    if got is None:
        return 'ROM 里读不到这条字符串（偏移或表结构变了）'
    if row is not None and got == (row.get('jp_text') or ''):
        need = code_len(want) + 1
        slot = int(row['n_codes']) + 1
        if need > slot:
            return (f'没写进去：译文 {need - 1} 码位 + 结尾 = {need}，槽位只有 {slot}'
                    f'（超 {need - slot}）—— 译文必须改短')
        return '没写进去（不是超长：可能是缺字/未映射，或该条目没被打包）'
    return '写进去的内容与主表不一致（不是没写，是写错了）'


# 差异最多列这么多行，其余只报数量
MAX_DIFF_SHOW = 40

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
# e0 的数据区 = 引擎的剧本分发表所在（FAT[0] = off 0 size 0xFEC）
CRIT_OFF = 0x15C000
CRIT_LEN = 0xFEC


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    m = mapio.load_map(a.map)
    # decode rule (verified against the JP ROM): char = map[code] for
    # code < 0x20, else map[code - 1]
    table = {}
    for idx, ch in m.items():
        if not ch:
            continue
        table[idx if idx < 0x20 else idx + 1] = ch

    cat = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'data', 'entry_catalog.tsv'),
                                 encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = (int(r['table_off']), int(r['base']))
    rows = list(csv.DictReader(open(a.master, encoding='utf-8'), delimiter='\t',
                               restkey='extra'))
    want = {}          # entry -> {idx: text}   (offset-table rows)
    want_pool = {}     # entry -> {absolute_offset: (text, slot_words)}
    row_of = {}        # (eid, idx)    -> 主表行（报告差异时还原上下文）
    row_of_pool = {}   # (eid, offset) -> 主表行
    for r in rows:
        tr = (r.get('translation') or '').strip()
        if not tr:
            continue
        eid = int(r['entry'])
        if (r.get('extra') or [''])[0] == 'pool':
            off = int(r['offset'])
            want_pool.setdefault(eid, {})[off] = (norm_tr(tr), int(r['n_codes']) + 1)
            row_of_pool[(eid, off)] = r
        else:
            idx = int(r['idx'])
            want.setdefault(eid, {})[idx] = norm_tr(tr)
            row_of[(eid, idx)] = r

    # --- 引擎关键区：e0 数据区（含剧本分发表）必须与日文原版逐字节一致 --------
    # FAT[0] = (off 0, size 0xFEC)，所以 file 0x15C000..0x15CFEC 是 **e0 的数据区**，
    # 引擎的剧本分发表（file 0x15C004，509 条 (keyA,keyB)→entry）就在里面。
    # 剧本入口和「读档后接着演」都查这张表；汉化只该改字符串槽位，这块必须一个
    # 字节都不动（FAT 只有 1024 项，eid>=1024 的写入也会砸到这里）。
    crit_ok = True
    crit_diffs = []
    if os.path.exists(g.JP_ROM):
        jp = open(g.JP_ROM, 'rb').read()
        if len(jp) >= CRIT_OFF + CRIT_LEN:
            crit_diffs = [i for i in range(CRIT_OFF, CRIT_OFF + CRIT_LEN)
                          if jp[i] != rom[i]]
            crit_ok = not crit_diffs
    if not os.path.exists(g.JP_ROM):
        print(f'engine-critical region 0x{CRIT_OFF:X}: 找不到日文原版，跳过对比'
              f'（{os.path.basename(g.JP_ROM)}）')
    else:
        print(f'engine-critical region 0x{CRIT_OFF:X}..0x{CRIT_OFF + CRIT_LEN:X} '
              f'(e0 数据 = 剧本分发表，读档续演靠它): {len(crit_diffs)} 字节差异 '
              f'{"✓" if crit_ok else "✗ PROBLEM"}')
        if crit_diffs:
            print('  前几处差异:', [hex(x) for x in crit_diffs[:8]])

    # --- font table sanity ------------------------------------------------
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    foff = BASE + off
    print(f'font entry {FONT_EID}: file 0x{foff:X} size {size} '
          f'({size // 0x80} glyphs); ROM size {len(rom)} ({len(rom) / 1048576:.2f} MB)')
    assert foff + size <= len(rom), 'font table runs past EOF'
    blank = sum(1 for i in range(size // 0x80)
                if not any(rom[foff + i * 0x80:foff + (i + 1) * 0x80]))
    print(f'blank glyph slots in table: {blank}')

    # --- every code used by the translation must have a glyph -------------
    used_codes = set()
    for eid, trs in want.items():
        d = g.load_entry(rom, eid)
        if d is None:
            continue
        if eid not in cat:
            continue
        toff, base = cat[eid]
        vals, p = [], toff
        while p + 4 <= len(d):
            x = struct.unpack_from('<I', d, p)[0]
            if x == 0xFFFFFFFF or x > len(d) or (vals and x <= vals[-1]):
                break
            vals.append(x)
            p += 4
        for idx in trs:
            if idx >= len(vals):
                continue
            o = base + vals[idx]
            n = 0
            while o + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, o + 2 * n)[0] != 0:
                n += 1
            if o + 2 * n > len(d):
                continue
            used_codes.update(struct.unpack_from(f'<{n}H', d, o))
    missing = sorted(c for c in used_codes
                     if c not in (0, 0x0D, 0x0E) and c < CTRL_MIN and c not in table)
    print(f'codes referenced by the script: {len(used_codes)}; without a glyph: {len(missing)}')
    if missing:
        print('  e.g.', [hex(c) for c in missing[:10]])

    # --- round-trip ------------------------------------------------------
    ok = bad = 0
    diffs = []          # (label, 主表行, 期望译文, ROM 里读到的)
    # entries whose text lives in a NUL-separated pool (no offset table)
    for eid in sorted(want_pool):
        d = g.load_entry(rom, eid)
        if d is None:
            print(f'e{eid:04d}: missing entry')
            continue
        for off, (tr, slot) in sorted(want_pool[eid].items()):
            n = 0
            while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
                n += 1
            got = ''.join(table.get(c, f'[{c:03X}]')
                          for c in struct.unpack_from(f'<{n}H', d, off))
            if got == tr:
                ok += 1
            else:
                bad += 1
                row = row_of_pool.get((eid, off))
                diffs.append((f'e{eid}:{row["idx"] if row else "?"} (pool @0x{off:X})',
                              row, tr, got))
    for eid in sorted(want):
        d = g.load_entry(rom, eid)
        if d is None:
            print(f'e{eid:04d}: missing entry')
            continue
        if eid not in cat:
            print(f'e{eid:04d}: not in entry catalog')
            continue
        toff, base = cat[eid]
        got = {}
        for idx, o, codes, end in ex.entry_strings(d, toff, base):
            got[idx] = ''.join(table.get(c, f'[{c:03X}]') for c in codes)
        for idx, tr in want[eid].items():
            g_ = got.get(idx)
            if g_ == tr:
                ok += 1
            else:
                bad += 1
                diffs.append((f'e{eid}:{idx}', row_of.get((eid, idx)), tr, g_))
    print(f'round-trip (table + pool): {ok} strings match, {bad} differ')
    for label, row, tr, got in diffs[:MAX_DIFF_SHOW]:
        print(f'  ✗ {label}   {why(row, tr, got)}')
        print(f'      主表: {tr}')
        print(f'      ROM : {got if got is not None else "（读不到这条字符串）"}')
    if len(diffs) > MAX_DIFF_SHOW:
        print(f'  … 其余 {len(diffs) - MAX_DIFF_SHOW} 处省略。')
    good = (bad == 0 and not missing and crit_ok)
    if not good:
        print('  一行行点名这些差异（1 秒，不用等打包）：')
        print('      python3 tools/check_slots.py     # 译文超过原槽位的情况')
    print('RESULT:', 'OK' if good else 'PROBLEM')
    # The build's self-check reads this exit code, so a mismatch has to fail the
    # build instead of only printing PROBLEM.
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
