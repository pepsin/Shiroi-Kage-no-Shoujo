#!/usr/bin/env python3
"""模拟游戏读档时的判定：这份 .sav 会不会被游戏判成「无法读取 / 已损坏」。

游戏的两道判定（逆向自日文原版 ROM，见 docs/存档格式.md）
--------------------------------------------------------
存档设备是 **64 kbit EEPROM（8 KB = 1024 个 8 字节块）**；`.sav` 是 EEPROM 镜像，
但**每个 8 字节块整体倒序**（EEPROM 是 MSB-first 串行传输）。还原后：

    头（文件 0x00..0x17 = 块 0..2，24 字节）
        内存镜像 [u32 校验和][u32 魔数 0x0DECADE0][w2][w3][w4][w5]
        0x08015C14：读这 24 字节 →
            base[1] != 0x0DECADE0 → 写回魔数、把 base[8] 的 bit0/1/2（「有存档」标志）
                                     清掉，返回 0（调用方 0x0801523E 会重建头）
            否则返回 (base[0] == base[1]+base[2]+base[3]+base[4]+base[5]) mod 2^32
    槽 i（文件 0x20 + 2720*i = 块 340*i+4，340 个块 = 2720 字节）
        内存镜像 [u32 校验和][2716 字节负载]
        0x08015DFC：读这 2720 字节 → 返回 (chk == 负载 679 个 u32 之和 mod 2^32)
    读档 0x08015AD0：槽校验 0 → 状态机置 7 → 显示 e417 第 9 条
                     「このファイルは壊れています」；1 → 解包 → 置 8。

也就是说：**游戏只看这两处加法校验和，不看 ROM 的任何数据**。

用法
----
  save_check.py a.sav [b.sav ...]     逐项报告 + 结论（默认）
  save_check.py a.sav --brief         每个文件只打一行结论
  save_check.py --selftest            查仓库里现有的 .sav
  save_check.py a.sav --resign        只重算校验和（不改内容）
  save_check.py a.sav --repair        重算校验和 + 补魔数 / 修「有存档」标志位
退出码
  0  游戏能读到至少一个有效存档
  1  游戏读不到有效存档（头无效 / 槽损坏 / 空卡）——具体原因逐行打印
  2  文件不是 8 KB 的 EEPROM 存档（游戏按 64 kbit EEPROM 寻址，读不到）
"""
import argparse
import glob
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SIZE = 8192
MAGIC = 0x0DECADE0
HDR_OFF, HDR_LEN = 0x00, 24
SLOT0_OFF, SLOT_LEN = 0x20, 2720
SLOT_DATA = SLOT_LEN - 4              # 2716
N_WORDS = SLOT_DATA // 4              # 679
N_SLOTS = 3
M = 0xFFFFFFFF
# 其它可能的存档设备长度（用来把「读不到」的原因说清楚）
OTHER_SIZES = {512: '4 kbit EEPROM', 65536: 'Flash 512 kbit', 131072: 'Flash 1 Mbit'}


def unblock(b):
    """EEPROM 文件字节 → 内存镜像（每个 8 字节块倒序）。这个变换自反。"""
    out = bytearray()
    for i in range(0, len(b), 8):
        out += b[i:i + 8][::-1]
    return bytes(out)


block = unblock


def header(sav):
    mem = unblock(sav[HDR_OFF:HDR_OFF + HDR_LEN])
    w = list(struct.unpack('<6I', mem))
    return {'mem': mem, 'w': w, 'magic': w[1], 'chk': w[0],
            'calc': sum(w[1:]) & M, 'magic_ok': w[1] == MAGIC,
            'sum_ok': w[0] == (sum(w[1:]) & M)}


def slot(sav, i):
    off = SLOT0_OFF + i * SLOT_LEN
    raw = sav[off:off + SLOT_LEN]
    if len(raw) < SLOT_LEN:
        return None
    mem = unblock(raw)
    chk = struct.unpack_from('<I', mem, 0)[0]
    calc = sum(struct.unpack_from(f'<{N_WORDS}I', mem, 4)) & M
    return {'i': i, 'off': off, 'mem': mem, 'chk': chk, 'calc': calc,
            'ok': chk == calc, 'empty': all(b == 0xFF for b in raw),
            'nonzero': sum(1 for b in mem[4:] if b)}


