#!/usr/bin/env python3
"""check_append_guard.py — 静态检查「追加条目」的两条硬约束（不需要模拟器）。

背景（docs/技术说明.md 第十节）
------------------------------
`import_script.py` 在重压后的条目比原槽更大时，会把新数据**追加到 ROM 尾部**并
改 FAT 指向。这带来两个只会在实机才炸的坑，两个都真实发生过：

1. **越界读 → 白屏 / EWRAM 被清零**。游戏解压某条目时会越过条目末尾一点点：
   JP 原版里紧随其后的是下一个条目的数据（无害），而追加条目的后面是**我们自己
   下一个条目的数据**，于是崩掉。修法是追加后补 `APPEND_GUARD = 32` 字节零。

2. **没对齐 → 解压失败 → 花屏**。BIOS 的 LZ77 SWI 要求源指针 **4 字节对齐**；
   追加到任意偏移会静默破坏这个契约（e417 落在 0xBEC5D6，mod 4 == 2），
   解压失败后笔记本/存档界面的名字行画出未初始化 VRAM，出现「花形乱码字」。

这两条都是**构建产物的静态性质**，本来就不需要跑模拟器、也不需要即时存档
（当年用 `work/crashtest.sh` + `assemble_subset.py` 二分，是因为还没定位到根因）。
现在直接在打包产物上验：

    python3 tools/check_append_guard.py                  # 用 work/font_patched.gba 当参照
    python3 tools/check_append_guard.py --ref <jp.gba>   # 拿不到 font_patched 时用日文原版

参照 ROM 的含义：**同一次构建、但没写过剧本**（`build_rom.py` 会把中间产物存成
`work/font_patched.gba`）。FAT 记录与参照不同的条目就是被改写的；其中**偏移也变了**
的就是「追加」。对追加条目要求：

  * `ROM 文件偏移 (BASE + off) % 4 == 0`
  * `[off+size, off+size+32)` 全是 0
  * 这段 guard 必须落在 ROM 内

退出码：0 = 全部通过；1 = 有违规；2 = 前置条件不满足（缺 ROM / 缺参照）。
"""

import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g   # noqa: E402

FAT = g.FAT
BASE = g.BASE
FAT_ENTRIES = 1024
GUARD = 32            # 与 tools/import_script.py 的 APPEND_GUARD 保持一致

DEFAULT_ROM = os.path.join(ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba')
DEFAULT_REF = os.path.join(ROOT, 'work', 'font_patched.gba')
JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')


def fat(rom, eid):
    return struct.unpack_from('<2I', rom, FAT + eid * 8)


def scan(rom, ref):
    """→ (violations, stats)。violations = [(eid, 类别, 说明)]"""
    bad = []
    for data, label in ((rom, 'ROM'), (ref, '参照 ROM')):
        if len(data) < FAT + FAT_ENTRIES * 8:
            raise ValueError(f'{label} 不含完整 FAT')
    rewritten = appended = 0
    for eid in range(FAT_ENTRIES):
        off, size = fat(rom, eid)
        roff, rsize = fat(ref, eid)
        if off == 0 and size == 0:
            if (roff, rsize) != (0, 0):
                bad.append((eid, 'range', '原有 FAT 条目被清空'))
            continue
        # The original FAT has zero-length placeholder entries (e841/e843).
        if (size == 0 and (off, size) != (roff, rsize)) or BASE + off + size > len(rom):
            bad.append((eid, 'range', 'FAT 条目为空或数据范围超出 ROM'))
            continue
        if (off, size) == (roff, rsize):
            continue
        rewritten += 1
        if off == roff:
            # 原地改写 / 填进原槽后的空隙：后面仍然是本条目或邻条目的原数据，
            # 与 JP 原版情形相同，不需要 guard（见 docs/技术说明.md 第十节）。
            continue
        appended += 1

        file_off = BASE + off
        if file_off % 4:
            bad.append((eid, 'align',
                        f'追加偏移 0x{file_off:X} 不是 4 的倍数'
                        f'（mod 4 = {file_off % 4}）→ BIOS LZ77 SWI 会解压失败（花屏）'))
        end = BASE + off + size
        if end + GUARD > len(rom):
            bad.append((eid, 'guard', f'数据末尾 0x{end:X} 之后不足 {GUARD} 字节就到 ROM 尾'))
            continue
        tail = rom[end:end + GUARD]
        if any(tail):
            nz = sum(1 for b in tail if b)
            first = next(i for i, b in enumerate(tail) if b)
            bad.append((eid, 'guard',
                        f'数据末尾 0x{end:X} 之后有 {nz}/{GUARD} 个非零字节'
                        f'（首个在 +{first}）→ 越界读会读到别的条目数据，可能白屏/清零 EWRAM'))
    return bad, {'rewritten': rewritten, 'appended': appended}


def disp(path):
    """仓库内的路径显示成相对路径，仓外的（比如 /tmp 的对照 ROM）显示绝对路径。"""
    ap = os.path.abspath(path)
    return os.path.relpath(ap, ROOT) if ap.startswith(ROOT + os.sep) else ap


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rom', default=DEFAULT_ROM)
    ap.add_argument('--ref', default='',
                    help=f'参照 ROM（无剧本写入的中间产物）；默认 {os.path.relpath(DEFAULT_REF, ROOT)}，'
                         f'缺了就用日文原版')
    ap.add_argument('--limit', type=int, default=40, help='最多列多少条违规')
    a = ap.parse_args()

    if not os.path.exists(a.rom):
        print(f'!! 找不到 ROM：{a.rom}\n   先跑 make rom', file=sys.stderr)
        return 2
    ref_path = a.ref or DEFAULT_REF
    if not os.path.exists(ref_path):
        if not a.ref and os.path.exists(JP_ROM):
            print(f'[warn] 没有 {os.path.relpath(DEFAULT_REF, ROOT)}，退回日文原版当参照；'
                  f'日文原版没有 CN 的字库/搬块改动，结果只作参考', file=sys.stderr)
            ref_path = JP_ROM
        else:
            print(f'!! 找不到参照 ROM：{ref_path}（先跑一次 make rom，或 --ref 指定）',
                  file=sys.stderr)
            return 2

    try:
        with open(a.rom, 'rb') as f:
            rom = f.read()
        with open(ref_path, 'rb') as f:
            ref = f.read()
        bad, st = scan(rom, ref)
    except (OSError, ValueError) as exc:
        print(f'!! {exc}', file=sys.stderr)
        return 2

    print(f'== 追加条目的对齐/零 guard 检查')
    print(f'   ROM      {disp(a.rom)}  ({len(rom)} 字节)')
    print(f'   参照     {disp(ref_path)}')
    print(f'   改写条目 {st["rewritten"]}  其中追加 {st["appended"]}  违规 {len(bad)}')
    for eid, kind, why in bad[:a.limit]:
        print(f'   e{eid:04d}  [{kind}]  {why}')
    if len(bad) > a.limit:
        print(f'   …还有 {len(bad) - a.limit} 条')
    if bad:
        print('== FAIL：追加条目必须 4 字节对齐，且后面补 32 字节零'
              '（tools/import_script.py 的 APPEND_GUARD）')
        return 1
    print('== OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
