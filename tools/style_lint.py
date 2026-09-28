#!/usr/bin/env python3
"""Rank scenes by how much Japanese word order / style they still carry.

`qa_translation.py` finds *mechanical defects* (dropped negation, length, kana).
This tool answers the next question: **which scenes should the word-order and
tone pass visit first?**  It scores a scene on the things the refinement rules in
``data/translation_rules.md`` forbid:

  passive    被 + verb           (Japanese 受身 copied into Chinese)
  calque     有着 / 的存在 / 的样子 / 进行 / 对于 / 能够 …
  verb-tail  a sentence unit whose Chinese *ends* on a bare verb
  particles  呢 / 吧 / 啊 / 呀 / 哦 … in narration (tone filler)
  filler     因为 / 所以 / 但是 / 非常 / 十分 … glue words the tone wants gone
  passive2   「被…所…」 frame
  bracket    「…的…的…」 stacked 的 inside one display line

It is a *worklist generator*, not a judge: every hit is a "look at this line",
and a human still decides.  The scene score is weighted hits per 100 lines so a
990-row chapter and a 20-row scene can be compared.

Usage
-----
  style_lint.py                          # all story scenes, worst first
  style_lint.py --kind scene --top 30
  style_lint.py --scene 9.0 --lines      # every flagged line of one scene
  style_lint.py --entries 1-20 --top 15
  style_lint.py --out work/style.tsv
"""
import argparse
import collections
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from scene_index import LINES, SCENES, read_escapes  # noqa: E402

RULES = [
    # name, pattern, weight, scope(line/unit)
    ('passive', re.compile(r'被[^，。！？『」“]{0,8}[了到着]'), 3, 'line'),
    ('passive2', re.compile(r'被[^，。！？『」“]{0,8}所'), 3, 'line'),
    ('calque', re.compile(r'(有着|的存在|的样子|的表情|的场合|所谓的|进行|加以|'
                          r'对于|关于|能够|不得不)'), 2, 'line'),
    ('filler', re.compile(r'(因为|所以|但是|可是|而且|然后|非常|十分|相当|'
                          r'感到|觉得|使得|让人|使人)'), 1, 'line'),
    ('particles', re.compile(r'[呢吧啊呀哦噢哟]'), 1, 'line'),
    ('stacked', re.compile(r'的[^，。！？『」“]{0,4}的'), 1, 'line'),
    ('verb-tail', re.compile(r'(停下|停住|出现|走来|过来|出来|下来|上来|进入|走向|走去|'
                             r'看向|浮现|退去|笼罩|拍了下来|感觉到)$'), 3, 'unit'),
    ('kana', re.compile(r'[\u3041-\u309F\u30A0-\u30FA]'), 5, 'line'),
]


def load():
    scenes = collections.OrderedDict()
    for r in csv.DictReader(open(SCENES, encoding='utf-8'), delimiter='\t'):
        scenes[r['scene']] = r
    by_scene = collections.OrderedDict()
    order = []
    for r in csv.reader(open(LINES, encoding='utf-8'), delimiter='\t'):
        if r[0] == 'rid':
            continue
        sid = r[3]
        if sid not in by_scene:
            by_scene[sid] = []
            order.append(sid)
        by_scene[sid].append(r)
    return scenes, by_scene, order


def line_hits(jp, cn):
    hits = []
    body = cn.rstrip('，。！？『」“')
    for name, pat, w, scope in RULES:
        if scope != 'line':
            continue
        if pat.search(body):
            hits.append(name)
    return hits


def unit_tail_hits(lines):
    """A unit that ends on a bare verb is the classic inversion."""
    hits = []
    for r in lines:
        if '>' in r[5]:
            continue
        cn = r[7].rstrip('，。！？『」“')
        if not cn:
            continue
        for name, pat, w, scope in RULES:
            if scope == 'unit' and pat.search(cn):
                hits.append(name)
                break
    return hits


def scan(scenes, by_scene, order, want_kinds=None, want=None, entries=None):
    out = []
    for sid in order:
        s = scenes.get(sid)
        if s is None:
            continue
        if want_kinds and s['kind'] not in want_kinds:
            continue
        if want and sid not in want:
            continue
        if entries and s['entry'] not in entries:
            continue
        lines = by_scene[sid]
        counter = collections.Counter()
        flagged = []
        for r in lines:
            h = line_hits(r[6], r[7])
            if h:
                counter.update(h)
                flagged.append((r, h))
        ut = unit_tail_hits(lines)
        counter.update(ut)
        weight = sum(w for n, _p, w, _s in RULES for _ in range(counter[n]))
        rows = max(1, len(lines))
        score = 100.0 * weight / rows
        out.append({'scene': sid, 'kind': s['kind'], 'entry': s['entry'],
                    'rows': rows, 'location': s['location'], 'score': score,
                    'hits': counter, 'flagged': flagged,
                    'speakers': s['speakers']})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kind', action='append', default=[])
    ap.add_argument('--scene', action='append', default=[])
    ap.add_argument('--entries', default='')
    ap.add_argument('--top', type=int, default=30)
    ap.add_argument('--min-rows', type=int, default=5)
    ap.add_argument('--lines', action='store_true', help='print flagged lines')
    ap.add_argument('--out', default='')
    a = ap.parse_args()

    scenes, by_scene, order = load()
    entries = None
    if a.entries:
        entries = set()
        for part in a.entries.split(','):
            part = part.strip()
            if '-' in part:
                lo, hi = part.split('-')
                entries.update(str(i) for i in range(int(lo), int(hi) + 1))
            elif part:
                entries.add(part)
    res = scan(scenes, by_scene, order, a.kind or ['scene'], set(a.scene), entries)
    res = [r for r in res if r['rows'] >= a.min_rows]
    res.sort(key=lambda r: -r['score'])

    if a.out:
        os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write('scene\tentry\trows\tscore\t' + '\t'.join(n for n, *_ in RULES) + '\n')
            for r in res:
                f.write('\t'.join([r['scene'], r['entry'], str(r['rows']),
                                   f"{r['score']:.1f}"]
                                  + [str(r['hits'][n]) for n, *_ in RULES]) + '\n')
        print(f'wrote {a.out}: {len(res)} scenes')

    if a.scene and a.lines:
        for r in res:
            print(f"=== {r['scene']} ({r['rows']} rows, score {r['score']:.1f})")
            for row, h in r['flagged']:
                print(f"  {row[2]:>5} [{','.join(h)}] {read_escapes(row[6])}")
                print(f"        {read_escapes(row[7])}")
        return 0

    print(f"{'scene':<9}{'rows':>6}{'score':>7}  top hits")
    for r in res[:a.top]:
        top = ', '.join(f'{n}={c}' for n, c in r['hits'].most_common(4))
        print(f"{r['scene']:<9}{r['rows']:>6}{r['score']:>7.1f}  {top}")
    n = len(res)
    avg = sum(r['score'] for r in res) / max(1, n)
    print(f'-- {n} scenes, mean score {avg:.1f}')
    tot = collections.Counter()
    for r in res:
        tot.update(r['hits'])
    print('-- hits: ' + ', '.join(f'{k}={v}' for k, v in tot.most_common()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
