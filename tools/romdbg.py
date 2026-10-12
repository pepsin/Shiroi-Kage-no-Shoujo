#!/usr/bin/env python3
"""romdbg.py - 剧本调试器（配套 work/bin/gbarun_dbg 无头 mGBA harness）。

本工具只做**只读**的事：看引擎在演哪一条目/哪一行、按脚本按键复跑、截图。

引擎侧的已知事实（详见 docs/调试器.md）
--------------------------------------
本作的剧本资源（FAT 条目）解压后：头部 `0x000`、流程/标志区 `0x680`、
字符串池 `0x7A0` 起（池内先放字节码流，再接行偏移表，最后是各行文本）。

引擎把整条资源解压进 **EWRAM 0x02030300**，并在 `0x02003F24` 维护一个描述符：

    0x02003F24  u32  字符串池基址（= 0x02030300 + 0x7A0）
    0x02003F2C  u32  资源缓冲基址（= 0x02030300）
    0x02003F34  u32  keyB
    0x02003F38  u32  keyCount（剧本分发参数，不等于对白行数）

显示时引擎从「行偏移表」里顺序取一项：`行地址 = 池基址 + u32[表项]`，
游标每次 +4。IWRAM `0x03007C40` 保存「刚读完的表项偏移 + 2」，
`0x03007C44` 保存「刚取到的行偏移」，所以运行中随时能读出「现在在演第几行」。

「现在演哪个条目」由 `tools/entry_id.py` 反查（描述符的 keyCount/keyB 当钥匙
去 ROM 分发表筛候选，再和 RAM 缓冲逐字节比对，见该文件说明）。

用法
----
  tools/romdbg.py list  --entry 500                 # 按引擎顺序列出某条目的行
  tools/romdbg.py find  --text 病死的               # 反查某句话在哪个条目/第几行
  tools/romdbg.py where --state work/dbg/base.ss1   # 报告存档里的条目 / 场景 / 行
  tools/romdbg.py base                              # 生成/刷新基准存档（冷启动到第一段对白）
  tools/romdbg.py shot  a.ppm b.png                 # PPM → PNG（放大可选）
  tools/romdbg.py raw ...                           # 直接透传给 gbarun_dbg

> 为什么**没有**「跳到任意一行」的功能：唯一的实现方式是「文本级注入」——把目标行
> 塞进当前已载入剧本的行表。那样背景/在场人物/分支标志都还是当前那一幕的，看着像
> 跳过去了，其实什么都没验证到，只会误导排查（卡死、花屏、剧情不对都可能被它掩盖）。
> 要真跳转，得走引擎自己的场景切换，见 `docs/调试器.md` 第四节。
> 这个坑踩过一次，代码已删除，不要再加回来。
"""
import argparse
import csv
import glob
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g        # noqa: E402
import entry_id           # noqa: E402
import scene_lookup       # noqa: E402

