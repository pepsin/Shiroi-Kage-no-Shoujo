#!/usr/bin/env python3
"""预检：译文装不装得进原槽位——打包前的快速失败。

为什么需要
----------
`import_script.py` 把译文**写回原字符串的槽位**：表行、pool 行的槽位都是
`n_codes + 1` 个 u16（含结尾的 0x0000）。译文的码位数一旦超过原文就装不下，
工具会**跳过这一行、保留日文原文**；ROM 于是和主表对不上，`build_rom.py` 的
自检（verify_rom.py）在打包两分钟后才打出一句很难读的差异，`make` 最后只留
`make: *** [rom] Error 1`。

这个检查只读 TSV、不碰 ROM，一秒内把每一行「差几个码位」列清楚，把失败提前到
打包之前。规则同 data/translation_rules.md：**译文码位数 ≤ 原文码位数**。

用法：
  tools/check_slots.py [--master data/translation.tsv] [--lines data/scene_lines.tsv]
退出码：0 全部装得下；1 有行装不下。
"""
import argparse
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
LINES = os.path.join(ROOT, 'data', 'scene_lines.tsv')

MAX_SHOW = 30


def code_count(s):
    """码位数——与 import_script.encode_text 同一约定。

    `\\xNNNN` 是一个码位（1 个 u16），其余每个字符也各占一个码位。
    """
    return len(re.sub(r'\\x[0-9A-Fa-f]{4}', 'X', s))


def load_scene_of(lines_path):
    """(entry, idx) -> '场景 / pos'，scene_lines.tsv 是派生文件，缺了也不影响判定。"""
    scene = {}
    if not os.path.exists(lines_path):
        return scene
    with open(lines_path, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            e, i = r.get('entry'), r.get('idx')
            if e and i:
                scene.setdefault((e, i), (r.get('scene') or '', r.get('pos') or ''))
    return scene


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--master', default=MASTER)
    ap.add_argument('--lines', default=LINES)
    a = ap.parse_args()

    with open(a.master, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f, delimiter='\t', restkey='extra'))
    scene = load_scene_of(a.lines)

    n_trans = 0
    bad = []
    for r in rows:
        tr = (r.get('translation') or '').strip()
        if not tr:
            continue
        n_trans += 1
        need = code_count(tr) + 1              # 译文 + 结尾 0x0000
        slot = int(r['n_codes']) + 1           # 原文槽位，见 import_script.rebuild_*
        if need > slot:
            kind = 'pool' if (r.get('extra') or [''])[0] == 'pool' else 'table'
            bad.append((r, kind, need, slot))

    print(f'== 槽位预检：译文码位数 ≤ 原文码位数（{os.path.relpath(a.master, ROOT)}）')
    print(f'   主表 {len(rows)} 行；已译 {n_trans} 行；装不下 {len(bad)} 行')
    if not bad:
        print('   OK：每一行的译文都放得进原槽位，可以打包。')
        return 0

    print()
    print(f'!! 有 {len(bad)} 行译文超过原文码位数，打包时会被跳过'
          '（ROM 里保留日文原文），自检必然失败：')
    for r, kind, need, slot in bad[:MAX_SHOW]:
        e, i, nc, jp, tr = r['entry'], r['idx'], int(r['n_codes']), r['jp_text'], r['translation']
        if kind == 'pool':
            where = f'e{e}:{i}  pool @0x{int(r["offset"]):X}'
        else:
            where = f'e{e}:{i}  table'
        sc, pos = scene.get((e, i), ('', ''))
        print()
        print(f'  {where}   原文 {nc} 码位 / 译文 {need - 1} 码位   ← 至少改短 {need - 1 - nc} 个码位')
        print(f'      jp  : {jp}')
        print(f'      译文: {tr}')
        if sc:
            print(f'      场景: {sc}' + (f'（第 {pos} 句）' if pos else ''))
        print(f'      只改这里: rid={e}:{i}（在 {os.path.relpath(a.lines, ROOT)} 里找这一行）')
    if len(bad) > MAX_SHOW:
        print(f'\n  … 其余 {len(bad) - MAX_SHOW} 行省略。')

    print()
    print(f'改法：在 {os.path.relpath(a.lines, ROOT)} 里把这几行译文改短到不超过原文码位数'
          '（标点别动，见 data/translation_rules.md），')
    print('     再跑 make——scene_lines 的改动会自动合并回主表。')
    print('     直接改主表也可以，但记得两边一致，否则 make 会把 scene_lines 的旧译文合并回来。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
