#!/usr/bin/env python3
"""state_inspect.py - 只读地看一眼 mGBA 即时存档（.ssN）里引擎在演什么。

Unlike tools/romdbg.py it does not need work/glyph_map.ext.csv; it builds the
code->char map straight from data/glyph_map.csv plus the ROM font table.

Usage:
  python3 tools/state_inspect.py where  <state.ssN> [...]
  python3 tools/state_inspect.py entry  <state.ssN> [...]   # 只报条目号/场景号
  python3 tools/state_inspect.py peek   <state.ssN> ADDR[,LEN] ...
  python3 tools/state_inspect.py rows   <state.ssN> [count]
  python3 tools/state_inspect.py diff   <a.ssN> <b.ssN> [--region wram|iwram|vram|oam|pram|io]
  python3 tools/state_inspect.py scan   <state.ssN> HEXBYTES
"""
import csv
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import entry_id      # noqa: E402
import scene_lookup  # noqa: E402

FAT, BASE, FONT_EID = 0x15A000, 0x15C000, 850
REGIONS = {'io': (0x400, 0x400), 'pram': (0x800, 0x400), 'oam': (0xC00, 0x400),
           'vram': (0x1000, 0x18000), 'iwram': (0x19000, 0x8000), 'wram': (0x21000, 0x40000)}
# guest addresses of the regions
GADDR = {'wram': 0x02000000, 'iwram': 0x03000000, 'vram': 0x06000000,
         'pram': 0x05000000, 'oam': 0x07000000, 'io': 0x04000000}

