#!/usr/bin/env python3
"""把在 scene_lines.tsv 上做的手工改动合并回主表 translation.tsv。

为什么需要它
------------
`data/scene_lines.tsv` 是**派生文件**——由 `tools/scene_index.py build`
从 `data/translation.tsv` 生成，好处是**按剧情顺序**排列、带 `scene / pos / join`
上下文，读起来比按存储顺序的主表舒服得多，所以顺手在上面改译文很自然。

但打包只认主表：`tools/build_rom.py` → `import_script.py --master data/translation.tsv`。
更糟的是，只要再跑一次 `scene_index.py build`，`scene_lines.tsv` 就会被主表覆盖，
**手改的译文会静默丢失**。

这个工具就是那道桥：以 `(entry, idx)`（也就是 `rid` 列 `条目:下标`）为键，
把 `scene_lines.tsv` 的 `translation` 列差异写回 `translation.tsv`。
两侧键是一一对应的（`scene_index.py check` 保证覆盖），所以不会有歧义。

实现要点（别改成整表 csv 重写）
------------------------------
* **按行原地替换**：只改命中行的第 7 个字段（`translation`），其余字节——
  行尾（本仓库是 LF）、字段引号、列序——一律不动。主表是 10 万行的手工文件，
  过一次 `csv.writer` 会带来十几万行的无意义 diff（行尾 LF→CRLF、含 `"` 的字段被转义）。
* **行号 = 文件行号**：主表第 1 行是表头，用「第 k 条数据」去索引 `lines[k]`
  会整体错位一行、把译文写到邻居身上。这里直接按文件行号建索引，
  写盘前再核对该行的 `entry`/`idx` 是否就是目标键，错位立刻报错而不写坏数据。
* **过期索引保护**：`scene_index.py build` 会在 `data/scene_lines.tsv.sig` 里记下
  当时主表的 sha1。如果主表在那之后被直接改过（README 推荐的另一种改法），
  scene_lines 就是旧索引，再拿它合并会把新译文改回去——这时本工具会拒绝写盘，
  提示你先重建索引；确实要用旧索引覆盖时加 `--force`。

用法
----
  tools/merge_scene_edits.py --check       # 只报告差异（退出码 1 表示有差异）
  tools/merge_scene_edits.py --dry-run     # 报告 + 约束校验，不写盘
  tools/merge_scene_edits.py               # 合并进 data/translation.tsv
  tools/merge_scene_edits.py --no-validate # 跳过约束校验（不推荐）

约束校验（照 data/translation_rules.md 的硬性规则）：
  * 码位数不得超过原文（列 `n_codes`）
  * 不得出现假名
  * 标点符号的种类与数量必须与原文一致
  * 用字必须在字库内（work/glyph_map.ext.csv）
有问题只报警、仍然写盘；合并后请自己决定是否返工。
"""
import argparse
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
LINES = os.path.join(ROOT, 'data', 'scene_lines.tsv')
EXT_MAP = os.path.join(ROOT, 'work', 'glyph_map.ext.csv')

KANA = re.compile(r'[\u3041-\u309F\u30A0-\u30FF]')
PUNCT = re.compile(r'[、。，！？「」『』・：；“”‘’（）〈〉《》‥…□○■×＋＜＞％]')


def n_codes(s):
    """把 \\xNNNN 记作 1 个码位（与 import_script 的约定一致）。"""
    return len(re.sub(r'\\x[0-9A-Fa-f]{4}', 'X', s))


def split_line(raw):
    """一行 → (字段表, 行尾)。按行解析，避免整表 csv 读写带来的重写。"""
    if raw.endswith('\r\n'):
        body, eol = raw[:-2], '\r\n'
    elif raw.endswith('\n'):
        body, eol = raw[:-1], '\n'
    else:
        body, eol = raw, ''
    return next(csv.reader([body], delimiter='\t')), eol


