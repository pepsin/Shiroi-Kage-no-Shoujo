#!/usr/bin/env python3
"""Translation-memory helpers for data/translation.tsv.

Workflow
--------
  tm.py batches --size N --outdir work/tm/w01     -> batch input files
  ... translator writes  bNNN.out.tsv  (rid<TAB>cn) ...
  tm.py verify work/tm/w01/*.out.tsv              -> constraint report
  tm.py apply  work/tm/w01/*.out.tsv              -> writes data/translation.tsv
  tm.py check / status                            -> progress + glyph gaps

Constraints enforced (AGENT.md):
  * punctuation of the source is kept exactly, same order (nothing added,
    nothing dropped)
  * the translation never needs more codes than the original slot
  * no kana (except rows listed in KANA_OK_RIDS)
  * digits / latin letters preserved

The master TSV is edited field-by-field and written back with its original
line endings (the file is CRLF), so untouched rows stay byte-identical.
"""
import argparse
import collections
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
WORK = os.path.join(ROOT, 'work', 'tm')
BATCHDIR = os.path.join(WORK, 'batches')
MAPFILE = os.path.join(ROOT, 'data', 'glyph_map.csv')

HEADER = ['entry', 'idx', 'offset', 'n_codes', 'n_bytes', 'jp_text', 'translation']
IN_HEADER = ['rid', 'entry', 'idx', 'len', 'jp_text']

# Punctuation that must survive translation unchanged and in order.
# ～ (0x14) is the long-vowel mark inside katakana loanwords, so it is NOT
# mandatory: デ～タ -> 数据 is correct.  々 (0x12) is a script separator and is.
MUST_KEEP = set('、。，！？「」『』・：；“”‘’（）〈〉《》‥…□○■×＋＜＞％')
# The script uses 々 as a line-final trailing-off mark (not as a repetition
# mark - that is ー in this game); it is written as … in the translation.
CHAR_MAP = {'々': '…'}
# ...except inside the surname 佐々木, which keeps its real repetition mark.
PROTECT = ['佐々木']
KANA = re.compile(r'[\u3040-\u30ff\u31f0-\u31ff]')

# Rows allowed to keep kana in the output (names written in kana in the
# original).  Empty unless the project decides otherwise.
KANA_OK_RIDS = set()


def load_master():
    """Return (lines, rows); lines are raw file lines, line endings preserved."""
    with open(MASTER, encoding='utf-8', newline='') as f:
        raw = f.read()
    lines = raw.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    rows = []
    for i, line in enumerate(lines[1:], start=1):
        body = line[:-1] if line.endswith('\r') else line
        p = body.split('\t')
        while len(p) < 7:
            p.append('')
        rows.append({'line': i, 'entry': p[0], 'idx': p[1], 'offset': p[2],
                     'n_codes': int(p[3] or 0), 'n_bytes': p[4], 'jp_text': p[5],
                     'translation': p[6], 'kind': p[7] if len(p) > 7 else ''})
    return lines, rows


def set_field(line, idx, value):
    """Replace one tab-separated field, preserving the line terminator."""
    cr = '\r' if line.endswith('\r') else ''
    body = line[:-1] if cr else line
    p = body.split('\t')
    while len(p) <= idx:
        p.append('')
    p[idx] = value
    return '\t'.join(p) + cr


def rid_of(entry, idx, kind=''):
    """Row id.  Pool rows share their idx numbering with the offset-table rows
    of the same entry, so they get a distinct prefix."""
    return f'{entry}:p{idx}' if kind == 'pool' else f'{entry}:{idx}'


def glyph_set():
    with open(MAPFILE, encoding='utf-8') as f:
        return {r['char'] for r in csv.DictReader(f)} - {''}


def punct_skeleton(s):
    for prot in PROTECT:                     # 佐々木 keeps its real 々
        s = s.replace(prot, '\x00' * len(prot))
    return ''.join(CHAR_MAP.get(ch, ch) for ch in s if CHAR_MAP.get(ch, ch) in MUST_KEEP)