def verdict(sav):
    """→ (退出码, 头, [槽...], 结论文字)。退出码语义见模块 docstring。

    判定顺序照游戏来：
      1) 头魔数不对 → 游戏重建头，并把「有存档」标志（base[8] 的 bit0/1/2）清掉，
         菜单里就看不到存档了（槽数据其实还在）；
      2) 头魔数对、校验和不对 → 游戏只是把校验和重签一遍，标志和槽都不受影响；
      3) 菜单按标志位列出存档；读某个槽时再校验该槽 → 不过就显示「已损坏」。
    """
    if len(sav) != SIZE:
        return 2, None, [], (f'长度 {len(sav)} 字节，不是本作的 8 KB（64 kbit EEPROM）存档'
                             + (f'；{len(sav)} 字节像是 {OTHER_SIZES[len(sav)]}'
                                if len(sav) in OTHER_SIZES else ''))
    h = header(sav)
    slots = [slot(sav, i) for i in range(N_SLOTS)]
    valid = [s['i'] for s in slots if s and s['ok'] and not s['empty']]
    bad = [s['i'] for s in slots if s and not s['ok'] and not s['empty']]
    flags = h['mem'][8]
    listed = [i for i in valid if flags >> i & 1]
    if not h['magic_ok']:
        if not valid and not bad:
            return 1, h, slots, '空卡 / 未初始化：游戏会重建头当新卡用，没有存档可读'
        return 1, h, slots, ('游戏读不到存档：头魔数不对 → 游戏会重建头并把「有存档」标志清掉，'
                             '菜单里就不再列出存档（槽数据仍在，--repair 可救回）')
    if listed:
        msg = f'游戏能读：槽 {"、".join(map(str, listed))}'
        if bad:
            msg += f'（槽 {"、".join(map(str, bad))} 会被判损坏）'
        return 0, h, slots, msg
    if bad:
        return 1, h, slots, (f'游戏会判损坏：槽 {"、".join(map(str, bad))} 校验和不对，'
                             f'读档会显示「このファイルは壊れています」')
    if valid:
        return 1, h, slots, ('槽数据本身是好的，但头里的「有存档」标志是 0，'
                             '菜单不会列出来（--repair 可修）')
    return 1, h, slots, '游戏读不到存档：没有任何槽有数据（空卡）'


def show(path, brief=False):
    sav = open(path, 'rb').read()
    code, h, slots, text = verdict(sav)
    name = os.path.basename(path)
    if brief:
        print(f'{name}: {text}')
        return code
    print(f'== {name}')
    if code == 2:
        print(f'   [文件] {text}')
        print(f'   ⇒ {text}')
        return code
    # 头
    mark = '✓' if h['magic_ok'] else '✗'
    print(f'   [头]  魔数 0x{h["magic"]:08X} {mark}'
          f'   校验和 0x{h["chk"]:08X} / 算出 0x{h["calc"]:08X} '
          f'{"✓" if h["sum_ok"] else "✗（差 0x%08X）" % ((h["calc"] - h["chk"]) & M)}')
    if not h['magic_ok']:
        print('          → 游戏不认这是有效存档头：会写回魔数并把「有存档」标志清掉')
        print('            （0x08015C14 返回 0 → 0x08015BB4 重建头），菜单里看不到存档')
    if h['magic_ok'] and not h['sum_ok']:
        print('          → 游戏会重建头（槽数据不受影响）；想保住菜单摘要用 --repair')
    flags = h['mem'][8]
    for s in slots:
        if s is None:
            continue
        f = '有' if flags >> s['i'] & 1 else '无'
        hour = struct.unpack_from('<H', h['mem'], 0x12 + 2 * s['i'])[0]
        if s['empty']:
            print(f'   [槽{s["i"]}] 空（擦除态 0xFF）            头标志={f}')
            continue
        print(f'   [槽{s["i"]}] 校验和 0x{s["chk"]:08X} / 算出 0x{s["calc"]:08X} '
              f'{"✓ 有效" if s["ok"] else "✗ 损坏（差 0x%08X）" % ((s["calc"] - s["chk"]) & M)}'
              f'   头标志={f}   小时={hour}   非零负载 {s["nonzero"]} B')
        if not s['ok']:
            print('          → 读这个槽会进状态 7，显示「このファイルは壊れています」')
        elif not (flags >> s['i'] & 1):
            print('          → 数据是好的，但头里的「有存档」标志是 0，菜单可能不列它；'
                  '--repair 可修')
    print(f'   ⇒ {text}')
    return code


