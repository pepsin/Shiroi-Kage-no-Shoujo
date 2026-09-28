#!/usr/bin/env python3
"""Export scene-grouped review blocks for the translation refinement pass.

The 2025 first pass translated each *display line* on its own.  Since the game
splits one sentence across several lines and Japanese puts the verb at the end,
the Chinese inherited Japanese word order ("我板着脸 / 向正走出会场的女性 /
目光停住了").  Fixing that needs the whole conversation in view, not a line:
word order can only be repaired across line boundaries if you can see which
line carries the verb.

This tool joins ``data/scene_index.py`` with the master table and writes review
blocks - a whole scene, in story order, with the Japanese and the current
Chinese of every line (and how the lines join into sentences).

Usage
-----
  # one scene, read it end to end
  export_scene_context.py --scene 3.0

  # every story scene of a few entries -> work/scenes/e1.tsv
  export_scene_context.py --entries 1,2,3 --out work/scenes/e0.tsv

  # only scenes whose Chinese looks inverted/calqued (see --flags)
  export_scene_context.py --flagged --limit 40 --out work/scenes/flag1.tsv

Output (TSV, one row per display line, blank line between scenes):

  scene  entry  idx  pos  join  len  jp_text  translation  note

``join`` is ``>`` when the sentence continues on the next line, ``<`` when it
continues from the previous one, ``<>`` for a middle line, ``-`` for a line
that ends a sentence.  ``len`` is the row's own code budget.
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

# Chinese constructions that usually mean the line copied Japanese order.
FLAG_PATTERNS = [
    ('passive', re.compile(r'被')),
    ('calque', re.compile(r'(有着|的存在|的样子|的表情|的场合|所谓的|进行|加以|能够|'
                          r'不得不|对于|关于|使得|让人|使人|感到|觉得)')),
    ('verb-tail', re.compile(r'(停下|停住|出现|走来|过来|出来|下来|上来|进入|走向|走去|'
                             r'看向|说|问|答|笑|站|坐|停|落|飘|闪|响|亮|暗|灭|涌|袭|'
                             r'去|来|走|回|到)$')),
    ('particles', re.compile(r'[呢吧啊呀哦噢哟]')),
    ('kana-left', re.compile(r'[\u3041-\u309f\u30a0-\u30fa]')),
    ('filler', re.compile(r'(非常|十分|相当|因为|所以|但是|可是|而且|然后)')),
]
KANA = re.compile(r'[\u3041-\u309F\u30A0-\u30FA]')


def load_scenes():
    out = collections.OrderedDict()
    for r in csv.DictReader(open(SCENES, encoding='utf-8'), delimiter='\t'):
        out[r['scene']] = r
    return out


def load_lines():
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
    return by_scene, order


def scene_flags(lines):
    """Cheap signals that a scene needs the word-order / style pass."""
    why = set()
    for r in lines:
        cn = r[7]
        if not cn:
            continue
        if KANA.search(cn):
            why.add('kana')
        for name, pat in FLAG_PATTERNS:
            if pat.search(cn.rstrip('，。！？『」“')):
                why.add(name)
                break
    # a unit that ends on a bare verb is the classic inversion
    for i, r in enumerate(lines):
        if '>' in r[5]:
            continue
        cn = r[7].rstrip('，。！？『」“')
        if FLAG_PATTERNS[2][1].search(cn):
            why.add('verb-tail')
    return sorted(why)


def emit(f, s, lines, why):
    print(f"# scene {s['scene']}  kind={s['kind']}  entry={s['entry']}  "
          f"idx {s['idx_from']}..{s['idx_to']}  rows={s['rows']}"
          + (f"  地点={s['location']}" if s['location'] else '')
          + (f"  在场={s['speakers']}" if s['speakers'] else '')
          + (f"  flags={','.join(why)}" if why else ''), file=f)
    print('\t'.join(['scene', 'entry', 'idx', 'pos', 'join', 'len',
                     'jp_text', 'translation', 'note']), file=f)
    for r in lines:
        _rid, eid, idx, sid, pos, join, jp, cn = r[:8]
        print('\t'.join([sid, eid, idx, pos, join or '-', str(len(jp)),
                         read_escapes(jp), read_escapes(cn), '']), file=f)
    print('', file=f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scene', action='append', default=[])
    ap.add_argument('--entries', default='', help='comma list / range, e.g. 1,2,3 or 1-20')
    ap.add_argument('--kind', action='append', default=[],
                    help='scene kind filter (scene / system / quiz / chara / credits)')
    ap.add_argument('--flagged', action='store_true',
                    help='only scenes with a style/word-order flag')
    ap.add_argument('--out', default='')
    ap.add_argument('--limit', type=int, default=0, help='max scenes')
    ap.add_argument('--min-rows', type=int, default=0)
    a = ap.parse_args()

    scenes = load_scenes()
    by_scene, order = load_lines()

    want = set(a.scene)
    if a.entries:
        sel = set()
        for part in a.entries.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                lo, hi = part.split('-')
                sel.update(str(i) for i in range(int(lo), int(hi) + 1))
            else:
                sel.add(part)
        want |= {sid for sid, s in scenes.items() if s['entry'] in sel}
    if not want:
        want = set(order)

    picked = []
    for sid in order:
        if sid not in want:
            continue
        s = scenes.get(sid)
        if s is None:
            continue
        if a.kind and s['kind'] not in a.kind:
            continue
        if a.min_rows and int(s['rows']) < a.min_rows:
            continue
        lines = by_scene[sid]
        why = scene_flags(lines)
        if a.flagged and not why:
            continue
        picked.append((s, lines, why))
    if a.limit:
        picked = picked[:a.limit]
    if not picked:
        print('nothing matched (try --kind scene / --flagged / --entries 1-20)')
        return 1

    if a.out:
        os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
        with open(a.out, 'w', encoding='utf-8') as f:
            for s, lines, why in picked:
                emit(f, s, lines, why)
        print(f'wrote {a.out}: {len(picked)} scenes, '
              f'{sum(len(l) for _, l, _ in picked)} rows')
    else:
        for s, lines, why in picked:
            emit(sys.stdout, s, lines, why)
    return 0


if __name__ == '__main__':
    sys.exit(main())