def alnum_skeleton(s):
    return ''.join(ch for ch in s if ch.isascii() and ch.isalnum())


def check_pair(src, cn, rid=''):
    """Return a list of violation strings for one source/translation pair."""
    errs = []
    if not cn:
        return errs
    if '\t' in cn or '\n' in cn or '\r' in cn:
        errs.append('contains TAB/CR/LF')
    sp, cp = punct_skeleton(src), punct_skeleton(cn)
    if sp != cp:
        # a kana surname restored to kanji may legitimately introduce 々
        # (ささき -> 佐々木); anything else is a real mismatch.
        extra = collections.Counter(cp) - collections.Counter(sp)
        if not (KANA.search(src) and set(extra) <= {'\u3005'}):
            errs.append(f'punctuation {sp!r} -> {cp!r}')
    # digits / latin letters may be written as Chinese numerals in prose
    # (2箱 -> 两盒); the skeleton is only reported by `check`.
    # ー (0x13) is the game's repetition mark (佐ー木 = 佐々木, 人ー = 人々):
    # it must disappear in normal words but stays inside personal names.
    # ー is the game's repetition mark; ・ is punctuation (in MUST_KEEP)
    kana = set(KANA.findall(cn)) - {'\u30fc', '\u30fb'}
    if kana and rid not in KANA_OK_RIDS:
        errs.append('kana left: ' + ''.join(sorted(kana)))
    if len(cn) > len(src):
        errs.append(f'too long: {len(cn)} > {len(src)}')
    return errs


def cmd_batches(a):
    _, rows = load_master()
    outdir = a.outdir or BATCHDIR
    os.makedirs(outdir, exist_ok=True)
    sel = [r for r in rows if not r['translation'].strip()]
    if a.entry:
        want = set(a.entry)
        sel = [r for r in sel if r['entry'] in want]
    if a.min_entry:
        sel = [r for r in sel if int(r['entry']) >= a.min_entry]
    if a.skip_entry:
        skip = set(a.skip_entry)
        sel = [r for r in sel if r['entry'] not in skip]
    if a.limit:
        sel = sel[:a.limit]
    batches = []
    cur = []
    for r in sel:
        if cur and (len(cur) >= a.size or (a.by_entry and r['entry'] != cur[-1]['entry'])):
            batches.append(cur)
            cur = []
        cur.append(r)
    if cur:
        batches.append(cur)
    for f in os.listdir(outdir):
        if f.startswith('b') and f.endswith('.tsv') and '.out.' not in f:
            os.remove(os.path.join(outdir, f))
    for n, b in enumerate(batches):
        path = os.path.join(outdir, f'b{n:03d}.tsv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\t'.join(IN_HEADER) + '\n')
            for r in b:
                f.write('\t'.join([rid_of(r['entry'], r['idx']), r['entry'], r['idx'],
                                   str(r['n_codes']), r['jp_text']]) + '\n')
    print(f'{len(sel)} untranslated rows -> {len(batches)} batches in {outdir}')
    for n, b in enumerate(batches):
        ents = sorted({r['entry'] for r in b}, key=int)
        print(f'  b{n:03d}: {len(b)} rows, entries {ents[0]}..{ents[-1]}')


def read_batch(path):
    """Read a batch output; accepts the compact (rid, cn) layout as well as the
    legacy full-row one.  Returns [(rid, cn, jp_text_or_None)]."""
    out = []
    with open(path, encoding='utf-8') as f:
        r = csv.DictReader(f, delimiter='\t')
        fields = r.fieldnames or []
        if 'rid' not in fields:
            raise ValueError(f'no rid column (fields={fields})')
        for row in r:
            out.append((row['rid'], row.get('cn', '') or '', row.get('jp_text')))
    return out