def fix_checksums(sav, allow_magic=False, fix_flags=False):
    """重算校验和（可选补魔数 / 修「有存档」标志位），返回改动说明行。

    注意：这是**按现有字节重签校验和**——「相信数据」。如果负载本身被改过，
    游戏会照样把这份数据读进去（进度可能是坏的），这里只负责让它「读得动」。
    """
    msgs = []
    if len(sav) != SIZE:
        return ['长度不对，不改']
    for i in range(N_SLOTS):
        s = slot(sav, i)
        if s is None or s['empty'] or s['ok']:
            continue
        mem = bytearray(s['mem'])
        mem[0:4] = struct.pack('<I', s['calc'])
        sav[s['off']:s['off'] + SLOT_LEN] = block(bytes(mem))
        msgs.append(f'槽{i} 校验和 0x{s["chk"]:08X} → 0x{s["calc"]:08X}'
                    f'（按现有 {s["nonzero"]} 个非零字节重签）')
    h = header(sav)
    mem = bytearray(h['mem'])
    if allow_magic and not h['magic_ok']:
        mem[4:8] = struct.pack('<I', MAGIC)
        msgs.append(f'补魔数 0x{h["magic"]:08X} → 0x{MAGIC:08X}')
    if fix_flags:
        flags = mem[8]
        want = 0
        for i in range(N_SLOTS):
            s = slot(sav, i)
            if s and not s['empty'] and s['ok']:
                want |= 1 << i
        if (flags & 7) != want:
            msgs.append(f'「有存档」标志位 {flags & 7:03b} → {want:03b}')
            mem[8] = (flags & ~7) | want
    old_chk = struct.unpack_from('<I', mem, 0)[0]
    new_chk = sum(struct.unpack_from('<5I', mem, 4)) & M
    mem[0:4] = struct.pack('<I', new_chk)
    if old_chk != new_chk:
        msgs.append(f'头校验和 0x{old_chk:08X} → 0x{new_chk:08X}')
    sav[HDR_OFF:HDR_OFF + HDR_LEN] = block(bytes(mem))
    return msgs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('savs', nargs='*', help='.sav 文件（可多个）')
    ap.add_argument('--brief', action='store_true', help='每个文件只打一行结论')
    ap.add_argument('--selftest', action='store_true', help='查仓库里现有的 .sav')
    ap.add_argument('--resign', action='store_true', help='只重算校验和并写回')
    ap.add_argument('--repair', action='store_true',
                    help='重算校验和 + 补魔数 + 修「有存档」标志位，再写回')
    a = ap.parse_args()
    paths = list(a.savs)
    if a.selftest:
        paths += sorted(glob.glob(os.path.join(ROOT, '*.sav')))
    if not paths:
        ap.error('给出 .sav 文件，或用 --selftest')

    worst = 0
    for p in paths:
        sav = bytearray(open(p, 'rb').read())
        if a.resign or a.repair:
            msgs = fix_checksums(sav, allow_magic=a.repair, fix_flags=a.repair)
            if msgs:
                open(p, 'wb').write(bytes(sav))
                print(f'== {os.path.basename(p)}  已写回：')
                for m in msgs:
                    print(f'   · {m}')
            else:
                print(f'== {os.path.basename(p)}  无需改动')
        code = show(p, a.brief)
        worst = max(worst, code)
        if not a.brief:
            print()
    if not a.brief:
        print({0: '结论：游戏能读', 1: '结论：游戏读不到（会被判损坏或没有存档）',
               2: '结论：文件不对，游戏读不到'}[worst])
    return worst


if __name__ == '__main__':
    sys.exit(main())
