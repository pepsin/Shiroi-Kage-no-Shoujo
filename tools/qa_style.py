#!/usr/bin/env python3
"""One-shot style regression check for the whole translation table.

Every rule here was learned from a real defect found during the 2025 refinement
passes (see ``docs/翻译作业流程.md`` 6.2 / 6.3 and ``docs/打包流程.md`` 14.x).
``style_lint.py`` ranks *scenes* for the next pass; this tool is the opposite:
it answers "did any of the already-fixed anti-patterns come back?" in one run.

Rules
-----
calque     关于 / 进行 / 有着 / 所谓的 / 的存在 / 对于   (Japanese-Chinese glue)
passive    被 + verb + 了/到/着   (Japanese 受身 copied, roughly 150 rows)
negation   JP has ない/ません but CN has no negation word
verb-tail  a unit whose Chinese *ends* on a bare verb while JP ends on an object
kana       kana left in the translation (names should be restored to kanji)
punct      punctuation skeleton differs from the source (tm.py already checks)

Usage
-----
  qa_style.py                 # summary table
  qa_style.py --list calque   # show the matching rows
  qa_style.py --limit 40      # cap the examples printed
  qa_style.py --json work/qa_style.json
"""
import argparse
import collections
import csv
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import tm  # noqa: E402  (check_pair / slots_used)

MASTER = os.path.join(ROOT, 'data', 'translation.tsv')

RULES = [
    ('calque', re.compile(r'(关于|进行|有着|所谓的|的存在|对于)')),
    ('passive', re.compile(r'被[^，。！？『」“]{0,8}[了到着]')),
    ('kana', re.compile(r'[\u3041-\u309F\u30A0-\u30FA]')),
    ('verb-tail', re.compile(r'[^，。！？『」“]{0,4}(停住|停下|走来|过来|出来|下来|'
                             r'上来|进入|走向|看去|浮现|退去|笼罩|蔓延|铺开)$')),
]
NEG_JP = re.compile(r'(ない|ません|なかった|ぬ|ず|まい)')
NEG_CN = re.compile(r'(不|没|别|未|无|非|怎么|难道|岂|莫)')

# 已经确认"译得自然、不要改"的固定说法，命中即跳过
ALLOW = {
    '的样子': {'看他们的样子', '那副慌张的样子', '一直在打听的样子'},
    '关于': set(),
}


def load():
    rows = []
    with open(MASTER, encoding='utf-8', newline='') as f:
        for r in csv.reader(f, delimiter='\t'):
            if len(r) >= 7 and r[0].isdigit():
                rows.append(r)
    return rows


def scan(rows):
    hits = collections.defaultdict(list)
    for r in rows:
        e, i, jp, cn = r[0], r[1], r[5], r[6]
        for name, pat in RULES:
            if name == 'verb-tail':
                if '>' in (r[5] and ''):      # placeholder, unit rule handled below
                    continue
                continue
            m = pat.search(cn)
            if not m:
                continue
            if name == 'calque':
                # allow the fixed 的样子 family and 关于+verb structures
                body = cn
                if '的样子' in body and any(a in body for a in ALLOW['的样子']):
                    if not re.search(r'(关于|进行|有着|所谓的|的存在|对于)', body):
                        continue
            hits[name].append({'entry': e, 'idx': i, 'jp': jp, 'cn': cn,
                               'match': m.group(0)})
    return hits


CLOSERS = '。，！？『』“'


def units(rows):
    """Group display lines back into sentence units.

    The game splits one sentence across lines, so a negation may sit on line 1
    and its Chinese counterpart on line 2.  Checking line by line produced
    3,240 false hits; checking the unit produced the ones that matter.
    """
    out = []
    cur = []
    for r in rows:
        cur.append(r)
        if r[5] and r[5][-1] in CLOSERS:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


# 只查"否定义丢了就读反"的高置信句式。日语里 ない／ず 有大量对应中文肯定式的
# 惯用表达（すまない→抱歉、いつも→平时、しか…ない→只有…才、はず／かもしれない
# →应该／也许），全量扫描会产生 2,000+ 误报，故按实测收敛到下列动词。
HARD_NEG = re.compile(r'(見つからな|できな|わからな|言えな|聞こえな|見えな|'
                      r'思えな|なれな|しなか|来なか|いなか)')


def unit_negation(rows):
    hits = []
    for u in units(rows):
        jp = ''.join(r[5] for r in u)
        cn = ''.join(r[6] for r in u)
        if HARD_NEG.search(jp) and not NEG_CN.search(cn):
            hits.append({'entry': u[0][0], 'idx': u[0][1], 'jp': jp, 'cn': cn,
                         'match': 'hard-neg'})
    return hits


def unit_verb_tail(rows):
    """A sentence unit that ends on a bare verb while the Japanese line ends on
    an object/particle: that is the cross-line inversion the rules forbid."""
    by_entry = collections.defaultdict(list)
    for r in rows:
        by_entry[r[0]].append(r)
    out = []
    jp_end = re.compile(r'(を|に|が|は|と|で|へ|も)[」『“]*$')
    cn_verb = RULES[3][1]
    for e, rs in by_entry.items():
        for k, r in enumerate(rs):
            if k + 1 < len(rs) and rs[k + 1][5][:1] and not jp_end.search(r[5]):
                continue
            if jp_end.search(r[5]) and cn_verb.search(r[6].rstrip('，。！？『」“')):
                out.append({'entry': e, 'idx': r[1], 'jp': r[5], 'cn': r[6],
                            'match': 'unit'})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', dest='show', default='')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--json', default='')
    a = ap.parse_args()

    rows = load()
    hits = scan(rows)
    hits['verb-tail-unit'] = unit_verb_tail(rows)
    hits['negation-unit'] = unit_negation(rows)

    names = ['calque', 'passive', 'negation-unit', 'verb-tail-unit', 'kana']
    print(f'rows: {len(rows)}')
    print(f"{'rule':<16}{'hits':>7}")
    for n in names:
        print(f'{n:<16}{len(hits.get(n, [])):>7}')

    if a.show:
        for h in hits.get(a.show, [])[:a.limit]:
            print(f"  e{h['entry']}[{h['idx']}] {h['jp'][:30]!r} -> {h['cn'][:34]!r}")
    if a.json:
        os.makedirs(os.path.dirname(a.json) or '.', exist_ok=True)
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump({k: v for k, v in hits.items()}, f, ensure_ascii=False, indent=1)
        print(f'wrote {a.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
