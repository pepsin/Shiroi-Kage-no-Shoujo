#!/usr/bin/env python3
"""script_vm.py - 剧本字节码虚拟机静态分析器。

背景（2026-10 逆向结论，运行时游标 trace + handler 反汇编双重验证）
-------------------------------------------------------------------
每条剧本条目解压后，字符串池（条目内偏移 0x7A0 起）的**开头**不是行偏移表，
而是一段**剧本字节码流**；行偏移表在其后（entry_catalog 的 table_off），
再往后才是字符串。引擎用一个字节码 VM 逐条执行它：

    流游标 = EWRAM 0x0200E014（相对流基址的字节偏移，流基址 = 池基址）
    读取函数：0x08018DEC(+1) / 0x08018E08(+2) / 0x08018E74(+3) / 0x08018E24(+4)
    分派循环：0x08018AD0 —— 读 1 字节 opcode，线性查 58 项 {op, handler} 表
             （ROM 0x0807CB80），handler 返回 0 则同一拍继续执行下一条；
             **查不到表的 opcode 静默跳过 1 字节**（walker 复刻此行为）。
    流头 21 字节：[u8 类型][u32 = 0x15][4×u32 段偏移]，运行时实测一次性跳过。

关键 opcode（e438 葬礼场景实测：字节码推导的翻页节奏与无头模拟器逐帧截图、
行游标监视完全一致）：

    01 <u16 行号> <u8>   播放一行台词（行号 = 行偏移表下标 = 主表 idx）
    47 <u8 flag> [u16]   flag != 0 时再读 u16——也用于显示行（选项等）
    2E / 2F              0 操作数；**翻页/清屏 = 屏边界**
    82 <u16> <u8>        演出延时（u16*6 帧；开场自动播放靠它）
    50 <u24> <u16> <u24> 画面效果（8 字节操作数）
    91 <u8> <u8> <u8> <u16>  声音指令（5 字节操作数）
    9E <u32> / 9F        挂起跳转目标 / 执行跳转（游标改写）
    54 <u8> <u16>        相对跳转（目标 = u16 自身位置 + 偏移）
    52 / 53              变长（见 OPCODES 表内注）

用法：
    python3 tools/script_vm.py widths             # 静态推导各 opcode 前缀宽度
    python3 tools/script_vm.py walk <eid> [-v]    # 走一条目，打印屏分组
    python3 tools/script_vm.py check [-v]         # 全条目校验失步情况
    python3 tools/script_vm.py solve              # 数据驱动求解未知宽度
"""
import collections
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gbtext as g  # noqa: E402

JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')
BASE = 0x08000000
CURSOR_VAR = 0x0200E014
STREAM_TABLE = 0x0807CB80      # 剧情流 opcode 表（58 项）

# 流读取函数 -> 前进字节数
READERS = {0x08018DEC: 1, 0x08018E08: 2, 0x08018E74: 3, 0x08018E24: 4}
# 辅助函数（反汇编核定）：H1=0x0801C828 读 2（自身1+H5），H5=0x0801C7F0 读 1，
# H2=0x0801C8C0 读 0，H3=0x0801C870 读 1+2×count（变长），H4=0x0801D324 读 0。
HELPER_READS = {0x0801C828: 2, 0x0801C7F0: 1, 0x0801C8C0: 0, 0x0801D324: 0}
VAR_HELPERS = {0x0801C870}   # 变长：u8 count + count×2