def cmd_verify(a):
    _, rows = load_master()
    src = {rid_of(r['entry'], r['idx'], r['kind']): r['jp_text'] for r in rows}
    ok_all = True
    for path in a.files:
        try:
            got = read_batch(path)
        except Exception as e:
            print(f'{path}: CANNOT READ ({e})')
            ok_all = False
            continue
        seen = {}
        mis = []
        for rid, cn, jp in got:
            if rid not in src:
                mis.append(f'unknown rid {rid}')
                continue
            if jp and jp != src[rid]:
                mis.append(f'{rid}: jp_text differs')
            if rid in seen:
                mis.append(f'{rid}: duplicate row')
            seen[rid] = cn
        empty = [k for k, v in seen.items() if not v.strip()]
        viol = []
        for rid, cn, _ in got:
            if rid in src and cn.strip():
                for e in check_pair(src[rid], cn, rid):
                    viol.append(f'{rid} {src[rid]!r} -> {cn!r} [{e}]')
        status = 'OK' if not mis and not empty and not viol else 'PROBLEM'
        if status != 'OK':
            ok_all = False
        print(f'{os.path.basename(path)}: {len(got)} rows, {len(seen)} rids, '
              f'{len(empty)} empty cn, {len(mis)} structural, {len(viol)} violations -> {status}')
        for m in mis[:5]:
            print(f'   ! {m}')
        for v in viol[:16]:
            print(f'   ~ {v}')
    sys.exit(0 if ok_all else 1)


def cmd_apply(a):
    lines, rows = load_master()
    by_rid = {rid_of(r['entry'], r['idx'], r['kind']): r for r in rows}
    src_by_rid = {rid_of(r['entry'], r['idx'], r['kind']): r['jp_text'] for r in rows}
    tm = {}
    conflicts = []
    rejected = []
    for path in a.files:
        for rid, cn, _jp in read_batch(path):
            cn = cn.strip()
            if not cn:
                continue
            src = src_by_rid.get(rid)
            if src is None:
                print(f'  {path}: unknown rid {rid}')
                continue
            errs = check_pair(src, cn, rid)
            if errs and not a.force:
                rejected.append((rid, src, cn, errs))
                continue
            if rid in tm and tm[rid] != cn:
                conflicts.append((rid, tm[rid], cn))
                continue
            tm[rid] = cn
    n = 0
    for rid, cn in tm.items():
        row = by_rid[rid]
        if row['translation'] != cn:
            lines[row['line']] = set_field(lines[row['line']], 6, cn)
            n += 1
    # duplicate sources get the same translation (translation memory)
    by_src_first = {}
    for r in rows:
        if r['translation']:
            by_src_first.setdefault(r['jp_text'], r['translation'])
    for rid, cn in tm.items():
        by_src_first.setdefault(src_by_rid[rid], cn)
    prop = 0
    if not a.no_propagate:
        for r in rows:
            if r['translation']:
                continue
            cn = by_src_first.get(r['jp_text'])
            if cn:
                lines[r['line']] = set_field(lines[r['line']], 6, cn)
                r['translation'] = cn
                prop += 1
    with open(MASTER, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'filled {n} rows, propagated {prop} duplicates; conflicts: {len(conflicts)}')
    for rid, x, y in conflicts[:10]:
        print(f'  conflict {rid}: {x!r} vs {y!r}')
    if rejected:
        os.makedirs(WORK, exist_ok=True)
        fix = os.path.join(WORK, 'needs_fix.tsv')
        with open(fix, 'w', encoding='utf-8') as f:
            f.write('rid\tlen\tjp_text\tcn\tproblem\n')
            for rid, src, cn, errs in rejected:
                f.write(f'{rid}\t{len(src)}\t{src}\t{cn}\t{"; ".join(errs)}\n')
        print(f'REJECTED {len(rejected)} rows (kept empty, queued in {fix}):')
        for rid, src, cn, errs in rejected[:10]:
            print(f'  {rid} {src!r} -> {cn!r} [{"; ".join(errs)}]')


