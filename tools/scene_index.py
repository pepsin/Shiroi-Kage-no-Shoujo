#!/usr/bin/env python3
"""Scene-progression index for data/translation.tsv.

Why this exists
---------------
The game splits one sentence into several display lines and stores them
per entry, but the master TSV is ordered by **storage offset**, which is not
always the story order:

  * e1 (the prologue) is a pure pool entry whose scan order is
    108..161 (the funeral scene) / 0..161 / 203..226 (Yoko / items), so reading
    the file top to bottom jumps between three different parts of the scene.
  * later entries start with a system block (save prompt / menu) that the game
    keeps in front of the scene even though it is not part of the story.
  * a single entry can hold several scenes (``俺達は…へとやって来た，``
    re-opens one) and several chapters share one entry (e80/e126/e173/e215 are
    the same 990-row text).

Translating line by line - the way the first machine pass did - loses all of
that: pronouns, tense and tone drift inside one conversation, and the Chinese
copies the Japanese verb-final word order because the line ends before the
verb.  This tool rebuilds the *story* order into scenes so a reviewer gets a
whole conversation at once, and so the style rules in
``data/translation_rules.md`` (no inversion, J-C grammar, hard-boiled tone) can
be applied to a coherent unit instead of to a fragment.

What it produces
----------------
``data/scenes.tsv``   one row per scene (id, kind, entry, index range, location,
                      speakers, line count)
``data/scene_lines.tsv``  one row per master row: rid -> scene / position /
                      sentence-continuation flags

Usage
-----
  python3 tools/scene_index.py build                 # rebuild both files
  python3 tools/scene_index.py build --report        # + summary tables
  python3 tools/scene_index.py list --kind story     # scene catalogue
  python3 tools/scene_index.py show s1.1             # read one scene end to end
  python3 tools/scene_index.py check                 # integrity: coverage, order

``show`` prints the scene in story order with the Japanese and Chinese of every
display line side by side - that is the review unit the style pass works on.
"""
import argparse
import collections
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
GLOSSARY = os.path.join(ROOT, 'data', 'glossary.tsv')
SCENES = os.path.join(ROOT, 'data', 'scenes.tsv')
LINES = os.path.join(ROOT, 'data', 'scene_lines.tsv')

HEADER = ['entry', 'idx', 'offset', 'n_codes', 'n_bytes', 'jp_text', 'translation']
SCENE_HEADER = ['scene', 'kind', 'entry', 'idx_from', 'idx_to', 'rows', 'codes',
                'location', 'speakers', 'note']
LINE_HEADER = ['rid', 'entry', 'idx', 'scene', 'pos', 'jp_text', 'translation']

# ---------------------------------------------------------------- entry kinds
# Numeric ranges are the game's own layout: the main story first, then the
# question/interrogation section (its lines come in {question, 洋子版, 神宮寺版}
# triples), then the trivia/credits pages.
QUIZ_RANGE = (282, 417)
CHARA_RANGE = (418, 431)
TRIVIA_RANGE = (432, 437)

CREDIT_MARK = re.compile(r'(STAFF|スタッフ|原画|サウンド|ディレクタ|パスワ～ド|IYSI|制作|開発|協力)')
TRIVIA_MARK = re.compile(r'(クイズ|問題|正解|不正解|第[一二三四五六七八九十]問|得点|点です)')
CHARA_MARK = re.compile(r'^[^\t]{0,6}(、|，)?[ぁ-ん]{2,}[、，][ぁ-ん]{2,}')  # 、、、、）みその、ようこ《
PROFILE_MARK = re.compile(r'(歳|生まれ|出身|卒業|職業|血液型|プロフィ～ル|人物)')
# System / menu text that never belongs to a conversation.
SYSTEM_TEXT = re.compile(
    r'(デ～タを記録しますか|記録する|しない|手帳を閉じ|移動をやめるか|やめておく|'
    r'今いる場所を|文字を入力|Aボタン|Bボタン|はい$|いいえ$|決定|戻る|'
    r'ファイルが存在します|この配分でよろしいですか|何段目を調べる|'
    r'^[ABLRスタートセレクト]+ボタン|^ダミ～$|^アプロ～チコマンド|^攻撃する$|'
    r'^とどめをさす$|^話に割って入る$|^黙っている$|^すべてを報告する$|^嘘を言う$|'
    r'^青い帽子$|^愛し合っていた$|^遺品を渡す$|^亜希への謝罪$|^お金がない$|'
    r'^祖父と娘$|^哲也の母親$|^徳子を病院に届け|^悠を探して|^調査を終え|'
    r'^移動する$|^聞き込む$|^調べる$|^見る$|^見ない$|^かける$|^かけない$)')
