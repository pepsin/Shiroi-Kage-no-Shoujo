#!/usr/bin/env python3
"""Narration-block index: group the 地の文 (non-dialogue) runs of the script.

A conversation is reviewed scene by scene; narration has to be reviewed as a
*paragraph*, because the Japanese splits one narrated sentence across several
display lines and that is exactly where the machine pass copied Japanese word
order ("...を...した" read as "...把...做了").  This tool finds the runs of
consecutive non-dialogue lines inside every story scene, so the paragraph can
be read as one unit, rewritten as one unit, and split back into the same number
of lines with the same per-line punctuation.

What counts as narration: a display line whose Japanese carries none of the
quote markers 」『「 (thought lines marked with 』…＋ also count) and which is
not a bare speaker label.  Menu/command rows live in system blocks and are
skipped because only `kind == scene` rows are indexed here.

Usage
-----
  python3 tools/narration_blocks.py list [--min-rows N] [--top N] [--scene S]
  python3 tools/narration_blocks.py show <scene>#<pos>     # one block
  python3 tools/narration_blocks.py show --scene 438.0     # all blocks of a scene
  python3 tools/narration_blocks.py stats
"""
import argparse
import collections
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene_index as si

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENE_LINES = os.path.join(ROOT, 'data', 'scene_lines.tsv')

CTRL = re.compile(r'\[[0-9A-Fa-f]{4}\]|\\x[0-9A-Fa-f]{4}')
QUOTE = re.compile(r'[」『「]')
ASCII_ONLY = re.compile(r'^[\x00-\x7f“”…＋・]*$')
# System prompts sit in the same storage run as the location narration, so they
# must be recognised and cut out, or a whole save menu ends up inside a block.
SYSTEM_SUBSTR = re.compile(r'デ～タを記録|記録しますか|セ～ブしますか')
MENU_EXACT = {'記録する', 'しない', 'はい', 'いいえ', 'セーブ', 'ロード', '消去',
              '持ち物', '移動', '周囲', '見る', '話す', '捜索', 'タバコ吸う',
              'パートナ～', '攻撃する', '様子を見る', 'どうする！', '逃げる'}


def strip_ctrl(s):
    return CTRL.sub('', s)


def load_lines():
    """scene -> list of rows in story order (from scene_lines.tsv)."""
    scenes = collections.OrderedDict()
    kinds = {}
    for r in csv.DictReader(open(si.SCENES, encoding='utf-8'), delimiter='\t'):
        kinds[r['scene']] = r['kind']
    for r in csv.DictReader(open(SCENE_LINES, encoding='utf-8'), delimiter='\t'):
        scenes.setdefault(r['scene'], []).append(r)
    for scene in scenes:
        scenes[scene].sort(key=lambda r: int(r['pos']))
    return scenes, kinds


def is_narration(row, next_row, names):
    jp = strip_ctrl(row['jp_text']).strip()
    if QUOTE.search(jp):
        return False
    if ASCII_ONLY.match(jp):
        return False
    if SYSTEM_SUBSTR.search(jp) or jp in MENU_EXACT:
        return False
    if si.is_label(jp, strip_ctrl(next_row['jp_text']) if next_row else '', names):
        return False
    return True


def sentence_groups(rows, names):
    """Split a scene into sentence groups (lines joined by > / < markers).

    A display line of dialogue does not have to carry a quote marker itself -
    the marker sits on the first line of the sentence and the later lines just
    continue it.  So a line can only be called narration once its whole
    sentence group is known: if *any* line of the group is dialogue, every line
    of it is dialogue and none of it is narration.
    """
    groups = []
    cur = []
    for i, row in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if cur and '<' not in (row.get('join') or ''):
            groups.append(cur)
            cur = []
        cur.append((row, nxt))
    if cur:
        groups.append(cur)
    return groups


def group_is_narration(group, names):
    return all(is_narration(row, nxt, names) for row, nxt in group)


