#!/usr/bin/env python3
"""Find cross-line translation candidates; never change translations automatically.

Read locally contiguous display instructions from the ORIGINAL script, mapping
engine row indices to master rows by absolute offset. Split at pages, branches,
menus and unknown instructions. These static groups are review hints, not proof
of a runtime path. Retain every distinct group and every reuse site: a shared
suffix must be reviewed with all its prefixes before editing.
"""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import struct

import gbtext
from romdbg import entry_table
from script_vm import walk
from work_paths import output_file

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = {'page', 'page2', 'page80', 'page81', 'end', 'rowopt'}
CONTROL_OPS = {0x9E, 0x9F, 0x54, 0x52, 0x53, 0x40, 0x42, 0x43, 0x6F}
ALIASES = {
    '哲司': r'哲司|てつじ', '哲三': r'哲三',
    '徳子': r'徳子|とくちゃん', '悠': r'ゆう(?:ちゃん)?(?!べ)|悠',
    '中西': r'中西|なかにし', '貴之': r'貴之|たかゆき',
    '智美': r'智美|ともみ', '亜希': r'亜希|あきちゃん',
    '美恵': r'美恵|みえちゃん', '荒川': r'荒川|あらかわ',
    '春菜': r'春菜|はるな', '洋子': r'洋子|ようこ',
}
DUPLICATES = {
    '担心': r'心配', '觉得': r'思|気が', '认为': r'思',
    '应该': r'はず|だろう|でしょう|思', '为了': r'ため',
    '可能': r'可能|かもしれ|有り得|ありえ|そうな',
    '一直': r'ずっと|いつまでも', '正在': r'ている',
    '想要': r'たい|欲し',
}


def load_master(path):
    result = {}
    with Path(path).open(encoding='utf-8', newline='') as f:
        for r in csv.reader(f, delimiter='\t'):
            if len(r) < 7 or not r[0].isdigit():
                continue
            off = int(r[2]) + (0 if len(r) > 7 and r[7] == 'pool' else 0x7A0)
            result[int(r[0]), off] = dict(idx=int(r[1]), jp=r[5], cn=r[6])
    return result


def display_runs(events):
    """Conservative local runs; never glue text across a jump or menu."""
    rows, pcs, uncertain = [], [], False
    for event in list(events) + [('end', -1)]:
        kind = event[0]
        if kind in {'unknown', 'truncated', 'bail'}:
            uncertain = True
            continue
        boundary = kind in BOUNDARIES or (kind == 'op' and event[2] in CONTROL_OPS)
        if boundary:
            if rows and not uncertain and len(rows) <= 6 and len(set(rows)) == len(rows):
                yield rows, pcs
            rows, pcs = [], []
            # A page is the existing decoder's resynchronization boundary.
            if kind in BOUNDARIES:
                uncertain = False
        elif kind == 'row':
            rows.append(event[2])
            pcs.append(event[1])


def collect_groups(rom, master):
    entries = sorted({struct.unpack_from('<h', rom, 0x15C004 + i * 8)[0]
                      for i in range(509)})
    groups, counts = {}, Counter(entries=len(entries))
    for eid in entries:
        data = gbtext.load_entry(rom, eid)
        toff, offsets = entry_table(data)
        if toff is None or toff < 0x7A0 + 0x15:
            counts['unmapped_entries'] += 1
            continue
        events = walk(data[0x7A0:toff], nrows=len(offsets))
        for indices, pcs in display_runs(events):
            rows = [master.get((eid, 0x7A0 + offsets[i])) for i in indices]
            if any(r is None for r in rows):
                counts['unmapped_runs'] += 1
                continue
            counts['decoded_runs'] += 1
            key = (eid, tuple(r['idx'] for r in rows))
            if key not in groups:
                groups[key] = dict(entry=eid, rows=list(key[1]), engine_rows=indices,
                                   jp=[r['jp'] for r in rows], cn=[r['cn'] for r in rows], sites=[])
            groups[key]['sites'].append(pcs)
    counts['unique_groups'] = len(groups)
    return list(groups.values()), dict(counts)


def cn_name(name, text):
    return name in text or (name == '徳子' and '小徳' in text)