# "we arrived at X" - the strongest scene-opening narration in this script.
ARRIVE = re.compile(
    r'^(?:俺|私)(?:達)?(?:と|は|が)?.*?'
    r'(?:へと?|に|の中へと?)?(?:やって(?:来|き)た|戻って(?:来|き)た|'
    r'に足を踏み入れ|へ入って(?:来|き)た|の中へとやって|へと向かった|に着いた)')
LOC_IN_ARRIVE = re.compile(
    r'^(?:俺|私)(?:達)?(?:と|は|が)?(.+?)(?:へと?|に|の中へと?)?'
    r'(?:やって(?:来|き)た|戻って(?:来|き)た|に足を踏み入れ|へ入って(?:来|き)た|'
    r'の中へとやって|へと向かった|に着いた)')
# Rows that close a sentence: the next row starts a new one.
CLOSERS = '。，！？『』“'
# Rows that are pure speaker labels (男 / 洋子 / 神宮寺の声).
LABEL_PUNCT = re.compile(r'[。、，！？「」『』“：]')


def load_master():
    with open(MASTER, encoding='utf-8', newline='') as f:
        rows = []
        for r in csv.reader(f, delimiter='\t'):
            if len(r) >= 7 and r[0].isdigit():
                rows.append({'entry': r[0], 'idx': r[1], 'offset': int(r[2] or 0),
                             'n_codes': int(r[3] or 0), 'jp_text': r[5],
                             'translation': r[6], 'kind': r[7] if len(r) > 7 else ''})
    return rows


def load_names():
    """Person names from the glossary (the 人名 section)."""
    names = set()
    if not os.path.exists(GLOSSARY):
        return names
    section = ''
    for line in open(GLOSSARY, encoding='utf-8'):
        line = line.rstrip('\n')
        if line.startswith('##'):
            section = line
            continue
        if line.startswith('#') or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) >= 2 and ('人名' in section or '定名' in section):
            for tok in re.split(r'[ ／/、]', parts[0]):
                if tok and not re.search(r'[ぁ-ん]', tok) or tok in ('かすみ', 'まなみ'):
                    names.add(tok)
    return names


def is_label(text, next_text, names):
    """A speaker label: short, no sentence punctuation, and followed by speech."""
    if len(text) > 8 or LABEL_PUNCT.search(text):
        return False
    if text in names:
        return True
    return bool(next_text) and next_text[0] in '」『“'


def classify(eid, rows):
    """Return (kind, note) for an entry."""
    e = int(eid)
    jp = [r['jp_text'] for r in rows]
    joined = ''.join(jp)
    n = len(rows)
    sys_rows = sum(1 for t in jp if SYSTEM_TEXT.search(t.strip()))
    if QUIZ_RANGE[0] <= e <= QUIZ_RANGE[1]:
        if TRIVIA_MARK.search(joined):
            return 'quiz', '问答/审讯分支'
        return 'quiz', '问答/审讯分支'
    if CHARA_RANGE[0] <= e <= CHARA_RANGE[1]:
        return 'chara', '人物资料页'
    if e == 432 or TRIVIA_MARK.search(joined) and n < 60:
        return 'trivia', '制作/问答彩蛋页'
    if e >= 418 and CREDIT_MARK.search(joined) and n < 80:
        return 'credits', 'STAFF/寄语页'
    if e >= 466:
        return 'credits', 'STAFF/寄语页'
    if sys_rows >= max(3, 0.5 * n):
        return 'system', '系统提示/菜单'
    return 'story', ''