CN_ROM = os.path.join(ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba')


def read_state(path):
    d = open(path, 'rb').read()
    i = 8
    while i + 8 <= len(d):
        ln = struct.unpack('>I', d[i:i + 4])[0]
        if d[i + 4:i + 8] == b'gbAs':
            raw = zlib.decompress(d[i + 8:i + 8 + ln])
            return {n: raw[o:o + s] for n, (o, s) in REGIONS.items()}
        i += 12 + ln
    raise SystemExit('no gbAs chunk in ' + path)


class State:
    def __init__(self, path):
        self.path = path
        self.reg = read_state(path)

    def at(self, addr):
        """-> (region_name, offset_in_region)"""
        for n in ('wram', 'iwram', 'vram', 'pram', 'oam', 'io'):
            base = GADDR[n]
            size = len(self.reg[n])
            if base <= addr < base + size:
                return n, addr - base
        raise KeyError(f'0x{addr:08X} outside dumped regions')

    def read(self, addr, ln):
        """Byte-wise read across region boundaries."""
        out = bytearray()
        while ln:
            n, off = self.at(addr)
            room = len(self.reg[n]) - off
            take = min(room, ln)
            out += self.reg[n][off:off + take]
            addr += take
            ln -= take
        return bytes(out)

    def u8(self, a):
        return self.read(a, 1)[0]

    def u16(self, a):
        return struct.unpack('<H', self.read(a, 2))[0]

    def u32(self, a):
        return struct.unpack('<I', self.read(a, 4))[0]


# ------------------------------------------------------------------ glyph map
def rom_and_map():
    rom = open(CN_ROM, 'rb').read()
    mp = {}
    used = set()
    csv_path = os.path.join(ROOT, 'work', 'glyph_map.ext.csv')
    if not os.path.exists(csv_path):
        # 没有打包产物时退回主表。注意：改字库后重建 ROM，字形槽位会重排，
        # 用这份旧映射去解「旧构建的即时存档」会读出乱码/错字（本轮踩过）。
        print('[warn] work/glyph_map.ext.csv 不存在，回退 data/glyph_map.csv；'
              '改过字库后解旧 .ssN 可能读到错字', file=sys.stderr)
        csv_path = os.path.join(ROOT, 'data', 'glyph_map.csv')
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            ch = (r.get('char') or '').strip()
            if not ch:
                continue
            code = int(r['code'], 16)
            used.add(code)
            mp.setdefault(code, ch)
    # index >= 0x6A8 came from the CN build; take them from the font table so the
    # tool never depends on a stale work/ artifact.
    o, s = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    font = rom[BASE + o: BASE + o + s]
    n = s // 0x80
    blank = {i for i in range(n) if not any(font[i * 0x80:(i + 1) * 0x80])}
    return rom, mp, blank, font, n


ROM = MAP = BLANK = FONT = NFONT = None


def ensure_rom():
    """Pure RAM inspection and test discovery must also work without a local ROM."""
    global ROM, MAP, BLANK, FONT, NFONT
    if ROM is None:
        ROM, MAP, BLANK, FONT, NFONT = rom_and_map()


def glyph_text(codes):
    ensure_rom()
    out = []
    for c in codes:
        idx = c if c < 0x20 else c - 1
        if c == 0:
            out.append('|')
        elif idx in BLANK:
            out.append(' ')
        else:
            out.append(MAP.get(idx, f'<{idx:03X}>'))
    return ''.join(out)


def read_row(data, off):
    if off < 0 or off + 2 > len(data):
        return []
    n = 0
    while off + 2 * n + 2 <= len(data) and struct.unpack_from('<H', data, off + 2 * n)[0] != 0:
        n += 1
    return list(struct.unpack_from(f'<{n}H', data, off))


def table_start(ew, pool, cursor):
    if cursor < 2 or not 0 <= pool - 0x02000000 < len(ew):
        return None
    slot = pool - 0x02000000 + cursor - 2
    if slot + 4 > len(ew) or slot < 8:
        return None
    start = slot
    while start - 4 >= pool - 0x02000000:
        a = struct.unpack_from('<I', ew, start - 4)[0]
        b = struct.unpack_from('<I', ew, start)[0]
        if a >= b or b == 0:
            break
        start -= 4
    return start - (pool - 0x02000000)


def engine(st):
    ew, iw = st.reg['wram'], st.reg['iwram']
    pool, romptr, buf, buf2, key_b, rows = struct.unpack_from('<6I', ew, 0x3F24)
    cur = struct.unpack_from('<I', iw, 0x7C40)[0]
    line = struct.unpack_from('<I', iw, 0x7C44)[0]
    rel = table_start(ew, pool, cur)
    idx = (cur - 2 - rel) // 4 if (rel is not None and cur >= 2 and cur - 2 >= rel) else None
    # keyCount is a dispatch key, not the number of lines (e1 has keyCount=1
    # but hundreds of dialogue lines). Validate the actual table pointer instead.
    live = (0x02020000 <= pool < 0x02040000 and
            0 <= pool - 0x02000000 + line < len(ew) - 1 and
            idx is not None and
            struct.unpack_from('<I', ew, pool - 0x02000000 + cur - 2)[0] == line)
    return dict(pool=pool, romptr=romptr, buf=buf, rows=rows, key_b=key_b,
                cur=cur, line=line, rel=rel, idx=idx, live=live)


# ------------------------------------------------------- entry 号反查（懒加载）
_IDX = None
_LOOKUP = None


def rom_index():
    global _IDX
    if _IDX is None:
        ensure_rom()
        _IDX = entry_id.RomIndex(ROM)
    return _IDX


def lookup():
    global _LOOKUP
    if _LOOKUP is None:
        _LOOKUP = scene_lookup.Lookup()
    return _LOOKUP


def resolve_entry(st, e):
    """→ (Match 或 None, 场景号 或 None, 行 dict 或 None)。非剧本态返回 (None,None,None)。"""
    if not e['live']:
        return None, None, None
    m = entry_id.resolve_from_ram(rom_index(), st.reg['wram'],
                                  key_count=e['rows'], key_b=e['key_b'])[0]
    if m.eid is None:
        return m, None, None
    lk = lookup()
    row = lk.at_offset(m.eid, e['line'])
    return m, row['scene'] if row else lk.best_scene(m.eid, None), row


def cmd_where(states):
    for p in states:
        st = State(p)
        e = engine(st)
        ew = st.reg['wram']
        print(f'== {os.path.basename(p)}')
        print(f'   池基址 0x{e["pool"]:08X}  缓冲 0x{e["buf"]:08X}  keyCount {e["rows"]}  rom 0x{e["romptr"]:08X}')
        print(f'   行表@池内 +0x{e["rel"]:X}' if e['rel'] is not None else '   行表 未知')
        print(f'   游标 0x{e["cur"]:04X} → 第 {e["idx"]} 项   当前行 off 0x{e["line"]:04X}')
        if e['live']:
            codes = read_row(ew, e['pool'] - 0x02000000 + e['line'])
            print(f'   文本 {glyph_text(codes)}')
        else:
            print(f'   文本 (引擎未在演剧本：池基址/当前行都不是有效值 —— 菜单、黑屏或过场中)')
        m, scene, row = resolve_entry(st, e)
        if m is None:
            print('   条目 (未在演剧本，跳过反查)')
        elif m.eid is None:
            print(f'   条目 反查失败（候选 {len(m.candidates)} 个，'
                  f'{"全表扫描" if m.scanned else "按钥匙筛选"}）')
        else:
            conf = '结构区全中' if m.structural_complete else \
                f'结构区 {m.structural_hits}/{m.structural_total}'
            tail = '（有同分候选 %s）' % m.ambiguous if m.ambiguous else ''
            print(f'   条目 e{m.eid}  场景 {scene}  [{conf}，窗口 {m.hits}/{m.total}'
                  f'，{"全表扫描" if m.scanned else f"候选 {len(m.candidates)} 个"}]{tail}')
            if row:
                print(f'   行 {row["rid"]}  jp={row["jp"]}')
                print(f'{"":>19}cn={row["cn"]}')
        # a few engine bytes that matter for "is it stuck"
        print(f'   inedge 0x02003EEC={st.u16(0x02003EEC):04X}  menu 0x02003EE0='
              f'{st.u32(0x02003EE0):08X}  keyblk 0x02000DD0={st.u16(0x02000DD0):04X}')
    return 0


def cmd_entry(states):
    for p in states:
        st = State(p)
        e = engine(st)
        m, scene, row = resolve_entry(st, e)
        name = os.path.basename(p)
        if m is None or m.eid is None:
            print(f'{name}\t-\t-\t(未在演剧本或反查失败)')
            continue
        print(f'{name}\te{m.eid}\t{scene}\t{row["rid"] if row else ""}')
    return 0


def cmd_peek(args):
    p, specs = args[0], args[1:]
    st = State(p)
    for s in specs:
        if ',' in s:
            a, l = s.split(',')
            addr, ln = int(a, 0), int(l, 0)
        else:
            addr, ln = int(s, 0), 4
        b = st.read(addr, ln)
        hexs = ' '.join(f'{x:02X}' for x in b)
        print(f'0x{addr:08X} len={ln}: {hexs}   u32={[hex(struct.unpack_from("<I", b, i)[0]) for i in range(0, max(0, ln - 3), 4)]}')
    return 0


def cmd_rows(args):
    p = args[0]
    count = int(args[1]) if len(args) > 1 else 12
    st = State(p)
    e = engine(st)
    ew = st.reg['wram']
    if e['rel'] is None:
        print('行表未知')
        return 1
    base = e['pool'] - 0x02000000 + e['rel']
    print(f'== {os.path.basename(p)} 行表@池内+0x{e["rel"]:X}  (游标项 {e["idx"]})')
    for i in range(0, count):
        o = struct.unpack_from('<I', ew, base + 4 * i)[0]
        if o == 0 or o > 0x10000:
            break
        codes = read_row(ew, e['pool'] - 0x02000000 + o)
        mark = ' <<<' if i == e['idx'] else '    '
        print(f'   {i:>4} off=0x{o:05X} n={len(codes):>3}{mark} {glyph_text(codes)}')
    return 0


def cmd_diff(args):
    region = 'wram'
    if '--region' in args:
        i = args.index('--region')
        region = args[i + 1]
        del args[i:i + 2]
    a, b = State(args[0]), State(args[1])
    ra, rb = a.reg[region], b.reg[region]
    print(f'== diff {os.path.basename(args[0])} -> {os.path.basename(args[1])} [{region}]')
    runs = []
    i = 0
    while i < len(ra):
        if ra[i] != rb[i]:
            j = i
            while j < len(ra) and ra[j] != rb[j]:
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    print(f'   {len(runs)} differing runs, {sum(n for _, n in runs)} bytes')
    for off, n in runs[:80]:
        base = GADDR[region] + off
        pa = ra[off:off + min(n, 16)]
        pb = rb[off:off + min(n, 16)]
        print(f'   0x{base:08X} len={n:<5} {pa.hex(" ")} -> {pb.hex(" ")}')
    return 0


def cmd_scan(args):
    st = State(args[0])
    pat = bytes.fromhex(args[1])
    for name in ('wram', 'iwram', 'vram'):
        buf = st.reg[name]
        start = 0
        while True:
            k = buf.find(pat, start)
            if k < 0:
                break
            print(f'{name} 0x{GADDR[name] + k:08X}')
            start = k + 1
    return 0


def main():
    cmd = sys.argv[1]
    args = sys.argv[2:]
    return {'where': cmd_where, 'entry': cmd_entry, 'peek': cmd_peek,
            'rows': cmd_rows, 'diff': cmd_diff, 'scan': cmd_scan}[cmd](args)


if __name__ == '__main__':
    sys.exit(main())
