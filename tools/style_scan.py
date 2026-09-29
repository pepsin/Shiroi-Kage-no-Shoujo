#!/usr/bin/env python3
"""Rank untranslated-by-us narration lines by "machine translation smell".

The worklist says what has been *touched*; it cannot say what still reads like a
machine.  This tool scores the remaining narration lines (地の文, non-dialogue
display lines, see ``narration_blocks.py``) against the patterns the style pass
keeps removing:

  calque   关于 / 进行 / 有着 / 所谓的 / 的存在 / 对于 / 作为一个
  passive  被 + verb + 了/到/着   (Japanese 受身 copied into Chinese)
  nomi     的事情 / 的样子 / 的时候 / 的东西   (…の事 / …様子 / …時 calques)
  bloat    Chinese line >= 1.45x the Japanese character count

Lines already named in a ``work/scenes/narr_order*.tsv`` / ``narr_align*.tsv`` /
``*_family.tsv`` mapping count as reviewed and are skipped, so the output shrinks
as the sweep progresses.

Usage
-----
  python3 tools/style_scan.py                 # summary + top offenders
  python3 tools/style_scan.py --limit 40      # more rows
  python3 tools/style_scan.py --scene 79.0    # one scene
  python3 tools/style_scan.py --rule passive  # one rule
  python3 tools/style_scan.py --json          # machine readable
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import narration_blocks as nb          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPPINGS = ['work/scenes/narr_order*.tsv', 'work/scenes/narr_align*.tsv',
            'work/scenes/*_family.tsv', 'work/scenes/defect_fix.tsv',
            'work/scenes/narr_fix_naina.tsv']

RULES = [
    ('calque', re.compile(r'关于|进行|有着|所谓的|的存在|对于|作为一个')),
    ('passive', re.compile(r'被.{1,6}(了|到|着)')),
    ('nomi', re.compile(r'的事情|的样子|的时候|的东西')),
]
# A Chinese "的事情/的东西/的样子/的时候" is only a calque when the Japanese
# line has no matching noun: 貴之が拾った物は -> "貴之捡到的东西" is faithful,
# while 報告するため -> "为了报告的事情" would not be.
NOMI_PAIR = [('的事情', ('事',)), ('的东西', ('物', 'もの')),
             ('的样子', ('様子', 'よう')), ('的时候', ('時', 'とき'))]


def reviewed_lines():
    out = set()
    for pat in MAPPINGS:
        for path in glob.glob(os.path.join(ROOT, pat)):
            for line in open(path, encoding='utf-8'):
                if line.startswith('#') or '\t' not in line:
                    continue
                out.add(line.split('\t')[0])
    return out


def scan(min_chars=9, scene=None, rule=None):
    seen = reviewed_lines()
    rows = []
    for b in nb.all_blocks(1, 0):
        if scene and b['scene'] != scene:
            continue
        if scene is None and float(b['scene'].split('.')[0]) < 19:
            continue
        for r in b['rows']:
            jp = nb.strip_ctrl(r['jp_text'])
            if len(jp) < min_chars or jp in seen:
                continue
            cn = r['translation']
            why = []
            for name, rx in RULES:
                if not rx.search(cn):
                    continue
                if name == 'nomi' and any(
                        w in cn and any(j in jp for j in words)
                        for w, words in NOMI_PAIR):
                    # the Japanese really says 事/物/様子/時 - not a calque
                    stripped = cn
                    for w, words in NOMI_PAIR:
                        if any(j in jp for j in words):
                            stripped = stripped.replace(w, '')
                    if not re.search(r'的事情|的样子|的时候|的东西', stripped):
                        continue
                why.append(name)
            if len(cn) >= len(jp) * 1.45:
                why.append('bloat')
            if why and (rule is None or rule in why):
                rows.append({'scene': b['scene'], 'entry': r['entry'],
                             'idx': r['idx'], 'jp': jp, 'cn': cn,
                             'rules': why})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=25)
    ap.add_argument('--scene')
    ap.add_argument('--rule')
    ap.add_argument('--min-chars', type=int, default=9)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()

    rows = scan(a.min_chars, a.scene, a.rule)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    by_rule = collections.Counter(r for row in rows for r in row['rules'])
    print(f'flagged narration lines: {len(rows)}')
    for k, v in by_rule.most_common():
        print(f'  {k}: {v}')
    big = collections.Counter(row['jp'] for row in rows)
    print('most repeated:')
    for jp, n in big.most_common(10):
        cx = next(r['cn'] for r in rows if r['jp'] == jp)
        print(f'  {n:>3}  {jp[:32]!r} | {cx[:28]!r}')
    print(f'--- first {a.limit} in scene order')
    rows.sort(key=lambda r: (float(r['scene'].split('.')[0]),
                             float(r['scene'].split('.')[1]), int(r['idx'])))
    for r in rows[:a.limit]:
        print(f"  {r['scene']:>7} e{r['entry']}:{r['idx']} "
              f"[{','.join(r['rules'])}] {r['jp'][:28]!r} | {r['cn'][:26]!r}")


if __name__ == '__main__':
    main()