def blocks_of(scene, rows, names):
    out = []
    cur = []
    for group in sentence_groups(rows, names):
        if group_is_narration(group, names):
            cur.extend(r for r, _ in group)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def all_blocks(min_rows=1, min_chars=0):
    scenes, kinds = load_lines()
    names = si.load_names()
    out = []
    for scene, rows in scenes.items():
        if kinds.get(scene) != 'scene':
            continue
        for blk in blocks_of(scene, rows, names):
            chars = sum(len(strip_ctrl(r['jp_text'])) for r in blk)
            if len(blk) < min_rows or chars < min_chars:
                continue
            out.append({'scene': scene, 'pos': int(blk[0]['pos']), 'rows': blk,
                        'chars': chars})
    return out


def block_id(b):
    return f"{b['scene']}#{b['pos']}"


def join_text(blk, field):
    """Join a block the way the game displays it, inserting the join pause."""
    parts = []
    for r in blk:
        mark = r.get('join', '')
        txt = r[field]
        if parts and mark in ('<', '<>'):
            parts[-1] = parts[-1] + '⏎'
        parts.append(txt)
    return ''.join(parts)


def cmd_list(a):
    blks = all_blocks(a.min_rows, a.min_chars)
    if a.scene:
        blks = [b for b in blks if b['scene'] == a.scene]
    blks.sort(key=lambda b: -b['chars'])
    if a.top:
        blks = blks[:a.top]
    if a.order == 'scene':
        blks.sort(key=lambda b: (float(b['scene'].split('.')[0]),
                                 float(b['scene'].split('.')[1]), b['pos']))
    print('block\tscene\tentry\trows\tjp_chars\tcn_preview')
    for b in blks:
        first = b['rows'][0]
        prev = join_text(b['rows'], 'translation')[:40]
        print(f"{block_id(b)}\t{b['scene']}\t{first['entry']}\t{len(b['rows'])}\t"
              f"{b['chars']}\t{prev}")


def cmd_show(a):
    blks = all_blocks(1, 0)
    want = []
    if a.scene:
        want = [b for b in blks if b['scene'] == a.scene]
    else:
        ids = set(a.block)
        want = [b for b in blks if block_id(b) in ids]
    if not want:
        raise SystemExit('no such block')
    master = {(r['entry'], r['idx']): r for r in si.load_master()}
    for b in sorted(want, key=lambda b: (b['scene'], b['pos'])):
        print(f"# {block_id(b)}  scene={b['scene']}  entry={b['rows'][0]['entry']}"
              f"  idx {b['rows'][0]['idx']}..{b['rows'][-1]['idx']}"
              f"  rows={len(b['rows'])}")
        for r in b['rows']:
            m = master[(r['entry'], r['idx'])]
            print(f"  {r['pos']:>4} {'>' if r.get('join') else ' '} "
                  f"{m['n_codes']:>3} | {r['jp_text']}")
            print(f"       {'':>3} | {r['translation']}")
        print()


def cmd_stats(a):
    blks = all_blocks(1, 0)
    rows = sum(len(b['rows']) for b in blks)
    print(f'narration blocks: {len(blks)}')
    print(f'narration lines : {rows}')
    hist = collections.Counter(min(len(b['rows']), 5) for b in blks)
    print('block size histogram (rows): '
          + ', '.join(f'{k}{"+" if k == 5 else ""}rows={v}'
                      for k, v in sorted(hist.items())))
    top = sorted(blks, key=lambda b: -b['chars'])[:10]
    print('largest blocks:')
    for b in top:
        print(f"  {block_id(b):>10}  {b['chars']:>4} chars  {len(b['rows'])} rows")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    l = sub.add_parser('list')
    l.add_argument('--min-rows', type=int, default=1)
    l.add_argument('--min-chars', type=int, default=0)
    l.add_argument('--top', type=int, default=0)
    l.add_argument('--scene')
    l.add_argument('--order', choices=['size', 'scene'], default='size')
    l.set_defaults(fn=cmd_list)
    s = sub.add_parser('show')
    s.add_argument('block', nargs='*')
    s.add_argument('--scene')
    s.set_defaults(fn=cmd_show)
    st = sub.add_parser('stats')
    st.set_defaults(fn=cmd_stats)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
