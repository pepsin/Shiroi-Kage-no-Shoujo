#!/usr/bin/env python3
"""crash_bisect.py — 出「白屏 / EWRAM 被清零」型崩溃时，二分出是哪几个条目的改写引起的。

这是 `work/crashtest.sh`（已随 work/ 被清掉）的重写版。当年的流程是
「assemble_subset 逐组重建 ROM + 无头跑按键脚本 + 看 EWRAM 是否被清零」，
二分到 e316 与 e312/313/314/315 同时写入就崩（docs/技术说明.md 第十节）。
根因（追加条目后面没补零 guard、以及没对齐）现在由
`tools/check_append_guard.py` **静态**兜住，不需要模拟器；这个工具留给
「静态检查全绿、实机仍然炸」的情况。

判据：从 --state 起跑 --frames 帧、按 --keys 按键后，EWRAM 的非零字节数低于
--min-nonzero（默认 16）即判为白屏/清零。可选 --use-dispcnt 额外要求
DISPCNT 处于强制消隐（bit 7 = 1）。

前置条件（三者必须出自**同一次构建**，否则即时存档不兼容）：
  --base   同一次构建、**没有写剧本**的 ROM（build_rom.py 会存成 work/font_patched.gba）
  --full   当前简中 ROM
  --state  崩溃现场附近的起点即时存档（.ss1）

用法：
  python3 tools/crash_bisect.py --state work/dbg/ss5.ss1 \
          --keys "A@60-1399:4:20"
  python3 tools/crash_bisect.py --self-test        # 只用合成判据验证二分逻辑（不跑模拟器）
  python3 tools/crash_bisect.py --state x.ss1 --probe   # 只跑一次判据，不二分

退出码：0 = 二分完成（报告最小崩溃集）；1 = 前置条件不成立（比如全集都不崩）；
2 = 参数/依赖缺失；3 = 超出 --max-runs 预算。
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g   # noqa: E402
from assemble_subset import differing_entries
from harness_utils import expand_keys, run_harness, HarnessError

FAT = g.FAT
FAT_ENTRIES = 1024
HARNESS = os.path.join(ROOT, 'work', 'bin', 'gbarun_dbg')
ASSEMBLE = os.path.join(ROOT, 'tools', 'assemble_subset.py')
WORKDIR = os.path.join(ROOT, 'work', 'crashbisect')

CN_ROM = os.path.join(ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba')
FONT_PATCHED = os.path.join(ROOT, 'work', 'font_patched.gba')


# ------------------------------------------------------------------ 二分
class Budget(Exception):
    pass


def ddmin(items, fails, max_runs=120, log=print):
    """把 items 缩到「去掉任何一个就不再崩」的小集合。

    同时检查分块与补集；缓存按集合记，预算只计真正的判据调用。
    返回 [] 表示未改写任何条目的参照也崩，不能归因于某条改写。
    """
    runs = [0]
    cache = {}

    def test(sub):
        key = tuple(sub)
        if key in cache:
            return cache[key]
        if runs[0] >= max_runs:
            raise Budget(f'超过 --max-runs={max_runs} 次模拟器运行，停手')
        runs[0] += 1
        r = fails(list(sub))
        cache[key] = r
        log(f'   [{runs[0]:>3}] {len(sub):>4} 个条目 → {"CRASH" if r else "ok"}')
        return r

    items = sorted(set(items))
    if not test(items):
        return None, runs[0]
    if test([]):
        return [], runs[0]
    cur = list(items)

    def split(seq, n):
        k, m = divmod(len(seq), n)
        out, i = [], 0
        for j in range(n):
            sz = k + (1 if j < m else 0)
            if sz:
                out.append(seq[i:i + sz])
            i += sz
        return [c for c in out if c]

    n = 2
    while len(cur) >= 2:
        parts = split(cur, n)
        shrunk = False
        complements = [[x for x in cur if x not in set(p)] for p in parts]
        for p in parts + complements:
            if len(p) < len(cur) and test(p):
                cur = p
                n = max(2, n - 1)
                shrunk = True
                break
        if shrunk:
            continue
        if n >= len(cur):
            break
        n = min(len(cur), n * 2)

    i = 0
    while i < len(cur):
        sub = cur[:i] + cur[i + 1:]
        if test(sub):
            cur = sub
            i = 0
        else:
            i += 1
    return cur, runs[0]


# ------------------------------------------------------------- 真实判据
class Probe:
    def __init__(self, a):
        self.a = a
        self.n = 0
        self.workdir = tempfile.mkdtemp(prefix='run-', dir=WORKDIR)

    def __call__(self, eids):
        self.n += 1
        sub = os.path.join(self.workdir, f'sub{self.n:03d}.gba')
        entries = ','.join(str(e) for e in sorted(eids))
        p = subprocess.run([sys.executable, ASSEMBLE, '--base', self.a.base,
                            '--full', self.a.full, '--entries', entries,
                            '--out', sub], capture_output=True, text=True, timeout=self.a.timeout)
        if p.returncode != 0:
            raise HarnessError(f'assemble_subset 失败：\n{p.stdout}\n{p.stderr}')

        prefix = os.path.join(self.workdir, f'probe{self.n:03d}')
        reads = f'0x04000000:2@{self.a.frames - 1}' if self.a.use_dispcnt else ''
        evidence, out = run_harness(HARNESS, sub, self.a.frames, self.a.keys, prefix,
                                    state=self.a.state, sav=self.a.sav,
                                    frame_read=reads, timeout=self.a.timeout)
        ew = evidence['ewram']
        nz = sum(1 for b in ew if b)
        crash = nz < self.a.min_nonzero
        if self.a.use_dispcnt:
            dc = parse_dispcnt(out)
            if dc is None:
                raise HarnessError('缺少 DISPCNT 采样，无法判断崩溃')
            crash = crash and (dc & 0x80) != 0
        return crash


def parse_dispcnt(out):
    """从 harness 输出里取 GBARUN_FRAME_READ 读到的 DISPCNT（u16）。"""
    m = re.search(r'\[read\] 0x04000000 len=2:(.*?)(?=\n\[|\Z)', out, re.S)
    if not m:
        return None
    toks = []
    for line in m.group(1).split('\n'):
        line = line.strip()
        if re.match(r'^0x[0-9A-Fa-f]{8}\b', line):
            line = line[10:].strip()
        toks += [t for t in line.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', t)]
    if len(toks) < 2:
        return None
    return int(toks[0], 16) | (int(toks[1], 16) << 8)


# --------------------------------------------------------------- self-test
def self_test():
    cases = [
        ('崩溃需要同时出现 3 和 7', lambda s: {3, 7} <= set(s), {3, 7}),
        ('单个 5 就崩', lambda s: 5 in s, {5}),
        ('任意 3 个就崩', lambda s: len(s) >= 3, None),
        ('空集也崩（应拒绝）', lambda s: True, None),
    ]
    ok = True
    for name, pred, want in cases:
        items = list(range(1, 21))
        got, runs = ddmin(items, pred, max_runs=200, log=lambda *_: None)
        good = got is not None and pred(got) and all(not pred(got[:i] + got[i + 1:])
                                                     for i in range(len(got)))
        if want is not None:
            good = good and set(got) == want
        ok = ok and good
        print(f'  [{"OK " if good else "BAD"}] {name}: → {got}（{runs} 次判据调用）')
    # 全集不崩时应当返回 None
    got, _ = ddmin(list(range(1, 11)), lambda s: False, max_runs=50, log=lambda *_: None)
    good = got is None
    ok = ok and good
    print(f'  [{"OK " if good else "BAD"}] 全集不崩 → {got}')
    print('== self-test ' + ('通过' if ok else '失败'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base', default=FONT_PATCHED)
    ap.add_argument('--full', default=CN_ROM)
    ap.add_argument('--state', default='', help='起点即时存档（.ss1），必须与 --base/--full 同一次构建')
    ap.add_argument('--sav', default='', help='卡带存档（需要的话）')
    ap.add_argument('--keys', default='A@60-1399:4:20')
    ap.add_argument('--frames', type=int, default=1400)
    ap.add_argument('--min-nonzero', type=int, default=16)
    ap.add_argument('--use-dispcnt', action='store_true')
    ap.add_argument('--max-runs', type=int, default=120)
    ap.add_argument('--timeout', type=float, default=60, help='每次模拟器/拼装最多运行秒数')
    ap.add_argument('--probe', action='store_true', help='只跑一次判据')
    ap.add_argument('--self-test', action='store_true', help='用合成判据验证二分逻辑')
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if a.frames <= 0 or a.max_runs <= 0 or not 0 < a.timeout <= 3600:
        ap.error('frames/max-runs 必须为正，timeout 必须在 (0, 3600] 秒内')
    if not 0 < a.min_nonzero <= 0x40000:
        ap.error('min-nonzero 必须在 1..262144 内')
    expand_keys(a.keys, a.frames)

    for p, why in ((a.base, '参照 ROM（build_rom.py 的 work/font_patched.gba）'),
                   (a.full, '当前简中 ROM')):
        if not os.path.exists(p):
            print(f'!! 缺少 {why}：{p}', file=sys.stderr)
            return 2
    if not os.path.exists(HARNESS):
        print(f'!! 缺少 {HARNESS}，先跑 make dbg', file=sys.stderr)
        return 2
    if not a.state:
        print('!! 必须给 --state（崩溃现场附近的即时存档）。\n'
              '   work/ 被 gitignore，历史存档不会留在仓库里；请先用 mGBA 手存一份，\n'
              '   或用 tools/romdbg.py play/base 生成后传进来。', file=sys.stderr)
        return 2
    if not os.path.exists(a.state):
        print(f'!! 找不到存档：{a.state}', file=sys.stderr)
        return 2

    os.makedirs(WORKDIR, exist_ok=True)
    base = open(a.base, 'rb').read()
    full = open(a.full, 'rb').read()
    cands = differing_entries(base, full)
    print(f'== 崩溃二分：{len(cands)} 个被改写的条目，判据 = EWRAM 非零字节 < {a.min_nonzero}')
    probe = Probe(a)
    if a.probe:
        cr = probe(cands)
        print(f'   全集 {"CRASH" if cr else "ok"}')
        return 0 if not cr else 1

    try:
        minimal, runs = ddmin(cands, probe, max_runs=a.max_runs,
                              log=lambda s: print(s, flush=True))
    except Budget as exc:
        print(f'!! {exc}', file=sys.stderr)
        return 3
    if minimal is None:
        print('== 全集都不崩：这条存档/按键路线复现不出崩溃，换起点存档或按键脚本再试。')
        return 1
    if not minimal:
        print('== 空集参照也崩：无法归因于条目改写，请检查起点存档与复现路线。')
        return 1
    print(f'== 最小崩溃集（{runs} 次运行）：{sorted(minimal)}')
    print('   去掉其中任何一个都不再崩；接下来的排查从这里开始。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, HarnessError, subprocess.TimeoutExpired) as exc:
        print(f'!! {exc}', file=sys.stderr)
        sys.exit(2)
