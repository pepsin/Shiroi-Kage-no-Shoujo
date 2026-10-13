#!/usr/bin/env python3
"""entry_id.py — 由 RAM 里的剧本缓冲反查「现在在演哪个 entry」（只读）。

为什么需要它
------------
`where` 一直只能报池基址 / 行数 / 游标，**报不出条目号**（docs/调试器.md 第五节）。
而覆盖率驱动的自动测试要把「现在演到哪」挂到 `data/scenes.tsv` /
`data/scene_lines.tsv` 上，就必须先知道 entry；否则观测是一串无意义的地址。

怎么反查（不依赖任何 work/ 构建产物，只用 ROM + RAM）
----------------------------------------------------
1. 描述符 RAM `0x02003F24` 的字段里已经有钥匙：

       +0x00 u32 字符串池基址      +0x10 u32 keyB
       +0x04 u32 ROM 指针          +0x14 u32 keyCount
       +0x08 u32 资源缓冲基址

   实测：`work/dbg/base.ss1` 的 keyCount=30 / keyB=1，正是引擎
   `loadScript(30, 1, 1)` 的参数（与分发表里 e438 那一条完全一致）。

2. ROM 分发表（file `0x15C004`，509 × 8 字节 `{entryId,keyCount,keyA,keyB}`）
   里按 (keyCount, keyB) 筛候选 —— 实测每组最多 7 个，e438 那组只有 [438, 469]。

3. 把 RAM 缓冲（描述符 +0x08 指向的地址）与候选条目的解压数据逐字节比。
   先比**结构区** `[0x000, 0x7A0)`（头部 + 流程/标志区，与译文无关，改译文不会动它），
   再比文本区的比例做兜底。实测 e438：结构区 1952/1952 全中，唯一竞争者 1944/1952。

注意：即时存档与 ROM 构建版本绑定（docs/调试器.md 第五节），用旧构建的 `.ss1`
配新 ROM 时文本区必然有差异；结构区 + 钥匙筛选正是为了在这种情况仍然能定出条目号。
"""

import struct

import gbtext as g

FAT = g.FAT
BASE = g.BASE
FAT_ENTRIES = 1024          # 0x15A000..0x15C000，再往后是引擎分发表
DISPATCH_FILE = 0x15C004
DISPATCH_COUNT = 509

POOL_OFF = 0x7A0            # 字符串池在条目内的偏移
STRUCT_END = 0x7A0          # [0, 0x7A0) = 头部 + 流程/标志区

DESC_ADDR = 0x02003F24      # 描述符
DEFAULT_WINDOW = 0x1000     # 比对窗口上限

# 判定「引擎正在演剧本」的宽松条件（与 tools/state_inspect.py 的 live 一致）
EWRAM_LO, EWRAM_HI = 0x02020000, 0x02040000
LINE_MAX = 0x20000


class Match:
    """一次反查结果。hits/total 越大越可信；structural 满命中即可确信。"""

    __slots__ = ('eid', 'structural_hits', 'structural_total', 'hits', 'total',
                 'first_diff', 'candidates', 'scanned', 'ambiguous')

    def __init__(self, eid, sh, st, h, t, first_diff, candidates, scanned, ambiguous=()):
        self.eid = eid
        self.structural_hits = sh
        self.structural_total = st
        self.hits = h
        self.total = t
        self.first_diff = first_diff
        self.candidates = candidates
        self.scanned = scanned
        self.ambiguous = list(ambiguous)

    @property
    def structural_complete(self):
        return self.structural_total == STRUCT_END and self.structural_hits == STRUCT_END

    @property
    def ratio(self):
        return self.hits / self.total if self.total else 0.0

    def __repr__(self):
        return (f'<Match e{self.eid} structural={self.structural_hits}/'
                f'{self.structural_total} window={self.hits}/{self.total} '
                f'({"scan" if self.scanned else "keys"})>')