# Project-wide term normalisation, re-applied after every apply so that
# batches translated at different times cannot drift apart.
NORM_RULES = [
    (('かすみ', 'バ～'), [('酒吧香澄', '香澄酒吧'), ('霞', '香澄')]),
    (('由香子', 'ゆかこ'), [('由佳子', '由香子')]),
    (('荒川', '聡'), [('敏', '聡')]),
    (('メゾン',), [('梅松西新宿', '西新宿公寓'), ('Maison西新宿', '西新宿公寓')]),
    # 〜君 (くん) is a boss-to-subordinate honorific; Chinese has no equivalent,
    # so it is dropped (洋子君 -> 洋子).
    (('君',), [('洋子君', '洋子'), ('貴之君', '貴之'), ('拓朗君', '拓朗'),
               ('神宮寺君', '神宮寺'), ('知也君', '知也'), ('春菜君', '春菜'),
               ('拓也君', '拓也'), ('拓海君', '拓海'), ('美鈴君', '美鈴')]),
    # the girl ゆう is written 悠 in the script itself (e279 悠を探していた)
    # ('悠' never collides with 悠然 etc. here: ゆっくり is translated 悠闲)
    # -- handled in ALWAYS_SUB below
    # 洋子's surname is 御苑 in the script (the kana profile page guessed 美園)
    (('みその', '御苑'), [('美園', '御苑')]),
    # とくちゃん is 安田徳子's nickname (-> 小徳), シゲ is 中西茂's (-> 阿茂);
    # keep the two apart by looking at the source line.
    (('とくちゃん',), [('阿茂', '小徳')]),
    # 安田徳子 / とくちゃん: keep the Japanese form 徳, one nickname spelling
    (('徳子', 'とくちゃん'), [('小徳子', '小徳'), ('德子', '徳子'), ('小德', '小徳')]),
]
NORM_FIXED = {'42:14': '天沼香澄。我是真奈美，'}
# name + 君 (honorific) -> name; never collides with the word 君子
ALWAYS_SUB = [('優', '悠'), ('洋子君', '洋子'), ('貴之君', '貴之'), ('拓朗君', '拓朗'),
              ('神宮寺君', '神宮寺'), ('知也君', '知也'), ('春菜君', '春菜'),
              ('隆君', '隆'), ('拓也君', '拓也'), ('拓海君', '拓海'), ('美鈴君', '美鈴')]


def cmd_set(a):
    """Apply explicit rid->cn corrections from a TSV (rid, cn)."""
    lines, rows = load_master()
    by_rid = {rid_of(r['entry'], r['idx']): r for r in rows}
    n = 0
    for path in a.files:
        for rid, cn, _jp in read_batch(path):
            cn = cn.strip()
            row = by_rid.get(rid)
            if row is None:
                print(f'  {path}: unknown rid {rid}')
                continue
            errs = check_pair(row['jp_text'], cn, rid)
            if errs:
                print(f'  {rid}: REFUSED [{"; ".join(errs)}]')
                continue
            if row['translation'] != cn:
                lines[row['line']] = set_field(lines[row['line']], 6, cn)
                n += 1
    with open(MASTER, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'set {n} rows')


def cmd_norm(a):
    lines, rows = load_master()
    n = 0
    for r in rows:
        cn = r['translation']
        if not cn:
            continue
        rid = rid_of(r['entry'], r['idx'])
        new = NORM_FIXED.get(rid, cn)
        # 貴之君 -> 小貴之: the boy is addressed affectionately
        # (but not in 貴之君達 "貴之 and the others", which must stay short)
        if '貴之君達' in r['jp_text']:
            new = new.replace('小貴之', '貴之')
        elif '貴之君' in r['jp_text'] and '小貴之' not in new:
            new = new.replace('貴之', '小貴之')
        if '君子' not in new:
            for a_, b_ in ALWAYS_SUB:
                new = new.replace(a_, b_)
        if '\u3005' in r['jp_text']:
            for k, prot in enumerate(PROTECT):
                new = new.replace(prot, f'\x00{k}\x00')
            new = new.replace('\u3005', '\u2026')
            for k, prot in enumerate(PROTECT):
                new = new.replace(f'\x00{k}\x00', prot)
        for keys, subs in NORM_RULES:
            if any(k in r['jp_text'] for k in keys):
                for a_, b_ in subs:
                    new = new.replace(a_, b_)
        if new != cn:
            lines[r['line']] = set_field(lines[r['line']], 6, new)
            n += 1
    with open(MASTER, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'normalised {n} rows')