# ------------------------------------------------------------------ 宽度表
# 规则：int = 固定操作数字节数；None = 未解（walker 按 VM 规则跳过 1 字节）；
#   'var47'  op 0x47: u8 flag，非 0 再 +2
#   'var52'  op 0x52: u24+u8+u16 + byte4(u8 count)×2 的内联表
#   'var53'  op 0x53: u16+u16+u8+u8 + byte6(u8 count)×7
#   ('m', {mode: w, ...}, default)  按第一个操作数字节分派的宽度
OPCODES = {
    # --- 已核定（运行时 trace / handler 精读）---
    0x01: ('row', 3),          # u16 行号 + u8
    0x2E: ('page', 0),         # 等 A + 清屏 = 屏边界
    0x2F: ('page2', 0),
    0x45: ('end', 0),          # 终止（handler 返回 2）
    0x82: ('delay', 3),        # u16*6 帧 + u8
    0x80: ('page80', 0),       # 等 A/菜单评估 + 换页（清屏变体）
    0x81: ('page81', 0),
    0x50: ('fx50', 8),         # u24 + u16 + u24
    0x47: ('rowopt', 'var47'),
    0x91: ('sound', 5),        # u8 通道 + u8 命令 + u8 参数 + u16
    0x66: ('mode66', 1),
    0x67: ('mode67', 1),
    0x68: ('mode68', 1),
    0x9E: ('setjmp', 4),
    0x9F: ('jmp', 0),
    0x43: ('op43', 2),
    0x44: ('op44', 3),
    0x52: ('case52', 'var52'),
    0x53: ('menu53', 'var53'),
    0x54: ('spawn54', 3),       # u8 count + u16 rel 描述表指针（表不在操作数里）
    # --- 前缀推导（待数据驱动复核）---
    0x46: ('op46', 0),
    0x49: ('op49', 0),
    0x48: ('op48', 1),
    0xD0: ('opD0', 1),
    0x5E: ('op5E', 1),
    0x5F: ('op5F', 1),
    0x60: ('op60', 3),
    0x61: ('op61', 1),
    0x62: ('op62', 1),
    0x63: ('op63', ('var62', 0)),
    0x64: ('op64', 1),
    0x65: ('op65', 1),
    0x6B: ('op6B', 'var6B'),     # u8 sub；sub==0 再 +1
    0x6C: ('op6C', 1),
    0x90: ('op90', 4),
    0x92: ('op92', 2),
    0x7F: ('op7F', 3),
    0x4C: ('op4C', 3),
    0x4D: ('op4D', 1),
    0x4E: ('op4E', 4),
    0x6D: ('op6D', 1),
    0x6E: ('op6E', 1),
    0x4B: ('op4B', 1),
    0x93: ('op93', 2),
    0x8C: ('op8C', 1),
    0x5A: ('op5A', ('m', {0x01: 8}, None)),
    0x5B: ('op5B', ('m', {0x01: 8}, None)),
    0x5C: ('op5C', 1),
    0x5D: ('op5D', 0),
    0x4A: ('op4A', 0),
    0x40: ('sect40', 3),       # u16+u8；跳 header 段槽（游标=u32@stream+5）
    0x41: ('op41', None),
    0x42: ('sect42', 3),       # u16+u8
    0x51: ('fx51', 8),         # u24+u16+u24（同 50 族）
    0x55: ('op55', 1),
    0x6F: ('dispatch6F', 2),     # 段派遣（跳转；操作数选中 header 段表项）
    0x70: ('setup70', 'var70'),  # u8 count + count×2（quiz/布置类）
}

# 跳转/派遣类 opcode：控制流断点（不关屏，屏边界只认 2E/2F）
JUMP_OPS = {0x54, 0x9E, 0x9F, 0x6F}

_rom = None


def rom():
    global _rom
    if _rom is None:
        _rom = open(JP_ROM, 'rb').read()
    return _rom


# ------------------------------------------------------------------ 静态分析
def load_op_table(addr=STREAM_TABLE):
    out = {}
    off = addr - BASE
    r = rom()
    while True:
        op, ptr = struct.unpack_from('<II', r, off)
        if not (op <= 0xFF and 0x08000001 <= ptr < 0x08080000 and ptr & 1):
            break
        out[op] = ptr & ~1
        off += 8
    return out


def disasm(addr, size=0x400):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    return list(md.disasm(rom()[addr - BASE:addr - BASE + size], addr))


def bl_target(ins_addr):
    h1, h2 = struct.unpack_from('<HH', rom(), ins_addr - BASE)
    if (h1 & 0xF800) != 0xF000 or (h2 & 0xF800) != 0xF800:
        return None
    s = (h1 >> 10) & 1
    val = ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
    if s:
        val -= 1 << 22
    return ins_addr + 4 + val


