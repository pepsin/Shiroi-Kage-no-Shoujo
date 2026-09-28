#!/usr/bin/env python3
"""Rebuild / check data/scene_worklist.tsv (scene refinement coverage).

A scene can be in one of four states:

    done      its entry appears in at least one patch file under work/scenes/
              (i.e. at least one line of that scene was actually rewritten)
    reviewed  it is listed in data/scene_reviewed.tsv: read line by line
              against the Japanese, judged to need no change
    todo      story scene that has not been looked at yet
    n/a       not a story scene (system prompt / quiz / character data / credits)

The two "looked at" states are kept apart on purpose: `done` means the wording
changed, `reviewed` means it was read and left alone.  Coverage of the story is
`done + reviewed` over all story scenes; the point of the ledger is that closing
coverage does not silently mean "every scene was rewritten".

Usage:
  worklist.py build [--out data/scene_worklist.tsv]
  worklist.py check
"""
import argparse
import csv
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENES = os.path.join(ROOT, 'data', 'scenes.tsv')
REVIEWED = os.path.join(ROOT, 'data', 'scene_reviewed.tsv')
DEFAULT_OUT = os.path.join(ROOT, 'data', 'scene_worklist.tsv')
PATCH_GLOBS = [
    os.path.join(ROOT, 'work', 'scenes', 'patch*.tsv'),
    os.path.join(ROOT, 'work', 'tm', 'w*', '*.out.tsv'),
    os.path.join(ROOT, 'work', 'batches', '*.clean.tsv'),
]


def read_scenes():
    with open(SCENES, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def read_reviewed():
    """scene -> note, from data/scene_reviewed.tsv."""
    out = {}
    if not os.path.exists(REVIEWED):
        return out
    with open(REVIEWED, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n').rstrip('\r')
            if not line or line.startswith('#') or line.startswith('scene\t'):
                continue
            parts = line.split('\t')
            out[parts[0]] = parts[2] if len(parts) > 2 else ''
    return out


def touched_entries():
    """Every entry whose translation some patch file rewrites."""
    done = set()
    for pat in PATCH_GLOBS:
        for path in glob.glob(pat):
            try:
                with open(path, encoding='utf-8') as f:
                    for line in f:
                        if line.startswith('#'):
                            continue
                        parts = line.rstrip('\n').split('\t')
                        if len(parts) >= 2 and parts[0].isdigit():
                            done.add(parts[0])
            except OSError:
                pass
    return done


def classify(rows, done, reviewed):
    out = []
    for s in rows:
        if s['kind'] != 'scene':
            status = 'n/a'
        elif s['entry'] in done:
            status = 'done'
        elif s['scene'] in reviewed:
            status = 'reviewed'
        else:
            status = 'todo'
        out.append((s['scene'], s['entry'], s['rows'], status))
    return out


def build(a):
    rows = classify(read_scenes(), touched_entries(), read_reviewed())
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        f.write('scene\tentry\trows\tstatus\n')
        for scene, entry, n, status in rows:
            f.write(f'{scene}\t{entry}\t{n}\t{status}\n')
    story = [r for r in rows if r[3] != 'n/a']
    done = [r for r in story if r[3] == 'done']
    rev = [r for r in story if r[3] == 'reviewed']
    todo = [r for r in story if r[3] == 'todo']
    print(f'wrote {a.out}')
    print(f'story scenes: {len(story)}  done: {len(done)}  '
          f'reviewed: {len(rev)}  todo: {len(todo)}')
    if story:
        print(f'coverage (done+reviewed): {len(done) + len(rev)}/{len(story)} '
              f'= {100 * (len(done) + len(rev)) / len(story):.1f}%')
    for scene, entry, n, _ in todo:
        print(f'  todo {scene} ({n} rows)')


def check(a):
    rows = classify(read_scenes(), touched_entries(), read_reviewed())
    todo = [r for r in rows if r[3] == 'todo']
    on_disk = []
    if os.path.exists(DEFAULT_OUT):
        with open(DEFAULT_OUT, encoding='utf-8') as f:
            on_disk = [tuple(l.rstrip('\n').split('\t'))
                       for l in f if l.strip() and not l.startswith('scene\t')]
    mismatch = [a_ for a_, b_ in zip(rows, on_disk) if a_ != b_]
    print(f'scenes: {len(rows)}; todo: {len(todo)}; '
          f'rows out of sync with {DEFAULT_OUT}: {len(mismatch)}')
    if mismatch:
        for a_, b_ in mismatch[:10]:
            print(f'  {a_}  !=  {b_}')
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--out', default=DEFAULT_OUT)
    b.set_defaults(fn=build)
    c = sub.add_parser('check')
    c.set_defaults(fn=check)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == '__main__':
    main()
