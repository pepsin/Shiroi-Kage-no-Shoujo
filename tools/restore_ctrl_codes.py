#!/usr/bin/env python3
"""restore_ctrl_codes.py - 把译文里丢掉的控制码补回去（全 ROM 扫描）。

为什么
------
剧本资源里 0x10..0x1F 是**控制码**（不是字形）。日文原文用它们表示
`ー`（长音符）、`～`（长音）、`々`（叠字）这类特殊字，它们在资源里与
文字混排。汉化时译者把这些特殊字整段改写掉了（佐ー木→佐佐木、
アパ～ト→公寓），控制码就随之丢失。

CN 资源缺控制码 → 引擎按控制码序列读取文本/流程时错位，玩家侧表现为
某句话之后再也翻不过去（见 docs/存档卡死分析.md 第零·补四节）。

本工具做的事：对每个条目，逐行比较 CN 资源与日文原版资源的控制码序列；
凡是 CN 上缺的，按「日文原文里该控制码的位置比例」插回主表译文，
写成 \\xNNNN 转义（tools/import_script.py 的 encode_text() 认这个语法）。

用法
----
  tools/restore_ctrl_codes.py                 # 只报告（默认）
  tools/restore_ctrl_codes.py --apply         # 写回 data/translation.tsv（先备份）
  tools/restore_ctrl_codes.py --apply --entries 500,438
"""
import argparse
import csv
import os
import shutil
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g  # noqa: E402

MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
CN_ROM = os.path.join(ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba')
JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')
ENE = 0x7A0                 # 池基址：file offset = 池内偏移 + 0x7A0
CTRL = set(range(0x10, 0x20))
MAX_ENTRY = 1024
MAX_ROWS = 4000


def load_ext_map():
    m = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'work', 'glyph_map.ext.csv'),
                                 encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            m[int(r['code'], 16)] = ch
    return m


def entry_table(d):
    """返回 (table_off, offsets) —— 池内偏移表（升序 u32）。"""
    best = (0, 0, [])
    for phase in range(4):
        p = phase
        while p + 16 <= len(d):
            vals = []
            q = p
            while q + 4 <= len(d):
                x = struct.unpack_from('<I', d, q)[0]
                if x > len(d) or (vals and x <= vals[-1]):
                    break
                vals.append(x)
                q += 4
            if len(vals) > best[0]:
                best = (len(vals), p, vals)
            p = q if q > p else p + 4
    return best[1], best[2]


def row_codes(d, off):
    start = ENE + off
    n = 0
    while start + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, start + 2 * n)[0] != 0:
        n += 1
    if n == 0 or start + 2 * n > len(d):
        return None
    return list(struct.unpack_from(f'<{n}H', d, start))