CN_ROM = os.path.join(ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba')
JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')
HARNESS = os.path.join(ROOT, 'work', 'bin', 'gbarun_dbg')
EXT_MAP = os.path.join(ROOT, 'work', 'glyph_map.ext.csv')
DBGDIR = os.path.join(ROOT, 'work', 'dbg')
ROM_BYTES = b''           # main() 里读入 CN_ROM，给 entry 反查用

# 引擎/工具在 RAM 里的约定（见 docs/调试器.md）
DESCRIPTOR = 0x02003F24      # pool base, rom ptr, buf base, buf base, ?, rows, ?
CURSOR_ADDR = 0x03007C40     # u32: 刚读完的「行偏移表项」偏移 + 2
CURRENT_ADDR = 0x03007C44    # u32: 刚取到的「行偏移」= pool 内相对偏移
POOL_BASE_RAM = 0x02030AA0   # 也就是 0x02030300 + 0x7A0


# --------------------------------------------------------------------- 字库
def load_ext_map(path=EXT_MAP):
    m = {}
    for r in csv.DictReader(open(path, encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            m[int(r['code'], 16)] = ch
    return m


EXT = {}
BLANK = set()


def load_blank_glyphs(rom_path):
    """字库里全 0 的字形（转写表里记作 □）其实是空白，显示成空格更好读。"""
    rom = open(rom_path, 'rb').read()
    off, size = struct.unpack_from('<2I', rom, g.FAT + 850 * 8)
    font = rom[g.BASE + off: g.BASE + off + size]
    for i in range(size // 0x80):
        if not any(font[i * 0x80:(i + 1) * 0x80]):
            BLANK.add(i)


def glyph_text(codes):
    """按**本工程的工具约定**解码，结果与 data/translation.tsv 完全一致：

        code <  0x20 -> 表[code]      （与实机显示相差一格，见 docs/技术说明.md 第 10 节）
        code >= 0x20 -> 表[code - 1]
    """
    out = []
    for c in codes:
        idx = c if c < 0x20 else c - 1
        if c == 0:
            out.append('|')
        elif idx in BLANK:
            out.append('␣')
        else:
            out.append(EXT.get(idx, f'<{idx:03X}>'))
    return ''.join(out)


# 引擎自己的「剧本分发表」：ROM 文件 0x15C004 起，509 条 * 8 字节
#   { s16 entryId, s16 key_count, s16 key_a, s16 key_b }
# 引擎调用 loadScript(key_count, key_a, key_b) -> 线性查这张表 -> 得到 entryId
# -> getEntryAddress(entryId) -> 解压 -> 播放。表项下标 = entryId - 1。
DISPATCH_FILE = 0x15C004
DISPATCH_COUNT = 509


def dispatch_table(rom):
    out = []
    for k in range(DISPATCH_COUNT):
        e, c, a, b = struct.unpack_from('<4h', rom, DISPATCH_FILE + 8 * k)
        out.append((e, c, a, b))
    return out


def dispatch_lookup(rom, entry):
    for k, r in enumerate(dispatch_table(rom)):
        if r[0] == entry:
            return k, r
    return None, None


# ----------------------------------------------------------------- 条目结构
def entry_table(d, limit=4000):
    """找条目里的「行偏移表」：最长的升序 u32 run（对齐 0/1/2/3 都要试）。"""
    best = (0, 0, [])
    for phase in (0, 1, 2, 3):
        p = phase
        while p + 16 <= len(d):
            vals = []
            q = p
            while q + 4 <= len(d):
                x = struct.unpack_from('<I', d, q)[0]
                if x > len(d) or (vals and x <= vals[-1]):
                    break
                vals.append(x)
                q += 4
            if len(vals) > best[0]:
                best = (len(vals), p, vals)
            p = q if q > p else p + 4
    n, off, vals = best
    return (off, vals[:limit]) if n >= 4 else (None, [])


def read_row(d, off):
    if off < 0 or off + 2 > len(d):
        return []
    n = 0
    while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
        n += 1
    return list(struct.unpack_from(f'<{n}H', d, off))


def entry_rows(rom, eid):
    """→ [(表项下标, 池内相对偏移, 码位, 引擎显示文本)]，顺序 = 引擎播放顺序。"""
    d = g.load_entry(rom, eid)
    if not d:
        return None, []
    toff, vals = entry_table(d)
    if toff is None:
        return d, []
    rows = []
    for i, o in enumerate(vals):
        off = 0x7A0 + o
        if off + 2 > len(d):
            continue
        codes = read_row(d, off)
        rows.append((i, o, codes, glyph_text(codes)))
    return d, rows


# ------------------------------------------------------------- 运行 harness
def run_harness(args, env_extra=None, capture=True):
    if not os.path.exists(HARNESS):
        raise SystemExit(
            f'缺少 {HARNESS}，先编译：\n'
            '  clang -O2 -o work/bin/gbarun_dbg legacy/gbarun_dbg.c \\\n'
            '        -I/opt/homebrew/include -L/opt/homebrew/lib -lmgba -lm -lpthread')
    env = dict(os.environ)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items() if v is not None})
    cmd = [HARNESS] + [str(a) for a in args]
    if capture:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True)
        out = p.stdout + p.stderr
        return p.returncode, out
    return subprocess.call(cmd, env=env), ''


def ram_snapshot(state, frames, prefix):
    """跑 frames 帧并抓 EWRAM/IWRAM 快照 → (ewram, iwram, harness 输出)"""
    rc, out = run_harness([CN_ROM, frames, prefix + '.ppm'],
                          {'GBARUN_STATE': state,
                           'GBARUN_RAMSNAP': f'{frames - 1}:{prefix}'})
    ew = open(prefix + '.ewram', 'rb').read()
    iw = open(prefix + '.iwram', 'rb').read()
    return ew, iw, out


def ram_table_start(ew, pool, cursor):
    """EWRAM 快照里，游标所在的那张「行偏移表」的起始偏移（池内相对）。

    表是一段严格递增的 u32；已知游标指向的表项地址，就往前走到不递增为止。
    """
    if cursor < 2:
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


def state_cursor(ew, iw):
    """→ (pool_base, buf_base, rows, cursor, current_line_offset)"""
    pool, romptr, buf, buf2, unk, rows = struct.unpack_from('<6I', ew, 0x3F24)
    cur = struct.unpack_from('<I', iw, 0x7C40)[0]
    line = struct.unpack_from('<I', iw, 0x7C44)[0]
    return pool, buf, rows, cur, line


# ------------------------------------------------------------------- 命令
def cmd_dispatch(a):
    rom = open(a.rom, 'rb').read()
    tbl = dispatch_table(rom)
    if a.entry:
        idx, r = dispatch_lookup(rom, a.entry)
        if r is None:
            print(f'条目 {a.entry} 不在分发表里（引擎不会用 loadScript 载入它）')
            return 1
        print(f'条目 {a.entry}: 表项 #{idx}  entryId={r[0]}  loadScript({r[1]}, {r[2]}, {r[3]})')
        return 0
    for k, r in enumerate(tbl):
        if a.start <= r[0] < a.start + a.count:
            print(f'#{k:3d}  entryId={r[0]:>4}  loadScript({r[1]}, {r[2]}, {r[3]})')
    return 0


def cmd_list(a):
    rom = open(a.rom, 'rb').read()
    d, rows = entry_rows(rom, a.entry)
    if d is None:
        print(f'entry {a.entry}: 没有数据')
        return 1
    toff, vals = entry_table(d)
    print(f'# 条目 {a.entry}: 解压后 {len(d)} 字节，行偏移表 @0x{toff:X}（{len(vals)} 行）')
    print(f'# 池基址 0x7A0，行表在池内 +0x{toff - 0x7A0:X}')
    for i, o, codes, txt in rows:
        if i < a.start or i >= a.start + a.count:
            continue
        print(f'{i:>4}  off=0x{o:05X}  n={len(codes):>2}  {txt}')
    return 0


def cmd_find(a):
    rom = open(a.rom, 'rb').read()
    hits = 0
    for eid in range(0, a.max_entry):
        d, rows = entry_rows(rom, eid)
        if not rows:
            continue
        for i, o, codes, txt in rows:
            if a.text in txt:
                print(f'条目 {eid:>5}  第 {i:>4} 行  off=0x{o:05X}  {txt}')
                hits += 1
                if hits >= a.limit:
                    return 0
    print(f'(共 {hits} 处)')
    return 0 if hits else 1


def cmd_where(a):
    os.makedirs(DBGDIR, exist_ok=True)
    ew, iw, out = ram_snapshot(a.state, a.frames, os.path.join(DBGDIR, 'where'))
    pool, buf, rows, cur, line = state_cursor(ew, iw)
    rel = ram_table_start(ew, pool, cur)
    idx = (cur - 2 - rel) // 4 if (rel is not None and cur >= 2) else None
    print(f'池基址      0x{pool:08X}')
    print(f'资源缓冲    0x{buf:08X}')
    print(f'keyCount    {rows}')
    print(f'行表位置    池内 +0x{rel:X}' if rel is not None else '行表位置    未知')
    print(f'游标        0x{cur:04X}   → 刚读完第 {idx} 项' if idx is not None
          else f'游标        0x{cur:04X}')
    print(f'当前行偏移  0x{line:04X}')
    codes = read_row(ew, pool - 0x02000000 + line)
    print(f'当前行文本  {glyph_text(codes)}')

    # entry 号反查（见 tools/entry_id.py）：把「演到哪一行」挂到项目坐标系上
    if not (0x02020000 <= pool < 0x02040000 and line < 0x20000):
        print('条目        引擎未在演剧本（菜单 / 黑屏 / 过场中），跳过反查')
        return 0
    m = entry_id.resolve_from_ram(entry_id.RomIndex(ROM_BYTES), ew)[0]
    if m.eid is None:
        print(f'条目        反查失败（候选 {len(m.candidates)} 个）')
        return 0
    lk = scene_lookup.Lookup()
    row = lk.at_offset(m.eid, line)
    scene = row['scene'] if row else lk.best_scene(m.eid, None)
    conf = '结构区全中' if m.structural_complete else f'结构区 {m.structural_hits}/{m.structural_total}'
    print(f'条目        e{m.eid}   场景 {scene}   [{conf}，窗口 {m.hits}/{m.total}]')
    if row:
        print(f'该行        {row["rid"]}  jp={row["jp"]}')
        print(f'{"":>12}cn={row["cn"]}')
    return 0


def ensure_base(a):
    """生成「冷启动、第一段对白」的基准存档，给 play/where 当起点用。"""
    os.makedirs(DBGDIR, exist_ok=True)
    base = a.base_state or os.path.join(DBGDIR, 'base.ss1')
    if os.path.exists(base) and not a.force:
        return base
    rc, out = run_harness([CN_ROM, 5460, os.path.join(DBGDIR, 'base.ppm'),
                           'START@5000:4', 'A@5300:4'],
                          {'GBARUN_SAVE': base})
    if not os.path.exists(base):
        print('生成基准存档失败：')
        print(out)
        return None
    print(f'[base] 已生成 {base}')
    return base




def cmd_shot(a):
    from PIL import Image
    im = Image.open(a.src).convert('RGB')
    if a.scale != 1:
        im = im.resize((im.width * a.scale, im.height * a.scale), Image.NEAREST)
    im.save(a.dst)
    print(f'{a.dst} {im.size}')
    return 0


def cmd_play(a):
    keys = a.keys.split() if isinstance(a.keys, str) else list(a.keys)
    args = [CN_ROM, a.frames, a.out + '.ppm'] + keys
    env = {'GBARUN_SHOT_EVERY': a.shot_every}
    if a.state:
        env['GBARUN_STATE'] = a.state
    if a.save:
        env['GBARUN_SAVE'] = a.save
    rc, out = run_harness(args, env)
    shots = sorted(glob.glob(a.out + '.ppm_shot*.ppm'))
    print(f'# 末帧 {a.out}_final.ppm，过程截图 {len(shots)} 张（{a.out}.ppm_shot*.ppm）')
    return 0


def cmd_raw(a):
    rest = a.rest
    if rest and rest[0] == '--':
        rest = rest[1:]
    return subprocess.call([HARNESS] + rest)


def cmd_base(a):
    b = ensure_base(a)
    return 0 if b else 1


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rom', default=CN_ROM)
    sub = ap.add_subparsers(dest='cmd', required=True)

    l = sub.add_parser('list', help='按引擎顺序列出条目里的对白行')
    l.add_argument('--entry', type=int, required=True)
    l.add_argument('--start', type=int, default=0)
    l.add_argument('--count', type=int, default=40)
    l.set_defaults(func=cmd_list)

    f = sub.add_parser('find', help='按文本反查条目/行号')
    f.add_argument('--text', required=True)
    f.add_argument('--max-entry', type=int, default=1400)
    f.add_argument('--limit', type=int, default=20)
    f.set_defaults(func=cmd_find)

    d = sub.add_parser('dispatch', help='查引擎的剧本分发表（file 0x15C004）')
    d.add_argument('--entry', type=int, default=0, help='条目号；不给就按范围列')
    d.add_argument('--start', type=int, default=1)
    d.add_argument('--count', type=int, default=20)
    d.set_defaults(func=cmd_dispatch)

    w = sub.add_parser('where', help='报告存档里正在演哪一行')
    w.add_argument('--state', required=True)
    w.add_argument('--frames', type=int, default=200)
    w.add_argument('--ram', action='store_true', default=True)
    w.set_defaults(func=cmd_where)

    b = sub.add_parser('base', help='生成/刷新基准存档')
    b.add_argument('--base-state', default='')
    b.add_argument('--force', action='store_true')
    b.set_defaults(func=cmd_base)

    s = sub.add_parser('shot', help='PPM → PNG')
    s.add_argument('src')
    s.add_argument('dst')
    s.add_argument('--scale', type=int, default=1)
    s.set_defaults(func=cmd_shot)

    pl = sub.add_parser('play', help='载入存档/冷启动跑一段，按脚本按键并截图')
    pl.add_argument('--state', default='')
    pl.add_argument('--frames', type=int, default=1200)
    pl.add_argument('--keys', default='', help='如 "A@60:4 A@120:4"')
    pl.add_argument('--shot-every', type=int, default=60)
    pl.add_argument('--out', default=os.path.join(DBGDIR, 'play'))
    pl.add_argument('--save', default='')
    pl.set_defaults(func=cmd_play)

    r = sub.add_parser('raw', help='直接调用 gbarun_dbg')
    r.add_argument('rest', nargs=argparse.REMAINDER)
    r.set_defaults(func=cmd_raw)
    return ap


def main():
    global EXT, ROM_BYTES
    EXT = load_ext_map()
    load_blank_glyphs(CN_ROM)
    ROM_BYTES = open(CN_ROM, 'rb').read()
    os.makedirs(DBGDIR, exist_ok=True)
    ap = build_parser()
    a = ap.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())