def derive_width(addr):
    """handler 的"直线前缀"（第一个分支指令之前）里的流读取总量。"""
    total = 0
    for ins in disasm(addr):
        if ins.mnemonic == 'bl':
            t = bl_target(ins.address)
            if t in READERS:
                total += READERS[t]
            elif t in HELPER_READS:
                total += HELPER_READS[t]
            elif t in VAR_HELPERS:
                return ('var', total)
            elif t:
                return ('unknown-call', total, t)
        if ins.mnemonic in ('b', 'beq', 'bne', 'bhs', 'blo', 'bgt', 'ble',
                            'bmi', 'bpl', 'bhi', 'bls'):
            return ('ok', total)
        if (ins.mnemonic == 'pop' and 'pc' in ins.op_str) or \
           (ins.mnemonic == 'bx' and ins.op_str == 'lr'):
            return ('ok', total)
    return ('ok', total)


# ------------------------------------------------------------------ walker
def _table_is_real(d, toff, base):
    """校验 catalog 里的 table_off 是不是真行表：连续升序 u32 且指向
    以 0x0000 结尾的合法字符串。e21 这类 pool 条目会被扫描器误报一个假表
    （字节码里的巧合升序段）。"""
    import struct as _s
    good = total = 0
    p = toff
    prev = -1
    while p + 4 <= len(d) and total < 4000:
        x = _s.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d) or x <= prev:
            break
        prev = x
        total += 1
        off = base + x
        if off + 2 > len(d):
            break
        # 串必须在条目内、以 0000 结尾、长度合理
        n = 0
        while off + 2 * n + 2 <= len(d) and \
                _s.unpack_from('<H', d, off + 2 * n)[0] != 0 and n < 40:
            n += 1
        if 0 < n < 40 and off + 2 * n + 2 <= len(d):
            good += 1
        p += 4
    return total >= 4 and good >= max(4, total * 3 // 4)


def _master_kind_counts(eid):
    """主表里该条目的 (表行数, pool 行数)。表行 = kind 列空；pool 行 = kind=pool。"""
    import csv
    master = os.path.join(ROOT, 'data', 'translation.tsv')
    ntab = npool = 0
    with open(master, encoding='utf-8', newline='') as f:
        for row in csv.reader(f, delimiter='\t'):
            if len(row) >= 7 and row[0] == str(eid) and row[0].isdigit():
                if len(row) > 7 and row[7] == 'pool':
                    npool += 1
                else:
                    ntab += 1
    return ntab, npool


def entry_bytecode(eid):
    """→ (bytecode bytes, nrows or None)。

    表格式条目：字节码 = pool[0 .. table_off]，nrows = 行表项数。
    pool 型条目：字节码 = pool[0 .. 主表最小串偏移]，nrows = 主表行数。
    catalog 会误报假行表（e21：表区实际是字节码），判定方法：
    主表该条目若**没有**表行（全部 kind=pool），catalog 的表就是假的。"""
    import csv
    cat = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    rows = {r['eid']: r for r in csv.DictReader(open(cat, encoding='utf-8'),
                                                delimiter='\t')}
    r = rows.get(str(eid))
    d = g.load_entry(rom(), eid)
    if d is None:
        return None, None
    ntab, npool = _master_kind_counts(eid)
    if r and r.get('table_off') and ntab > 0 and \
            _table_is_real(d, int(r['table_off']), int(r['base'])):
        toff = int(r['table_off'])
        return d[0x7A0:toff], int(r['strings'])
    # pool 型：字节码到行表/首串为止。catalog 没收录行表的 pool 条目
    # （e44 等）其实有真行表——表首项 = 首串偏移 − 0x7A0，按此定位。
    master = os.path.join(ROOT, 'data', 'translation.tsv')
    min_off = None
    with open(master, encoding='utf-8', newline='') as f:
        for row in csv.reader(f, delimiter='\t'):
            if len(row) >= 7 and row[0] == str(eid) and row[0].isdigit():
                off = int(row[2] or 0)
                if min_off is None or off < min_off:
                    min_off = off
    end = min_off if min_off and min_off > 0x7A0 else len(d)
    if min_off and ntab == 0:
        first_val = min_off - 0x7A0
        best = None
        for phase in (0x7A0, 0x7A1):
            p = phase
            while p + 4 <= end:
                if struct.unpack_from('<I', d, p)[0] == first_val:
                    # 测这个候选的升序 run 长度
                    n, q, prev = 0, p, -1
                    while q + 4 <= len(d):
                        x = struct.unpack_from('<I', d, q)[0]
                        if x > len(d) or x <= prev:
                            break
                        prev = x
                        n += 1
                        q += 4
                    if best is None or n > best[1]:
                        best = (p, n)
                    p = q if q > p else p + 2
                else:
                    p += 2      # 行表可能 2 字节对齐（奇数偏移）
        if best and best[1] >= 4:
            end = min(end, best[0])
    return d[0x7A0:end], ntab + npool


def op_size(bc, p, table, nrows=None):
    """返回 (事件种类, 总字节数 incl opcode, 解析结果)。未知 op → ('unknown',1)。

    bail 规则（实测推断）：显示类 op（01/47）的行号越界时，说明运行时处于
    菜单/分支模式，handler 会**不消费操作数直接让位**（0x0801D0D4 模式检查）——
    流作者故意让"操作数"也能被当 op 逐字节跳过（高位多为 00）。所以越界时
    walker 也只消费 opcode 字节本身，后续字节按 op 继续解析。"""
    op = bc[p]
    if op not in table:
        return ('unknown', 1, None)
    name, rule = table[op]
    if rule is None:
        return ('unknown', 1, None)
    if rule == 'var47':
        if p + 1 >= len(bc):
            return ('truncated', 1, None)
        flag = bc[p + 1]
        n = 1 + (2 if flag else 0)
        row = struct.unpack_from('<H', bc, p + 2)[0] if flag and p + 4 <= len(bc) else None
        if row is not None and nrows is not None and row >= nrows:
            return ('bail', 1, None)
        return ('rowopt', 1 + n, (row, flag))
    if rule == 'var52':
        if p + 6 > len(bc):
            return ('truncated', 1, None)
        n = 6 + 2 * bc[p + 4]
        return ('op', 1 + n, (op, bc[p + 1:p + 1 + n]))
    if rule == 'var53':
        if p + 7 > len(bc):
            return ('truncated', 1, None)
        n = 6 + 7 * bc[p + 6]
        return ('op', 1 + n, (op, bc[p + 1:p + 1 + n]))
    if isinstance(rule, tuple) and rule[0] == 'var62':
        # u8 n + n×3 + u8 + (65: u8) + u8 count + count×2
        extra = rule[1]
        if p + 2 > len(bc):
            return ('truncated', 1, None)
        n = bc[p + 1]
        q = p + 2 + 3 * n + extra
        if q + 2 > len(bc):
            return ('truncated', 1, None)
        count = bc[q + 1]
        size = (q + 2 + 2 * count) - p
        return ('op', size, (op, bc[p + 1:p + size]))
    if rule == 'var6B':
        if p + 2 > len(bc):
            return ('truncated', 1, None)
        n = 1 + (1 if bc[p + 1] == 0 else 0)
        return ('op', 1 + n, (op, bc[p + 1:p + 1 + n]))
    if rule == 'var70':
        if p + 2 > len(bc):
            return ('truncated', 1, None)
        n = 1 + 2 * bc[p + 1]
        return ('op', 1 + n, (op, bc[p + 1:p + 1 + n]))
    if isinstance(rule, tuple):          # ('m', {mode: w}, default)
        modes, default = rule[1], (rule[2] if len(rule) > 2 else None)
        mode = bc[p + 1] if p + 1 < len(bc) else None
        w = modes.get(mode, default)
        if w is None:
            return ('unknown', 1, None)
        return ('op', 1 + w, (op, bc[p + 1:p + 1 + w]))
    if p + 1 + rule > len(bc):
        return ('truncated', 1, None)
    raw = bc[p + 1:p + 1 + rule]
    if name == 'row':
        row = struct.unpack_from('<H', raw, 0)[0]
        if nrows is not None and row >= nrows:
            return ('bail', 1, None)
        return ('row', 1 + rule, (row, raw[2]))
    if name in ('page', 'page2', 'page80', 'page81', 'end'):
        return (name, 1 + rule, None)
    return ('op', 1 + rule, (op, raw))


def walk(bc, table=None, nrows=None, limit=200000):
    """走字节码流，产出事件列表。

    事件：('row', pos, 行号, u8flag) / ('rowopt', pos, 行号orNone, flag)
          ('page', pos) / ('page2', pos) / ('end', pos) / ('bail', pos)
          ('op', pos, opcode, raw) / ('unknown', pos, byte) / ('truncated', pos, op)
    """
    if table is None:
        table = OPCODES
    ev = []
    p = 0x15      # 头部 21 字节一次性跳过（运行时实测）
    if len(bc) < 0x15:
        return ev
    while p < len(bc) and limit > 0:
        limit -= 1
        kind, size, res = op_size(bc, p, table, nrows)
        if kind == 'row':
            ev.append(('row', p, res[0], res[1]))
        elif kind == 'rowopt':
            ev.append(('rowopt', p, res[0], res[1]))
        elif kind in ('page', 'page2', 'page80', 'page81', 'end', 'bail'):
            ev.append((kind, p))
        elif kind == 'op':
            ev.append(('op', p, res[0], res[1]))
        elif kind == 'truncated':
            ev.append(('truncated', p, bc[p]))
        else:
            ev.append(('unknown', p, bc[p]))
        p += size
        # 注意：0x45 不在此停走——数据区里也会出现 45 字节（线性走法把它当
        # 普通事件，主流真正的终止符只是其中一个）。
    return ev


def pages_of(events, nrows=None):
    """→ [(page_no, [行号...], trusted)]；nrows 给定时过滤越界行号（失步噪声）。

    trusted = 这一屏区间内所有字节都被已知宽度的 opcode 覆盖（没有 unknown /
    truncated 事件）。不可信的屏通常是分支/内嵌数据区，线性走法在那里会产生
    噪声——上层只应采用 trusted 屏。"""
    pages, cur = [], []
    trust = True
    optab = load_op_table()
    for e in events:
        if e[0] == 'row':
            if nrows is None or e[2] < nrows:
                cur.append(e[2])
        elif e[0] == 'rowopt' and e[2] is not None:
            if nrows is None or e[2] < nrows:
                cur.append(e[2])
        elif e[0] == 'truncated' or e[0] == 'bail':
            trust = False
        elif e[0] == 'unknown' and e[2] in optab:
            # 表内但宽度未知的字节：VM 与 walker 解读可能分叉 → 不可信。
            # 表外字节双方同样跳 1 字节，不影响可信度。
            trust = False
        elif e[0] in ('page', 'page2', 'page80', 'page81'):
            pages.append((cur, trust))
            cur = []
            trust = True
    if cur:
        pages.append((cur, trust))
    return [(i, pg, tr) for i, (pg, tr) in enumerate(pages)]




# ------------------------------------------------------------------ CFG walker
def _u32(bc, off):
    if 0 <= off and off + 4 <= len(bc):
        return struct.unpack_from('<I', bc, off)[0]
    return None


def walk_cfg(bc, table=None, nrows=None, limit=200000):
    """按控制流走字节码（BFS）。只解析**可达**位置的指令；数据 blob 和
    死代码（无条件跳转的落透区）自然被排除，不再产生噪声行。

    边规则：
      普通 op        -> p+size
      0x9E           -> u32 绝对跳转（无落透）
      0x43 N         -> 段表计算跳转：u32@(stream+u32@0x11 + 6N + 4)
      0x40           -> u32@stream+5（header 段槽 1）
      0x54           -> 操作数 u16 位置 + rel（描述表/代码表）
      0x45 (end)     -> 无出边
      其它（含 9F）  -> 落透（9F 的条件目标依赖运行态，静态只走落透）
    根：0x15（主流入口）+ header 的 4 个段偏移（恢复用入口）。
    """
    if table is None:
        table = OPCODES
    events = {}
    roots = [0x15]
    for k in range(4):
        v = _u32(bc, 5 + 4 * k)
        if v and 0x15 <= v < len(bc):
            roots.append(v)
    work = collections.deque(r for r in dict.fromkeys(roots))
    seen = set()
    nsteps = 0
    while work and nsteps < limit:
        p = work.popleft()
        while 0 <= p < len(bc) and p not in seen and nsteps < limit:
            seen.add(p)
            nsteps += 1
            kind, size, res = op_size(bc, p, table, nrows)
            op = bc[p]
            if kind == 'row':
                events[p] = ('row', p, res[0], res[1])
            elif kind == 'rowopt':
                events[p] = ('rowopt', p, res[0], res[1])
            elif kind in ('page', 'page2', 'page80', 'page81', 'end', 'bail'):
                events[p] = (kind, p)
            elif kind == 'op':
                events[p] = ('op', p, res[0], res[1])
            elif kind == 'truncated':
                events[p] = ('truncated', p, bc[p])
            else:
                events[p] = ('unknown', p, bc[p])
            if kind == 'end':
                break
            # 控制流出边
            if op == 0x9E and size >= 5:
                t = _u32(bc, p + 1)
                if t is not None and t not in seen:
                    work.append(t)
                break                      # 无条件跳转：无落透
            if op == 0x43 and size >= 3:
                tab = _u32(bc, 0x11)
                n = struct.unpack_from('<H', bc, p + 1)[0]
                t = _u32(bc, tab + 6 * n + 4) if tab is not None else None
                if t is not None and t not in seen:
                    work.append(t)
                break
            if op == 0x40:
                t = _u32(bc, 5)
                if t is not None and t not in seen:
                    work.append(t)
                break
            if op == 0x54 and size >= 4:
                rel = struct.unpack_from('<H', bc, p + 2)[0]
                t = (p + 2) + rel          # r8 = u16 操作数位置
                if 0 <= t < len(bc) and t not in seen:
                    work.append(t)
                # 落透（case 区）也走，over-approx
            p += size
    return [events[k] for k in sorted(events)]


def pages_of_cfg(events, nrows=None):
    """CFG 事件的屏分组（events 已按位置排序）。同一行出现在多个分支时取
    第一次出现的屏。"""
    pages, cur, trust = [], [], True
    seen_rows = set()
    for e in events:
        if e[0] in ('row', 'rowopt') and e[2] is not None:
            if nrows is None or e[2] < nrows:
                if e[2] not in seen_rows:
                    cur.append(e[2])
                    seen_rows.add(e[2])
        elif e[0] in ('unknown', 'truncated'):
            trust = False
        elif e[0] in ('page', 'page2', 'page80', 'page81'):
            pages.append((cur, trust))
            cur, trust = [], True
    if cur:
        pages.append((cur, trust))
    return [(i, pg, tr) for i, (pg, tr) in enumerate(pages)]


# ------------------------------------------------------------------ 校验
def score_entry(bc, nrows, table=None):
    """→ (clean, stats)：walker 校验一条目。

    clean 判据：无越界行号、有屏边界、行覆盖率 ≥ 60%。
    （未知字节不作判据：VM 本来就静默跳过表外字节，流里合法存在。）
    """
    ev = walk(bc, table, nrows)
    unk = [e for e in ev if e[0] == 'unknown']
    rows = [e[2] for e in ev if e[0] in ('row', 'rowopt') and e[2] is not None]
    oor = [x for x in rows if x >= nrows]
    npages = sum(1 for e in ev if e[0] in ('page', 'page2', 'page80', 'page81'))
    ended = any(e[0] == 'end' for e in ev)
    cover = len(set(x for x in rows if x < nrows)) / max(1, nrows)
    clean = (not oor) and npages > 0 and cover >= 0.6
    return clean, {'rows': len(rows), 'oor': len(oor), 'pages': npages,
                   'ended': ended, 'unk': len(unk), 'cover': cover,
                   'unk_ops': sorted({e[2] for e in unk})}


def iter_table_entries():
    """全部有字节码的条目（表格式 + pool 型）→ (eid, bc, nrows)。"""
    import csv
    cat = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    eids = set()
    for r in csv.DictReader(open(cat, encoding='utf-8'), delimiter='\t'):
        if r.get('dec') == '1':
            eids.add(int(r['eid']))
    for eid in sorted(eids):
        bc, nrows = entry_bytecode(eid)
        if bc and nrows:
            yield eid, bc, nrows


def cmd_check(a):
    clean = dirty = 0
    unk_counter = collections.Counter()
    for eid, bc, nstr in iter_table_entries():
        ok, st = score_entry(bc, nstr)
        if ok:
            clean += 1
        else:
            dirty += 1
            unk_counter.update(st['unk_ops'])
            if a.verbose:
                print(f"e{eid}: rows={st['rows']} 越界={st['oor']} "
                      f"pages={st['pages']} end={st['ended']} unk={st['unk']} "
                      f"{[f'{o:02X}' for o in st['unk_ops'][:10]]}")
    print(f'clean={clean} dirty={dirty}')
    print('高频未知字节:', ' '.join(f'{b:02X}×{c}' for b, c in unk_counter.most_common(15)))
    return 0


# ------------------------------------------------------------------ 求解器
def candidate_rules(op):
    """未解 opcode 的候选宽度规则。"""
    out = [('fixed', w) for w in range(0, 13)]
    for w in range(0, 13):
        out.append(('mode', w))   # 宽度 = f(第一操作数字节) 的特例暂不自动生成
    return out


def clean_prefix(bc, nrows, table=None):
    """从头走，返回首个失步点之前干净解析的字节数（梯度指标）。"""
    ev = walk(bc, table, nrows)
    run = 0
    for e in ev:
        if e[0] == 'unknown':
            run += 1
            if run >= 4:
                return e[1] - 3
        else:
            run = 0
        if e[0] in ('row', 'rowopt') and e[2] is not None and e[2] >= nrows:
            return e[1]
        if e[0] == 'truncated':
            return e[1]
    return len(bc)


def cmd_solve(a):
    """迭代：找失步点最常疑似的未解 op → 试候选宽度 → 以"干净前缀总长"为梯度锁定。"""
    table = dict(OPCODES)
    entries = list(iter_table_entries())

    def total_prefix(tbl):
        return sum(clean_prefix(bc, nstr, tbl) for _, bc, nstr in entries)

    base_score = total_prefix(table)
    print(f'初始干净前缀总长: {base_score}')
    optab = load_op_table()
    for it in range(a.rounds):
        offenders = collections.Counter()
        for eid, bc, nstr in entries:
            ev = walk(bc, table)
            run_start = None
            for i, e in enumerate(ev):
                bad = False
                if e[0] == 'unknown':
                    if run_start is None:
                        run_start = i
                    if i - run_start >= 3:
                        bad = True
                else:
                    run_start = None
                if e[0] in ('row', 'rowopt') and e[2] is not None and e[2] >= nstr:
                    bad = True
                    if run_start is None:
                        run_start = i
                if e[0] == 'truncated':
                    bad = True
                    if run_start is None:
                        run_start = i
                if bad:
                    first = ev[run_start]
                    if first[0] == 'unknown' and first[2] in optab:
                        offenders[first[2]] += 1
                        break
                    for j in range(run_start - 1, max(0, run_start - 10), -1):
                        pj = ev[j]
                        if pj[0] == 'op':
                            offenders[pj[2]] += 1
                            break
                        if pj[0] in ('row', 'rowopt') and pj[2] is not None \
                           and pj[2] < nstr:
                            break
                    break
        if not offenders:
            print('没有更多失步点')
            break
        op, cnt = offenders.most_common(1)[0]
        curname = OPCODES.get(op, (f'op{op:02X}', None))[0]
        best = None
        for w in range(0, 15):
            trial = dict(table)
            trial[op] = (curname, w)
            sc = total_prefix(trial)
            if best is None or sc > best[1]:
                best = (w, sc)
        if best[1] <= base_score:
            print(f'第 {it} 轮: op 0x{op:02X} 无解提升（最优宽 {best[0]} '
                  f'score={best[1]} ≤ 当前 {base_score}），留 None')
            table[op] = (curname, None)
            # 把它从候选项里永久排除，避免空转
            OPCODES[op] = (curname, 0)
            table[op] = (curname, 0)
            continue
        table[op] = (curname, best[0])
        base_score = best[1]
        print(f'第 {it} 轮: op 0x{op:02X} -> 宽 {best[0]}  干净前缀总长={best[1]}')
    print('\n求解后表:')
    for op in sorted(table):
        print(f'    0x{op:02X}: {table[op]},')
    return 0


# ------------------------------------------------------------------ 命令
def cmd_emit(a):
    """生成 data/page_map.tsv：entry / idx / page / trusted。

    page = 条目内屏序号（p0, p1, ...）。同一行可能在多个分支屏里出现，
    规则：有可信屏则取首个可信屏，否则取首个出现（trusted=0）。"""
    import csv
    out = os.path.join(ROOT, 'data', 'page_map.tsv')
    n_mapped = n_rows = 0
    with open(out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['entry', 'idx', 'page', 'trusted'])
        for eid, bc, nstr in iter_table_entries():
            ev = walk(bc, nrows=nstr)
            first_trusted = {}
            first_any = {}
            for pno, pg, trust in pages_of(ev, nstr):
                for row in pg:
                    if trust and row not in first_trusted:
                        first_trusted[row] = pno
                    if row not in first_any:
                        first_any[row] = (pno, trust)
            for row in sorted(set(first_any) | set(first_trusted)):
                if row in first_trusted:
                    w.writerow([eid, row, f'p{first_trusted[row]}', 1])
                else:
                    pno, trust = first_any[row]
                    w.writerow([eid, row, f'p{pno}', 0])
                n_mapped += 1
            n_rows += nstr
    print(f'wrote {out}: {n_mapped} 行映射 / 全表约 {n_rows} 行')
    return 0


def cmd_widths(a):
    for op, haddr in load_op_table().items():
        w = derive_width(haddr)
        known = OPCODES.get(op)
        print(f'op 0x{op:02X} h=0x{haddr:08X} 前缀推导={w} 当前表={known}')


def cmd_walk(a):
    bc, toff = entry_bytecode(a.eid)
    if bc is None:
        print(f'e{a.eid}: 无法加载')
        return 1
    import csv
    nrows = None
    cat = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    for r in csv.DictReader(open(cat, encoding='utf-8'), delimiter='\t'):
        if r['eid'] == str(a.eid) and r.get('strings'):
            nrows = int(r['strings'])
    ev = walk(bc, nrows=nrows)
    pages = pages_of(ev, nrows)
    nro = sum(1 for e in ev if e[0] in ('row', 'rowopt'))
    unk = [e for e in ev if e[0] == 'unknown']
    print(f'e{a.eid}: 字节码 {len(bc)} 字节, 事件 {len(ev)}, '
          f'row 事件 {nro}, 屏数 {len(pages)}, 未知 opcode {len(unk)}')
    if a.verbose:
        for e in ev[:a.limit]:
            print(' ', e)
    else:
        for i, pg, tr in pages[:a.limit]:
            print(f'  page {i:>3}: rows {pg}{"" if tr else "  (不可信)"}')
    if unk:
        seen = sorted({e[2] for e in unk})
        print('  未知字节:', ' '.join(f'{b:02X}' for b in seen[:20]))
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('widths').set_defaults(func=cmd_widths)
    w = sub.add_parser('walk')
    w.add_argument('eid', type=int)
    w.add_argument('--verbose', '-v', action='store_true')
    w.add_argument('--limit', type=int, default=40)
    w.set_defaults(func=cmd_walk)
    c = sub.add_parser('check')
    c.add_argument('--verbose', '-v', action='store_true')
    c.set_defaults(func=cmd_check)
    s = sub.add_parser('solve')
    s.add_argument('--rounds', type=int, default=12)
    s.set_defaults(func=cmd_solve)
    e = sub.add_parser('emit')
    e.set_defaults(func=cmd_emit)
    a = ap.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())
