#!/usr/bin/env python3
"""Rank translated rows by how obviously broken they are.

The 2025-era machine pass translated each display line on its own, which shows up
as a handful of *mechanical* defects that can be spotted without understanding
the sentence:

  untranslated  the Chinese still holds kana (or equals the Japanese)
  neg           Japanese says ない/ません/ず/ぬ but the Chinese has no 不/没/无/…
  neg+          the reverse: Chinese negates a Japanese line that does not
  number        the ASCII digits differ between the two
  short         Chinese is under 55% of the Japanese length (content dropped)
  punct         the leading/trailing punctuation does not match the Japanese
  stray         leftover Japanese marks (～ ＋ －) or ASCII the Japanese lacks
  calque        a phrase that is a straight calque of a common Japanese pattern
  over          longer than the row's own code budget (import keeps Japanese then)

Everything else - word order, tone, whether a sentence reads well - needs a human
eye, so this only produces a worklist; tools/apply_cn_patch.py writes the fixes.

Usage:
  qa_translation.py [--table data/translation.tsv] [--out work/qa.tsv]
                    [--entry 3] [--limit 200] [--summary]
"""
import argparse
import collections
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KANA = re.compile(r'[\u3041-\u309F\u30A0-\u30FA\u30FC]')
NEG_JP = re.compile(r'(ない|ません|なか|ず|ぬ|まい|なし|無い|駄目|だめ|無理|嫌|不可|禁止|中止|やめ|よせ)')
# Only negation *constructions*: a bare 别/无 matches 别人/无法 and would make the
# "Chinese negates a positive Japanese line" check uselessly noisy.
NEG_CN = re.compile(r'(没有|没能|无法|不能|不会|不是|不要|不曾|并未|尚未|毫无|不用|不必|'
                    r'不想|不知|不了|不像|不对|不行|不可|不已|不足|不论|不管)')
NEG_CN_WIDE = re.compile(r'(不|没|无|別|别|莫|未|否|勿)')
CALQUE = ['的表情', '的存在', '的场合', '的样子', '之类的', '所谓的', '进行',
          '有着', '不得不', '～']
LEAD = '、。」『「“·，！？'
TAIL = '、。」『「，！？'


def marks(s):
    lead = s[0] if s and s[0] in LEAD else ''
    tail = s[-1] if s and s[-1] in TAIL else ''
    return lead, tail


def digits(s):
    return ''.join(sorted(re.findall(r'[0-9]', s)))


def check(jp, cn, budget):
    why = []
    if not cn.strip():
        why.append('empty')
    if cn == jp and KANA.search(jp):
        why.append('same')          # kana in the Japanese and identical Chinese
    elif KANA.search(cn):
        why.append('kana')          # leftovers, but often a kept proper name
    if NEG_JP.search(jp) and not NEG_CN_WIDE.search(cn):
        why.append('neg')
    if NEG_CN.search(cn) and not NEG_JP.search(jp):
        why.append('neg+')
    if digits(jp) != digits(cn) and digits(jp):
        why.append('number')
    if jp and len(cn) < 0.45 * len(jp):
        why.append('short')
    if marks(jp) != marks(cn):
        why.append('punct')
    for tok in ('～', '＋', '－'):
        if tok in cn and tok not in jp:
            why.append('stray')
            break
    if re.search(r'[A-Za-z]', cn) and not re.search(r'[A-Za-z]', jp):
        why.append('stray')
    for pat in CALQUE:
        if pat in cn:
            why.append('calque')
            break
    if len(cn) > budget:
        why.append('over')
    return why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'work', 'qa.tsv'))
    ap.add_argument('--entry')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--summary', action='store_true')
    a = ap.parse_args()

    hits = []
    total = 0
    with open(a.table, encoding='utf-8', newline='') as f:
        for row in csv.reader(f, delimiter='\t'):
            if len(row) < 7 or not row[0].isdigit():
                continue
            total += 1
            if a.entry and row[0] != a.entry:
                continue
            why = check(row[5], row[6], int(row[3]))
            if why:
                hits.append((row[0], row[1], ','.join(why), row[5], row[6], row[3]))

    kinds = collections.Counter()
    for _e, _i, w, _j, _c, _b in hits:
        for k in w.split(','):
            kinds[k] += 1
    print(f'rows scanned: {total}, flagged: {len(hits)} ({100.0 * len(hits) / max(1, total):.1f}%)')
    for k, n in kinds.most_common():
        print(f'  {k:<13} {n}')
    if a.summary:
        return 0

    if a.limit:
        hits = hits[:a.limit]
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        for e, i, w, jp, cn, b in hits:
            f.write(f'{e}\t{i}\t{w}\t{b}\t{jp}\t{cn}\n')
    print(f'wrote {a.out} ({len(hits)} rows)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