class RomIndex:
    """ROM 侧的候选索引：分发表 + 条目解压缓存。"""

    def __init__(self, rom):
        self.rom = rom
        self._cache = {}
        self.dispatch = [struct.unpack_from('<4h', rom, DISPATCH_FILE + 8 * k)
                         for k in range(DISPATCH_COUNT)]
        self._by_keys = {}
        for e, c, _a, b in self.dispatch:
            self._by_keys.setdefault((c, b), []).append(e)
        self.all_entries = [eid for eid in range(FAT_ENTRIES)
                            if struct.unpack_from('<2I', rom, FAT + eid * 8) != (0, 0)]

    def entry(self, eid):
        if eid not in self._cache:
            self._cache[eid] = g.load_entry(self.rom, eid)
        return self._cache[eid]

    def candidates(self, key_count, key_b):
        """按 (keyCount, keyB) 筛；筛不到就退一步只按 keyCount。"""
        hit = self._by_keys.get((key_count, key_b))
        if hit:
            return sorted(set(hit)), False
        if key_count is not None:
            loose = [e for e, c, _a, _b in self.dispatch if c == key_count]
            if loose:
                return sorted(set(loose)), False
        return list(self.all_entries), True


def _score(d, buf, window):
    n = min(len(d), len(buf), window)
    if n <= 0:
        return 0, 0, 0, 0, None
    a, b = d[:n], buf[:n]
    hits = 0
    first = None
    for i in range(n):
        if a[i] == b[i]:
            hits += 1
        elif first is None:
            first = i
    m = min(n, STRUCT_END)
    sh = sum(1 for i in range(m) if a[i] == b[i])
    return sh, m, hits, n, first


def resolve(idx, buf, key_count=None, key_b=None, window=DEFAULT_WINDOW):
    """反查 buf（RAM 里的整条剧本资源）对应哪个 entry。

    key_count/key_b 来自描述符；给不出就退化成全表扫描（scanned=True）。
    """
    cands, scanned = idx.candidates(key_count, key_b) if key_count is not None \
        else (list(idx.all_entries), True)
    if not buf or window <= 0:
        return Match(None, 0, 0, 0, 0, None, cands, scanned)

    scored = []
    for eid in cands:
        d = idx.entry(eid)
        if not d:
            continue
        # 先看 32 字节前缀，明显不像的跳过（全表扫描时省时间）
        pre = min(len(d), len(buf), 32)
        if pre and sum(1 for i in range(pre) if d[i] == buf[i]) < pre // 2:
            continue
        sh, st, h, t, first = _score(d, buf, window)
        scored.append((sh / st if st else 0.0, h / t if t else 0.0, eid, sh, st, h, t, first))

    if not scored:
        return Match(None, 0, 0, 0, 0, None, cands, scanned)

    scored.sort(key=lambda r: (-r[0], -r[1], r[2]))
    best = scored[0]
    ties = [r[2] for r in scored[1:] if r[0] == best[0] and r[1] == best[1]]
    return Match(best[2], best[3], best[4], best[5], best[6], best[7],
                 cands, scanned, ties)


def resolve_from_ram(rom_or_index, ewram, key_count=None, key_b=None, buf_addr=None):
    """便利入口：直接从 EWRAM 快照（+ 描述符）反查。

    buf_addr 不给就用描述符里的资源缓冲基址。返回 (Match, buf_bytes)。
    """
    idx = rom_or_index if isinstance(rom_or_index, RomIndex) else RomIndex(rom_or_index)
    if buf_addr is None:
        buf_addr = struct.unpack_from('<I', ewram, DESC_ADDR + 8 - 0x02000000)[0]
    if key_count is None:
        key_count = struct.unpack_from('<I', ewram, DESC_ADDR + 0x14 - 0x02000000)[0]
    if key_b is None:
        key_b = struct.unpack_from('<I', ewram, DESC_ADDR + 0x10 - 0x02000000)[0]
    off = buf_addr - 0x02000000
    buf = bytes(ewram[off:off + 0x20000]) if 0 <= off < len(ewram) else b''
    return resolve(idx, buf, key_count, key_b), buf