def flags_for(jp_lines, cn_lines):
    jp, cn = ''.join(jp_lines), ''.join(cn_lines)
    flags = set()
    for name, pattern in ALIASES.items():
        if re.search(pattern, jp) and not cn_name(name, cn):
            others = [n for n, p in ALIASES.items() if cn_name(n, cn) and not re.search(p, jp)]
            if others:
                flags.add('人物指代:' + name + '→' + ','.join(others))
    for a, b in zip(cn_lines, cn_lines[1:]):
        for word, source in DUPLICATES.items():
            # Repetition already present in Japanese is not a translation defect.
            if word in a and word in b and len(re.findall(source, jp)) < 2:
                flags.add('跨行重复:' + word)
    if re.search(r'知ら(?:ない|なかった|なさ|ぬ)|知りません|覚えていない', jp):
        # "Don't you know...?" can correctly become a positive Chinese question.
        if '！' not in jp and not re.search(r'不|没|无|未|忘', cn):
            flags.add('否定可能丢失:认知')
    for name, pattern in ALIASES.items():
        if re.search('(?:' + pattern + r')[＋』]*を探している', jp):
            names = r'(?:徳子|小徳)' if name == '徳子' else '(?:小?' + name + ')'
            if re.search(names + r'[＋』]*(?:正|正在)寻找', cn):
                flags.add('主客体疑似颠倒:寻找')
    return sorted(flags)


def shared_combinations(groups):
    uses = defaultdict(list)
    for g in groups:
        for row in g['rows']:
            uses[g['entry'], row].append(g)
    return {key: variants for key, variants in uses.items() if len(variants) > 1}


def audit_groups(groups, master):
    findings = []
    covered_roles = set()
    for group in groups:
        flags = flags_for(group['jp'], group['cn'])
        if flags:
            findings.append(dict(group, flags=flags, basis='static-display-run'))
            if any(f.startswith('主客体') for f in flags):
                covered_roles.update((group['entry'], row) for row in group['rows'])
    # This complete-clause rule is also safe on a single slot. Do not apply
    # negative/name checks here: their translation can move across line breaks.
    for (eid, offset), row in master.items():
        flags = [f for f in flags_for([row['jp']], [row['cn']]) if f.startswith('主客体')]
        if flags and (eid, row['idx']) not in covered_roles:
            findings.append(dict(entry=eid, rows=[row['idx']], engine_rows=[],
                                 jp=[row['jp']], cn=[row['cn']], sites=[], flags=flags,
                                 basis='single-complete-clause', offset=offset))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rom', type=Path, default=Path(gbtext.JP_ROM))
    ap.add_argument('--master', type=Path, default=ROOT / 'data/translation.tsv')
    args = ap.parse_args()
    master = load_master(args.master)
    groups, counts = collect_groups(args.rom.read_bytes(), master)
    findings = audit_groups(groups, master)
    report = dict(status='REVIEW_CANDIDATES_NOT_ERRORS',
                  master_sha256=hashlib.sha256(args.master.read_bytes()).hexdigest(),
                  counts=dict(counts, master_slots=len(master), flagged_groups=len(findings)), findings=findings,
                  note='静态局部显示组合；并非分支可达性或实际屏幕证明。只提示，不自动修改或阻断构建。')
    Path(output_file('reports/context-audit.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    with Path(output_file('reports/context-audit.tsv')).open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['entry', 'rows', 'flags', 'jp_context', 'cn_context', 'bytecode_sites'])
        for g in findings:
            w.writerow([g['entry'], ','.join(map(str, g['rows'])), ';'.join(g['flags']),
                        ' / '.join(g['jp']), ' / '.join(g['cn']), json.dumps(g['sites'])])
    with Path(output_file('reports/context-shared-lines.tsv')).open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['entry', 'shared_idx', 'combination', 'jp_context', 'cn_context'])
        for (eid, row), variants in shared_combinations(groups).items():
            for g in variants:
                w.writerow([eid, row, ','.join(map(str, g['rows'])), ' / '.join(g['jp']), ' / '.join(g['cn'])])
    print(json.dumps(report['counts'], ensure_ascii=False))
    print('仅人工复核候选；见 work/reports/context-audit.tsv 与 context-shared-lines.tsv')


if __name__ == '__main__':
    main()