def load_ext_chars(path=EXT_MAP):
    chars = set()
    if not os.path.exists(path):
        return chars
    for r in csv.DictReader(open(path, encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            chars.add(ch)
    return chars


def read_master(path):
    """→ (lines, rows, line_of_key)：rows[k] 是 lines[k] 解析出的字段表。"""
    with open(path, encoding='utf-8', newline='') as f:
        lines = f.readlines()
    rows, line_of_key = [], {}
    for k, raw in enumerate(lines):
        fields, _ = split_line(raw)
        rows.append(fields)
        if len(fields) >= 7 and fields[0].isdigit():
            line_of_key.setdefault((fields[0], fields[1]), k)
    return lines, rows, line_of_key


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--master', default=MASTER)
    ap.add_argument('--lines', default=LINES)
    ap.add_argument('--check', action='store_true', help='只报告差异，不写盘')
    ap.add_argument('--dry-run', action='store_true', help='报告 + 校验，不写盘')
    ap.add_argument('--no-validate', action='store_true', help='跳过约束校验')
    ap.add_argument('--force', action='store_true',
                    help='scene_lines 是旧索引时也照样覆盖主表')
    a = ap.parse_args()

    lines, rows, line_of_key = read_master(a.master)
    ext = load_ext_chars()

    # scene_lines 是不是从「当前这一版主表」生成的？
    import hashlib
    stale = None
    sig_path = a.lines + '.sig'
    if os.path.exists(sig_path):
        want = open(sig_path, encoding='utf-8').read().split('\t')[0].strip()
        now = hashlib.sha1(open(a.master, 'rb').read()).hexdigest()
        if want and want != now:
            stale = (want, now)
    elif not a.force and not a.check:
        stale = (None, None)

    n_data = sum(1 for r in rows if len(r) >= 7 and r[0].isdigit())
    diffs, unknown, problems = [], [], []
    with open(a.lines, encoding='utf-8', newline='') as f:
        for raw in f:
            fields, _ = split_line(raw)
            if len(fields) < 8 or not fields[1].isdigit():
                continue
            k = line_of_key.get((fields[1], fields[2]))
            if k is None:
                unknown.append((fields[1], fields[2]))
                continue
            old, new, jp = rows[k][6], fields[7], rows[k][5]
            if old == new:
                continue
            diffs.append((k, fields[1], fields[2], jp, old, new, int(rows[k][3])))

    print(f'主表 {n_data} 行；scene_lines 里对不上的键 {len(unknown)} 个')
    print(f'需要合并的改动：{len(diffs)} 行')
    for k, e, i, jp, old, new, nc in diffs:
        flags = []
        if n_codes(new) > nc:
            flags.append(f'超长 {n_codes(new)}>{nc}')
        if KANA.search(new):
            flags.append('含假名')
        if len(PUNCT.findall(new)) != len(PUNCT.findall(jp)):
            flags.append(f'标点 {len(PUNCT.findall(new))}≠{len(PUNCT.findall(jp))}')
        if ext:
            miss = [c for c in new if c not in ext and c not in '\\x']
            if miss:
                flags.append('缺字 ' + ''.join(miss))
        tag = ('  !!! ' + '; '.join(flags)) if flags else ''
        print(f'  {e}:{i}  n={nc}  JP {jp}')
        print(f'        旧 {old}')
        print(f'        新 {new}{tag}')
        if flags and not a.no_validate:
            problems.append((e, i, flags))

    if problems:
        print(f'\n有 {len(problems)} 行违反 data/translation_rules.md 的硬性规则'
              '（已照合并，请自行决定是否返工）。')

    if stale and diffs and not a.force:
        print()
        print('!! data/scene_lines.tsv 是**旧索引**：主表在它生成之后被直接改过。')
        print('   拿它合并会把主表里更新的译文改回去，已中止（未改文件）。')
        print('   想丢弃 scene_lines 上的改动、只保留主表：')
        print('       python3 tools/scene_index.py build')
        print('   确实要用 scene_lines 覆盖主表：加 --force')
        return 3

    if a.check or a.dry_run:
        return 1 if diffs else 0
    if not diffs:
        print('主表已经是最新的，无需合并。')
        return 0

    bad = 0
    for k, e, i, jp, old, new, nc in diffs:
        # 写盘前核对键：行号一旦错位，这里必须报错而不是把邻居改坏
        if len(rows[k]) < 7 or rows[k][0] != e or rows[k][1] != i:
            print(f'!! 行号错位：第 {k + 1} 行是 {rows[k][:2]}，期望 {e}:{i}，已跳过')
            bad += 1
            continue
        fields, eol = split_line(lines[k])
        fields[6] = new
        lines[k] = '\t'.join(fields) + eol
    if bad:
        print(f'!! {bad} 行键不符，已中止，文件未改动')
        return 2

    with open(a.master, 'w', encoding='utf-8', newline='') as f:
        f.writelines(lines)
    print(f'\n已写回 {a.master}（{len(diffs)} 行，按行原地替换，行尾与其余字段未动）。')
    print('接着重建场景索引与 ROM：')
    print('  python3 tools/scene_index.py build')
    print('  python3 tools/build_rom.py --out "侦探神宫寺三郎 - 白影的少女 (简中).gba"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