def insert_codes(text, codes):
    """把 codes 追加到 text 末尾。

    这些控制码是日文特殊字（ー / ～ / 々）的编码，中文里没有对应字，
    只能保证"码位存在于该行"而不能保证语义位置。插在词中间（如
    「假目\x0014标」）反而更糟，所以统一追加到行尾。
    """
    if not codes:
        return text
    return text + ''.join(f'\\x{c:04X}' for c in codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--entries', default='', help='只处理这些条目，逗号分隔')
    a = ap.parse_args()
    only = {int(x) for x in a.entries.split(',') if x.strip()} if a.entries else None

    EXT = load_ext_map()
    cn_rom = open(CN_ROM, 'rb').read()
    jp_rom = open(JP_ROM, 'rb').read()

    with open(MASTER, encoding='utf-8', newline='') as f:
        rd = csv.DictReader(f, delimiter='\t')
        fields = rd.fieldnames
        mrows = list(rd)
    by_key = {}
    for r in mrows:
        by_key.setdefault((r['entry'], r['idx']), r)

    def dec(codes):
        return ''.join(EXT.get(c if c < 0x20 else c - 1, '?') for c in codes)

    fixes = []
    for eid in range(0, MAX_ENTRY):
        if only and eid not in only:
            continue
        dc = g.load_entry(cn_rom, eid)
        dj = g.load_entry(jp_rom, eid)
        if not dc or not dj:
            continue
        tc, tab_c = entry_table(dc)
        tj, tab_j = entry_table(dj)
        if not tab_c or tab_c != tab_j:
            continue      # 结构不一致的条目跳过（只处理两边行表相同的）
        # 主表 offset 基准：file offset = 池内偏移 + 0x7A0（实测，见文件头）
        base = ENE
        # 建立主表 offset -> 行
        mrows_e = {}
        for r in mrows:
            if r['entry'] == str(eid):
                mrows_e[int(r['offset'])] = r
        if not mrows_e:
            continue
        # 用最小行对齐算出主表 offset 的起点（主表 offset 一般单调）
        for i, off in enumerate(tab_c):
            c = row_codes(dc, off)
            j = row_codes(dj, off)
            if c is None or j is None:
                continue
            cc = [x for x in c if x in CTRL]
            cj = [x for x in j if x in CTRL]
            if cc == cj:
                continue
            from collections import Counter
            need = Counter(cj) - Counter(cc)
            if not need:
                continue
            m = mrows_e.get(off + base)
            if m is None:
                continue
            # 校验：主表译文的可见文字应和 ROM 里这行的文字一致
            vis = re.sub(r'\\x[0-9A-Fa-f]{4}', '', m['translation'] or '')
            if vis.strip() and vis.strip() not in dec(c).strip() and dec(c).strip() not in vis.strip():
                print(f'  [skip] e{eid} 行{i}: 主表 idx={m["idx"]} 文本对不上 '
                      f'(主表 {vis.strip()[:16]!r} vs ROM {dec(c).strip()[:16]!r})')
                continue
            fixes.append(dict(entry=eid, row=i, off=off,
                              missing=[k for k, n in need.items() for _ in range(n)],
                              master=m, cn=dec(c).strip(), jp=dec(j).strip(),
                              slot_words=max(0, (tab_c[i + 1] - off) // 2) if i + 1 < len(tab_c)
                                         else len(c) + 1,
                              have_words=len(c)))

    dropped = []
    for f in fixes:
        m = f['master']
        codes = f['missing']
        # 现有译文的可见码位数（去掉已有转义）
        vis = re.sub(r'\\x[0-9A-Fa-f]{4}', '', m['translation'] or '')
        vis_n = len(vis)
        room = f['slot_words'] - 1 - len(codes)      # 留给可见字的码位数
        if room < 0:
            dropped.append((f, '槽位太小，连控制码都放不下'))
            continue
        if vis_n > room:
            # 优先保控制码：把可见文字缩短到 room 个码位
            f['trimmed'] = vis[:room] if room > 0 else ''
            dropped.append((f, f'缩短 {vis_n - room} 个码位：'
                               f'{vis!r} -> {f["trimmed"]!r}'))
        f['suggest'] = insert_codes(f.get('trimmed', vis), codes)
    real = [f for f in fixes if f.get('suggest')]
    print(f'\n共 {len(fixes)} 行需要补控制码；其中 {len(dropped)} 行需要缩短译文。')
    for f, why in dropped[:15]:
        print(f'   e{f["entry"]} 行{f["row"]} idx={f["master"]["idx"]}: {why}')
    if len(dropped) > 15:
        print(f'   … 其余 {len(dropped) - 15} 行同样处理')
    fixes = real

    if a.apply and fixes:
        bak = MASTER + '.bak-ctrlcodes'
        shutil.copy2(MASTER, bak)
        print(f'已备份 {bak}')
        # 按行改写，避免 csv 往返破坏其它列
        want = {(f['entry'], f['master']['idx']): f['suggest'] for f in fixes}
        n = 0
        with open(MASTER, encoding='utf-8') as fi, \
             open(MASTER + '.tmp', 'w', encoding='utf-8') as fo:
            header = fi.readline()
            fo.write(header)
            for line in fi:
                parts = line.rstrip('\n').split('\t')
                keyset = {(int(parts[0]), parts[1])} if len(parts) > 1 else set()
                hit = next((k for k in keyset if k in want), None)
                if hit is not None:
                    parts[-1] = want[hit]
                    n += 1
                    fo.write('\t'.join(parts) + '\n')
                else:
                    fo.write(line)
        os.replace(MASTER + '.tmp', MASTER)
        print(f'已写回 {MASTER}（改写 {n} 行）')
    elif not a.apply:
        print('（未改动；加 --apply 写回）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