def load_base():
    """eid -> string-pool base, from entry_catalog.tsv (offset-table entries)."""
    base = {}
    path = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    if not os.path.exists(path):
        return base
    for r in csv.DictReader(open(path, encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            base[r['eid']] = int(r['base'])
    return base


def abs_offset(row, base):
    """Absolute offset of a row inside the decompressed entry.

    Table rows carry the offset-table value (relative to the entry's string
    pool base, see docs/翻译作业流程.md 七); pool rows already carry the
    absolute offset.  Both are positions in the same decompressed buffer, so
    they can be merged - that is what turns the file's storage order back into
    the order the player reads.
    """
    if row['kind'] == 'pool':
        return row['offset']
    return base.get(row['entry'], 0) + row['offset']


def in_story_order(rows, base, pool_order='idx'):
    """Order an entry's rows the way the player reads them.

    ``idx`` is the order the two extractors established for the entry: offset
    table slots ascending, then the table-less pool runs appended after them
    (``extract_pool``).  Checking every row of the master against the ROM shows
    that (a) table rows are ordered by their absolute offset ``base + offset``
    (99.2% decode back to the recorded text; the rest are the password/ASCII
    rows) and (b) in the 132 mixed entries no pool row falls inside the table
    index range, i.e. the two sets really are appended, not interleaved.
    So sorting by ``idx`` is the story order; the offset-merge experiment that
    looked plausible only for pure-pool entries is kept for A/B reading.

    ``pool_order='offset'``: re-merge everything by absolute offset (only
    meaningful for mixed entries, and it disagrees with the extractor - do not
    use it without an emulator trace to confirm).
    """
    if pool_order == 'offset':
        return sorted(rows, key=lambda r: (abs_offset(r, base), int(r['idx'])))
    return sorted(rows, key=lambda r: int(r['idx']))


def split_blocks(rows):
    """Split an ordered entry into storage blocks.

    A block boundary is a run discontinuity: the offset jumps backwards instead
    of moving forward.  This is how an entry falls into its scene run and its
    system-prompt run (e1: 序幕正文 0..161 / 洋子问候与独白 162..202 / 地点与
    道具串 203..226), which is what we want to see apart.
    """
    blocks = []
    cur = [0]
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if b['offset'] < a['offset']:
            blocks.append(cur)
            cur = []
        cur.append(i + 1)
    blocks.append(cur)
    return blocks


def scene_kind(rows):
    n = len(rows)
    sys_rows = sum(1 for r in rows if SYSTEM_TEXT.search(r['jp_text'].strip()))
    if sys_rows >= max(3, 0.6 * n):
        return 'system'
    return 'scene'


def scene_location(rows):
    for r in rows[:6]:
        m = ARRIVE.match(r['jp_text'])
        if m:
            loc = LOC_IN_ARRIVE.match(r['jp_text'])
            if loc:
                return loc.group(1).strip('、。，')
    return ''


SPEAKER_EXTRA = {'男', '女', '女性', '男性', '少女', '少年', '子供', '子供の声', '男の子',
                 '女の子', '店員', '受付', '声', '神宮寺の声', '洋子の声', '女将', '医者',
                 '警官', '刑事', '通行人', '老人', '母親', '父親', '若い男', '若い女'}


def scene_speakers(rows, names):
    """Speaker labels actually used in the scene (glossary names + common roles).

    The game writes a bare name on its own display line ahead of the quoted
    speech.  Restricting to known names keeps answer words that happen to sit
    alone on a line (風邪, 仮病 …) out of the catalogue.
    """
    out = collections.OrderedDict()
    for i, r in enumerate(rows):
        nxt = rows[i + 1]['jp_text'] if i + 1 < len(rows) else ''
        t = r['jp_text'].strip()
        if not is_label(t, nxt, names):
            continue
        if t not in names and t not in SPEAKER_EXTRA:
            continue
        out.setdefault(t, 0)
        out[t] += 1
    return out


def build(report=False, pool_order='offset'):
    rows = load_master()
    names = load_names()
    base = load_base()
    by_entry = collections.OrderedDict()
    for r in rows:
        by_entry.setdefault(r['entry'], []).append(r)

    scenes = []
    lines = []
    for eid in sorted(by_entry, key=int):
        ordered = in_story_order(by_entry[eid], base, pool_order)
        kind, note = classify(eid, ordered)
        if kind != 'story':
            spk = scene_speakers(ordered, names)
            scenes.append({
                'scene': f'{eid}', 'kind': kind, 'entry': eid,
                'idx_from': ordered[0]['idx'], 'idx_to': ordered[-1]['idx'],
                'rows': len(ordered), 'codes': sum(r['n_codes'] for r in ordered),
                'location': '', 'speakers': ' '.join(spk),
                'note': note})
            for pos, r in enumerate(ordered):
                lines.append((eid, r, f'{eid}', pos, ordered))
            continue

        k = 0
        for block in split_blocks(ordered):
            seg = [ordered[i] for i in block]
            skind = scene_kind(seg)
            sid = f'{eid}.{k}'
            spk = scene_speakers(seg, names)
            scenes.append({
                'scene': sid, 'kind': skind, 'entry': eid,
                'idx_from': seg[0]['idx'], 'idx_to': seg[-1]['idx'],
                'rows': len(seg), 'codes': sum(r['n_codes'] for r in seg),
                'location': scene_location(seg),
                'speakers': ' '.join(spk), 'note': ''})
            for pos, r in enumerate(seg):
                lines.append((eid, r, sid, pos, seg))
            k += 1

    # ---- write catalog
    with open(SCENES, 'w', encoding='utf-8', newline='') as f:
        f.write('\t'.join(SCENE_HEADER) + '\n')
        for s in scenes:
            f.write('\t'.join(str(s[h]) for h in SCENE_HEADER) + '\n')
    scene_of = {s['scene']: s for s in scenes}
    with open(LINES, 'w', encoding='utf-8', newline='') as f:
        f.write('\t'.join(LINE_HEADER) + '\n')
        for eid, r, sid, pos, seg in lines:
            nxt = seg[pos + 1]['jp_text'] if pos + 1 < len(seg) else ''
            rid = f"{eid}:{r['idx']}"
            # does the sentence continue on the next line of the same scene?
            cont = '' if (not r['jp_text'] or r['jp_text'][-1] in CLOSERS) else '>'
            prev = seg[pos - 1]['jp_text'] if pos else ''
            joins = '<' if prev and prev[-1] not in CLOSERS and pos else ''
            f.write('\t'.join([rid, eid, r['idx'], sid, str(pos),
                               ('' if not cont else cont) + joins,
                               r['jp_text'], r['translation']]) + '\n')

    if report:
        kc = collections.Counter(s['kind'] for s in scenes)
        print(f'entries: {len(by_entry)}  scenes: {len(scenes)}  rows: {len(lines)}')
        print('scene kinds: ' + ', '.join(f'{k}={v}' for k, v in kc.most_common()))
        big = sorted((s for s in scenes if s['kind'] in ('scene', 'system')),
                     key=lambda s: -s['rows'])[:10]
        print('\nlargest scenes:')
        for s in big:
            print(f"  {s['scene']:<9} {s['rows']:>5} rows  {s['idx_from']}..{s['idx_to']}"
                  f"  {s['location'][:18]:<18} {s['speakers'][:40]}")
        locs = collections.Counter(s['location'] for s in scenes
                                   if s['kind'] == 'scene' and s['location'])
        print(f'\ndistinct locations: {len(locs)}')
        for l, c in locs.most_common(12):
            print(f'  {c:>3}  {l}')
    return scenes, lines


def cmd_build(a):
    build(report=a.report, pool_order=a.pool_order)
    print(f'wrote {SCENES}')
    print(f'wrote {LINES}')
    return 0


def read_scenes():
    out = {}
    for r in csv.DictReader(open(SCENES, encoding='utf-8'), delimiter='\t'):
        out[r['scene']] = r
    return out


def cmd_list(a):
    scenes = read_scenes()
    n = 0
    for sid, s in sorted(scenes.items(), key=lambda kv: (int(kv[1]['entry']),
                                                        float(kv[0].split('.')[1])
                                                        if '.' in kv[0] else 0)):
        if a.kind and s['kind'] != a.kind:
            continue
        if a.entry and s['entry'] != a.entry:
            continue
        n += 1
        print(f"{sid:<9} {s['kind']:<8} {s['rows']:>5}行 {s['codes']:>6}字 "
              f"{s['idx_from']:>4}..{s['idx_to']:<4} {s['location'][:16]:<16} {s['speakers']}")
    print(f'-- {n} scenes')
    return 0


def read_escapes(s):
    """Render the game's inline control codes as [XXXX] for readability."""
    return re.sub(r'\\x([0-9A-Fa-f]{4})', lambda m: '[' + m.group(1).upper() + ']', s)


def cmd_show(a):
    lines = []
    for r in csv.reader(open(LINES, encoding='utf-8'), delimiter='\t'):
        if r[0] == 'rid' or r[3] != a.scene:
            continue
        lines.append(r)
    if not lines:
        print(f'no such scene: {a.scene} (see scene_index.py list)')
        return 1
    scenes = read_scenes()
    s = scenes.get(a.scene)
    if s:
        print(f"# {a.scene}  kind={s['kind']}  entry={s['entry']}  "
              f"idx {s['idx_from']}..{s['idx_to']}  rows={s['rows']}")
        if s['location']:
            print(f"# 地点: {s['location']}")
        if s['speakers']:
            print(f"# 在场: {s['speakers']}")
        if s['note']:
            print(f"# 注: {s['note']}")
    print()
    for r in lines:
        rid, eid, idx, sid, pos, cont, jp, cn = r[:8]
        mark = '↳' if '>' in cont else ('↰' if '<' in cont else ' ')
        jp = read_escapes(jp)
        cn = read_escapes(cn)
        if a.cn:
            print(f'{idx:>5} {mark} {jp:<34} | {cn}')
        else:
            print(f'{idx:>5} {mark} {jp}')
    return 0


def cmd_check(a):
    rows = load_master()
    master = {(r['entry'], r['idx']) for r in rows}
    seen = {}
    dup = 0
    order_bad = 0
    for r in csv.reader(open(LINES, encoding='utf-8'), delimiter='\t'):
        if r[0] == 'rid':
            continue
        key = (r[1], r[2])
        if key in seen:
            dup += 1
        seen[key] = r
    missing = master - set(seen)
    extra = set(seen) - master
    # within a scene, position must be 0..n-1
    pos = collections.defaultdict(list)
    for r in csv.reader(open(LINES, encoding='utf-8'), delimiter='\t'):
        if r[0] == 'rid':
            continue
        pos[r[3]].append(int(r[4]))
    for sid, ps in pos.items():
        if ps != list(range(len(ps))):
            order_bad += 1
    print(f'master rows {len(master)}  indexed {len(seen)}  duplicated {dup}')
    print(f'missing from index: {len(missing)}  not in master: {len(extra)}')
    print(f'scenes with non-contiguous positions: {order_bad}')
    if missing:
        print('  e.g. ' + ', '.join(f'{e}:{i}' for e, i in list(missing)[:10]))
    if extra:
        print('  e.g. ' + ', '.join(f'{e}:{i}' for e, i in list(extra)[:10]))
    return 1 if (missing or extra or order_bad) else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--report', action='store_true')
    b.add_argument('--pool-order', choices=('idx', 'offset'), default='idx',
                   help="index order (default) or re-merge by absolute offset "
                        "(experimental)")
    b.set_defaults(func=cmd_build)
    l = sub.add_parser('list')
    l.add_argument('--kind')
    l.add_argument('--entry')
    l.set_defaults(func=cmd_list)
    s = sub.add_parser('show')
    s.add_argument('scene')
    s.add_argument('--cn', action='store_true', default=True)
    s.add_argument('--no-cn', action='store_false', dest='cn')
    s.set_defaults(func=cmd_show)
    c = sub.add_parser('check')
    c.set_defaults(func=cmd_check)
    a = ap.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())