def cmd_check(a):
    _, rows = load_master()
    have = glyph_set()
    bad = collections.Counter()
    missing = collections.Counter()
    examples = collections.defaultdict(list)
    ratio = collections.Counter()
    translated = 0
    for r in rows:
        cn = r['translation'].strip()
        if not cn:
            continue
        translated += 1
        rid = rid_of(r['entry'], r['idx'], r['kind'])
        for e in check_pair(r['jp_text'], cn, rid):
            key = e.split(':')[0].split(' ')[0]
            bad[key] += 1
            if len(examples[key]) < 8:
                examples[key].append(f'{rid}: {r["jp_text"]!r} -> {cn!r} [{e}]')
        for ch in cn:
            if ch not in have and not ch.isspace():
                missing[ch] += 1
        ratio[round(len(cn) / max(len(r['jp_text']), 1), 1)] += 1
    print(f'translated rows: {translated} / {len(rows)}')
    print(f'violations: {sum(bad.values())}')
    for k, v in bad.most_common():
        print(f'  {k}: {v}')
        for ex in examples[k]:
            print(f'    {ex}')
    print(f'distinct chars missing from font: {len(missing)}')
    os.makedirs(WORK, exist_ok=True)
    out = os.path.join(WORK, 'missing_glyphs.txt')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(''.join(sorted(missing, key=lambda c: -missing[c])))
    print(f'wrote {out}')
    dash = [f'{rid_of(r["entry"], r["idx"], r["kind"])} {r["jp_text"]!r} -> {r["translation"]!r}'
            for r in rows if '\u30fc' in r['translation']]
    print(f'rows whose translation still contains ー: {len(dash)}')
    with open(os.path.join(WORK, 'dash_rows.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(dash))
    print('length ratio (cn/jp) histogram:')
    for k in sorted(ratio):
        print(f'  {k:.1f}: {"#" * min(60, ratio[k] // max(1, translated // 400))} {ratio[k]}')


def cmd_status(a):
    _, rows = load_master()
    done = [r for r in rows if r['translation'].strip()]
    print(f'{len(done)} / {len(rows)} rows translated ({len(done) / len(rows):.1%})')
    byent = collections.Counter()
    tot = collections.Counter()
    for r in rows:
        tot[r['entry']] += 1
        if r['translation'].strip():
            byent[r['entry']] += 1
    rem = [(tot[e] - byent[e], e) for e in tot]
    rem.sort(reverse=True)
    print('entries with work left:', sum(1 for n, _ in rem if n))
    print('  ' + ', '.join(f'e{e}({n})' for n, e in rem[:20] if n))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('batches')
    b.add_argument('--size', type=int, default=200)
    b.add_argument('--entry', action='append', default=[])
    b.add_argument('--by-entry', action='store_true', default=True)
    b.add_argument('--no-by-entry', action='store_false', dest='by_entry')
    b.add_argument('--limit', type=int, default=0)
    b.add_argument('--min-entry', type=int, default=0)
    b.add_argument('--skip-entry', action='append', default=[])
    b.add_argument('--outdir', default='')
    b.set_defaults(func=cmd_batches)
    p = sub.add_parser('apply')
    p.add_argument('files', nargs='+')
    p.add_argument('--no-propagate', action='store_true')
    p.add_argument('--force', action='store_true')
    p.set_defaults(func=cmd_apply)
    v = sub.add_parser('verify')
    v.add_argument('files', nargs='+')
    v.set_defaults(func=cmd_verify)
    st = sub.add_parser('set')
    st.add_argument('files', nargs='+')
    st.set_defaults(func=cmd_set)
    n = sub.add_parser('norm')
    n.set_defaults(func=cmd_norm)
    c = sub.add_parser('check')
    c.set_defaults(func=cmd_check)
    s = sub.add_parser('status')
    s.set_defaults(func=cmd_status)
    a = ap.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()
