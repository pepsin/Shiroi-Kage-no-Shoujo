#!/usr/bin/env python3
"""Build the Simplified-Chinese ROM.

Two things have to happen:

1. **Font table**  the translation needs 2104 glyphs (931 reuse the JP table,
   1171 are new) but the JP table only has 1704 slots and only 771 of the
   unused ones can be recycled -> the table is rewritten with 400 extra
   glyphs.  A bigger table does not fit in the 198 KB tail (it needs 269 KB),
   so it is placed at ROM file 0x800000 and the file is grown past 8 MB;
   FAT entry 850 is repointed at it.  This is the same layout the reference
   Chinese release used (see tools/build_jp_map.py) and needs no code patch:
   the table is addressed through the FAT, and glyphs are read straight out
   of cart ROM.

2. **Script**  import_script.py rewrites every translated string in place with
   the extended character map.

任何一步出错都会**立刻中止**（后面一步都不跑）：开头是 1 秒的槽位预检
（译文装不进原槽位就别打包），中途是 import_script / 各个 patch 工具的退出码，
最后是六项自检；自检不过则以非 0 退出并列出 ✗ 清单。

Usage:
  build_rom.py [--rom JP.gba] [--master data/translation.tsv] [--out out.gba]
               [--headroom 128] [--no-script] [--no-preflight]
"""
import argparse
import re
import subprocess
import time
import collections
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import font_patch
import gbtext as g
import mapio

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
OLD_COUNT = 0x6A8
GLYPH_BYTES = 0x80
NEW_TABLE_OFF = 0x800000          # file offset of the rewritten table
ADV_TABLE_OFF = 0x6A4840          # FAT 851: per-glyph advance bytes (JP: 1704 x 0x0C)
ADV_VALUE = 0x0C                  # the JP value for every glyph
WORK = os.path.join(ROOT, 'work')
EXT_MAP = os.path.join(WORK, 'glyph_map.ext.csv')
# a rebuild normally has no hand-edited glyphs; more than this many differing
# PNGs means the directory is stale and would revert the render
GLYPH_EDIT_LIMIT = 200


def load_master(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def block_span(rom, head=852, max_gap=0x40):
    """The contiguous FAT run that starts at `head` (entries laid out back to back).

    Returns (start, end, eids) or None.  The game reads across these entry
    boundaries, so they must stay adjacent after relocation.
    """
    ents = []
    for eid in range(1500):
        o, sz = struct.unpack_from('<2I', rom, FAT + eid * 8)
        if sz:
            ents.append((BASE + o, BASE + o + sz, eid))
    ents.sort()
    idx = next((k for k, (a, b, e) in enumerate(ents) if e == head), None)
    if idx is None:
        return None
    block = [ents[idx]]
    k = idx + 1
    while k < len(ents):
        a, b, e = ents[k]
        if a - block[-1][1] < max_gap:
            block.append(ents[k])
            k += 1
        else:
            break
    return block[0][0], block[-1][1], [e for _, _, e in block]


def plan(rows, gmap, recycle=False, manifest=None, headroom=0):
    """Return (assignment, new_chars, table_count, free_used, extra).

    table_count includes `headroom` spare slots at the end.

    Slot assignment for new characters must be STABLE across builds: the
    hand-edit directory (data/glyph_png) and any in-RAM text saved in player
    savestates refer to glyph indices, so re-sorting the tail whenever the
    character set changes corrupts both.  When `manifest` (the previous
    build's data/glyph_png/manifest.tsv) is available, characters keep their
    previous slots and genuinely new characters fill slots that were free
    (未使用 / headroom) before the table grows.
    """
    rev = {}
    for idx, ch in sorted(gmap.items(), reverse=True):
        code = idx if idx < 0x20 else idx - 1
        if code >= 0 and ch:
            rev.setdefault(ch, code)
    used = collections.Counter()
    for r in rows:
        # '\xFFF2'-style escapes are raw control codes, not glyphs
        for ch in re.sub(r'\\x[0-9A-Fa-f]{4}', '', (r.get('translation') or '')).strip():
            used[ch] += 1
    have = set(gmap.values()) - {''}
    used_idx = set()
    for ch in used:
        code = rev.get(ch)
        if code is not None:
            used_idx.add(code if code < 0x20 else code + 1)
    free = [i for i in range(0x20, OLD_COUNT) if i not in used_idx]
    new_chars = sorted((c for c in used if c not in have), key=lambda c: (-used[c], c))
    if recycle:
        # reuse slots the *script* no longer references -- smaller table, but
        # glyphs used by other resources (title art, menus) would be destroyed
        got = new_chars[:len(free)]
        extra = new_chars[len(free):]
        assign = {ch: free[i] for i, ch in enumerate(got)}
        table_count = OLD_COUNT + len(extra) + headroom
        return assign, new_chars, table_count, len(got), extra, used, rev, used_idx
    prev_assign = {}     # char -> previous slot (new-glyph region only)
    prev_count = 0
    if manifest and os.path.exists(manifest):
        for r in csv.DictReader(open(manifest, encoding='utf-8'), delimiter='\t'):
            idx = int(r['index'], 16)
            prev_count = max(prev_count, idx + 1)
            ch = (r.get('char') or '').strip()
            if ch and idx >= OLD_COUNT:
                prev_assign.setdefault(ch, idx)
    if not prev_assign:
        extra = new_chars
        assign = {}
        for i, ch in enumerate(extra):
            assign[ch] = OLD_COUNT + i
        table_count = OLD_COUNT + len(extra) + headroom
        return assign, new_chars, table_count, 0, extra, used, rev, used_idx
    # stable path: chars keep their old slots; new chars take slots that were
    # unassigned in the previous build (headroom), then append
    assign = {ch: prev_assign[ch] for ch in new_chars if ch in prev_assign}
    # note: slots of dropped characters are NOT reused -- their manifest rows
    # still carry the old bitmap, and reusing them would let the stale PNG
    # overwrite the fresh render
    free_slots = [i for i in range(OLD_COUNT, prev_count)
                  if i not in set(prev_assign.values())]
    really_new = [ch for ch in new_chars if ch not in prev_assign]
    got = really_new[:len(free_slots)]
    for ch, slot in zip(got, free_slots):
        assign[ch] = slot
    extra = really_new[len(free_slots):]
    top = max(prev_count, OLD_COUNT)
    for i, ch in enumerate(extra):
        assign[ch] = top + i
    # prev_count already includes the previous build's headroom; only grow
    # (and re-add headroom) when the overflow spilled past the old table end
    table_count = max(prev_count, top + len(extra) + (headroom if extra else 0))
    return assign, new_chars, table_count, len(got), extra, used, rev, used_idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--headroom', type=int, default=128,
                    help='extra empty glyph slots appended for safety')
    ap.add_argument('--recycle', action='store_true',
                    help='reuse glyph slots the script no longer references '
                         '(smaller table, but overwrites glyphs other '
                         'resources may still use -- off by default)')
    ap.add_argument('--no-script', action='store_true',
                    help='only patch the font table, do not import the script')
    ap.add_argument('--glyph-dir', default=os.path.join(ROOT, 'data', 'glyph_png'),
                    help='hand-edited glyph PNGs to apply on top of the render')
    ap.add_argument('--no-glyph-edits', action='store_true',
                    help='ignore data/glyph_png hand edits')
    ap.add_argument('--outline-font', action='store_true',
                    help='render new glyphs by downscaling a system outline font '
                         'instead of the bundled pixel font (tools/fonts/zpix)')
    ap.add_argument('--force-glyph-edits', action='store_true',
                    help='apply data/glyph_png even if it looks stale')
    ap.add_argument('--no-preflight', action='store_true',
                    help='跳过打包前的槽位预检（make 里已经跑过 make slots 时才用）')
    a = ap.parse_args()

    # --- 预检：译文装不装得进原槽位（出错就别往下走）-------------------------
    # 回写是「写回原槽位」：译文比原文长的行会被 import_script 跳过、只留日文原文，
    # 那样一路跑到最后自检才报错，白白多跑两分钟，还把菜单/标题/字库步骤都跑了一遍。
    # check_slots 只读 TSV，一秒内把这类问题按行点名，拦在门口。
    if not a.no_script and not a.no_preflight:
        rc = run_check([sys.executable, os.path.join(ROOT, 'tools', 'check_slots.py'),
                        '--master', a.master])
        if rc != 0:
            print()
            print('!! 打包中止：槽位预检没通过（上面列出的行就是原因）。')
            print('   后面的步骤（扩表 / 回写剧本 / 菜单 / 标题 / 字库 / 自检）一步都没跑。')
            raise SystemExit(1)

    font_patch.set_pixel_font(not a.outline_font)
    print(f'new glyphs: {"pixel font " + os.path.basename(font_patch.PIXEL_FONT) if font_patch.use_pixel_font() else "outline font downscale"}',
          flush=True)
    rom = bytearray(open(a.rom, 'rb').read())
    gmap = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    rows = load_master(a.master)
    assign, new_chars, count, recycled, extra, used, rev, used_idx = plan(
        rows, gmap, a.recycle, headroom=a.headroom,
        manifest=None if a.recycle else os.path.join(a.glyph_dir, 'manifest.tsv'))
    print(f'distinct chars in translation : {len(used)}')
    print(f'  reuse JP glyph slots        : {len(used) - len(new_chars)}')
    print(f'  new glyphs                  : {len(new_chars)}')
    print(f'    recycled free slots       : {recycled}'
          f'{" (disabled: append-only)" if not a.recycle else ""}')
    print(f'    appended past 0x{OLD_COUNT:03X}          : {len(extra)}')
    print(f'new table: {count} glyphs = {count * GLYPH_BYTES} bytes '
          f'(+{(count - OLD_COUNT) * GLYPH_BYTES} vs JP)')

    # --- build the table -------------------------------------------------
    old = rom[font_patch.GLYPH_FILE_OFF:
              font_patch.GLYPH_FILE_OFF + OLD_COUNT * GLYPH_BYTES]
    table = bytearray(old) + bytearray((count - OLD_COUNT) * GLYPH_BYTES)
    for ch, idx in sorted(assign.items(), key=lambda kv: kv[1]):
        table[idx * GLYPH_BYTES:(idx + 1) * GLYPH_BYTES] = font_patch.render_glyph(ch)
    print(f'rendered {len(assign)} glyphs; table holds {len(table) // GLYPH_BYTES} slots')

    t_phase = time.time()
    # --- new map ---------------------------------------------------------
    newmap = dict(gmap)
    for ch, idx in assign.items():
        newmap[idx] = ch
    # work/ 是可再生的中间目录，可能被 make clean 整个删掉——自己建，别假设它存在
    os.makedirs(os.path.dirname(EXT_MAP), exist_ok=True)
    with open(EXT_MAP, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['code', 'dec', 'char'])
        for idx in range(count):
            w.writerow([f'{idx:03X}', idx, newmap.get(idx, '')])
    print(f'wrote {EXT_MAP} ({count} entries)')

    print(f'[phase] rendered {len(new_chars)} glyphs in {time.time() - t_phase:.1f}s')
    t_phase = time.time()
    # --- patch the ROM ---------------------------------------------------
    end = NEW_TABLE_OFF + len(table)
    if len(rom) < end:
        rom.extend(b'\x00' * (end - len(rom)))
    rom[NEW_TABLE_OFF:end] = table
    off = NEW_TABLE_OFF - BASE
    struct.pack_into('<II', rom, FAT + FONT_EID * 8, off, len(table))
    print(f'font table -> file 0x{NEW_TABLE_OFF:X} (FAT {FONT_EID}: '
          f'off=0x{off:X} size={len(table)})')

    # --- extend the per-glyph advance table ------------------------------
    # The text renderer computes a character's x as
    #     base + field8 + (i+1)*field10 + sum(1 + ADV[glyph])
    # where ADV is a byte table that in the JP ROM sits at file 0x6A4840
    # (FAT 851, 1704 bytes, all 0x0C) immediately after the 1704 glyph
    # bitmaps.  Our appended glyphs (index >= 1704) read past its end into
    # the next resource, which produced overlapping characters and huge gaps
    # in game.  FAT 852's data starts at 0x6A4EF0, inside the range the
    # extended table needs, so that data has to move out of the way.
    #
    # FAT 852 is the head of one long *contiguous* run of entries
    # (e852..e1006, 0x6A4EF0..0x7CE6C0).  The game reads across those entry
    # boundaries - moving only e852 made it run off into whatever we appended
    # next and the game died with a white screen (EWRAM wiped, DISPCNT=0x80).
    # So the whole run moves as one block, keeping every relative offset, and
    # a zero guard follows it because the JP ROM has free (zero) space after
    # the run.
    GUARD = 0x10000
    tbl_end = ADV_TABLE_OFF + count
    block = block_span(rom)
    if block and block[0] < tbl_end:
        bstart, bend, eids = block
        blob = bytes(rom[bstart:bend])
        new_off = len(rom)
        rom.extend(blob)
        rom.extend(b'\x00' * GUARD)
        for eid in eids:
            o, s = struct.unpack_from('<2I', rom, FAT + eid * 8)
            struct.pack_into('<II', rom, FAT + eid * 8, new_off + (BASE + o - bstart) - BASE, s)
        print(f'relocated the contiguous block e{min(eids)}..e{max(eids)} '
              f'({len(eids)} entries, {bend - bstart} bytes): file '
              f'0x{bstart:X} -> 0x{new_off:X} (+{GUARD:#x} zero guard)')
        # NOTE: do **not** try to "fix up" absolute addresses inside the block.
        # An earlier attempt added +delta to every u32 that happened to fall in
        # the old address window; that is a false-positive trap, because this
        # block is mostly 4bpp artwork whose bytes constantly form values in
        # 0x00800000..0x0092A6C0.  That heuristic rewrote 876 *data* words
        # (every one of them had its top byte flipped 0x08 -> 0x00 / 0x09 ->
        # 0x0A, the signature of value+delta), corrupting the notebook
        # background and the dialogue layer.  Verified: the relocated block in
        # the last known-good build (c893115) is byte-identical to the JP ROM,
        # i.e. the block contains no self-references that need patching - only
        # the FAT entry moves, and the game resolves the block through it.
    n_adv = 0
    for i in range(count):
        pos = ADV_TABLE_OFF + i
        if pos >= len(rom):
            rom.extend(b'\x00' * (pos + 1 - len(rom)))
        if rom[pos] != ADV_VALUE:
            rom[pos] = ADV_VALUE
            n_adv += 1
    print(f'advance table: 0x{ADV_TABLE_OFF:X}..0x{tbl_end:X} '
          f'({count} glyphs, {n_adv} bytes set to 0x{ADV_VALUE:02X}; FAT 851 size kept)')
    # sanity: the old tail must be free, and the new blob must not collide
    print(f'[phase] font/table/block patched in {time.time() - t_phase:.1f}s; '
          f'ROM size now {len(rom)} bytes ({len(rom) / 1048576:.2f} MB)')

    patched = os.path.join(WORK, 'font_patched.gba')
    open(patched, 'wb').write(bytes(rom))
    print(f'wrote {patched}')

    if a.no_script:
        return
    # 每一步都用 run_step：**失败即中止**，不让后面的菜单/标题/字库/自检接着跑
    # （以前是 subprocess.run(check=True)，出错只有一坨调用栈，而且流程已经走完了）。
    # --- import the script ----------------------------------------------
    run_step('回写剧本（译文 → ROM）',
             [sys.executable, os.path.join(ROOT, 'tools', 'import_script.py'),
              '--master', a.master, '--rom', patched, '--out', a.out,
              '--map', EXT_MAP], a.out)

    # --- redraw the pre-rendered menu plates (title screen) ---------------
    run_step('菜单牌子（标题画面）',
             [sys.executable, os.path.join(ROOT, 'tools', 'patch_menu_plates.py'),
              '--apply', a.out, a.out], a.out)

    # --- redraw the pre-rendered save/load screen plates ------------------
    run_step('存/读档牌子',
             [sys.executable, os.path.join(ROOT, 'tools', 'patch_save_plates.py'),
              '--apply', a.out, a.out], a.out)

    # --- status bar label: パートナー -> 助手 -------------------------------
    # The top bar's labels are pre-rendered 8x8 tile plates, not font text; the
    # partner label lives at the tail of the uncompressed entry e833.
    run_step('状态栏文字',
             [sys.executable, os.path.join(ROOT, 'tools', 'patch_status_bar.py'),
              a.out, '--apply'], a.out)

    # --- title screen cover artwork ---------------------------------------
    # The cover is a pre-rendered page of the same kind as the disclaimer screen.
    # docs/title/标题画面_BG3原画.png is its master: the hand-retouched artwork,
    # so a rebuild must import it rather than fall back to the Japanese original.
    run_step('标题画面原画',
             [sys.executable, os.path.join(ROOT, 'tools', 'patch_title_page.py'),
              a.out, '--apply'], a.out)

    # --- translate the opening disclaimer page ----------------------------
    # That screen is not font text, so it never reaches translation.tsv: it is a
    # pre-rendered LZ77 blob reached through e840's resource table.  The tool
    # rewrites the blob (Chinese lines + a credit footer) and keeps the table
    # entry in sync, writing in place whenever the new stream is small enough.
    run_step('免责页',
             [sys.executable, os.path.join(ROOT, 'tools', 'patch_disclaimer_page.py'),
              a.out, '--apply'], a.out)

    # --- apply hand-edited glyph PNGs -------------------------------------
    # data/glyph_png is the editable view of the font table; without this step
    # a rebuild would silently revert every glyph the user redrew by hand.
    if not a.no_glyph_edits and os.path.exists(os.path.join(a.glyph_dir, 'manifest.tsv')):
        print('\n== 手工改过的字形 PNG', flush=True)
        imp = [sys.executable, os.path.join(ROOT, 'tools', 'import_glyphs.py'),
               '--dir', a.glyph_dir, '--skip-unused']
        # A freshly rendered font should match the exported PNGs exactly, so
        # "hand edits" should be just the handful the user touched.  A large
        # count means the PNG directory was exported from some other ROM and
        # would revert the render - ask before doing that.
        chk = subprocess.run(imp + ['--rom', a.out, '--dry-run'],
                             capture_output=True, text=True)
        if chk.returncode != 0:
            # 以前这里不看退出码：dry-run 挂了会当成「0 处手改」继续往下跑。
            print(chk.stdout, chk.stderr, flush=True)
            raise SystemExit(f'import_glyphs.py --dry-run 失败（退出码 {chk.returncode}），'
                             f'已中止；{a.out} 停在描画字形之前。')
        m = re.search(r'hand-edited images: (\d+)', chk.stdout)
        n_hand = int(m.group(1)) if m else 0
        if n_hand > GLYPH_EDIT_LIMIT and not a.force_glyph_edits:
            print(chk.stdout, flush=True)
            raise SystemExit(
                f'{n_hand} glyph PNGs in {a.glyph_dir} differ from the font this build '
                f'rendered (expected only a few hand edits).\n'
                f'The directory was probably exported from a different ROM; re-export it '
                f'from a current build, or pass --force-glyph-edits to use it as-is.')
        run_step('写回手工字形',
                 imp + ['--rom', a.out, '--out', a.out], a.out)

        # --- re-export the editable view ---------------------------------
        # A newly needed character can land in a slot the previous export
        # labelled `未使用`; that stale blank PNG is deliberately NOT imported
        # (--skip-unused), so the directory no longer mirrors the ROM and the
        # glyph self-check would flag it.  Re-export from the ROM we just
        # wrote: every hand edit has been imported by now, so --force only
        # discards the stale blanks.
        run_step('重新导出字形底稿',
                 [sys.executable, os.path.join(ROOT, 'tools', 'export_glyphs.py'),
                  '--rom', a.out, '--out', a.glyph_dir, '--map', EXT_MAP, '--force'], a.out)


    # --- self-checks ------------------------------------------------------
    # 每项都打标签：失败时末尾给一张清单，而不是只说 "see the output above"
    # （上面几百行格式化输出，真正的问题经常已经滚出屏幕）。
    t_check = time.time()
    print('\n=== self-check ===', flush=True)
    t = os.path.join(ROOT, 'tools')
    py = sys.executable
    checks = [
        ('verify_rom.py（ROM 回读 vs 主表译文）', [py, os.path.join(t, 'verify_rom.py'), a.out]),
        ('verify_writes.py（写入范围）', [py, os.path.join(t, 'verify_writes.py'), '--cn', a.out]),
        ('verify_glyphs.py（字形 / PNG 同步）',
         [py, os.path.join(t, 'verify_glyphs.py'), a.out, '--dir', a.glyph_dir]),
        ('patch_disclaimer_page.py（免责页）',
         [py, os.path.join(t, 'patch_disclaimer_page.py'), a.out, '--check']),
        ('patch_status_bar.py（状态栏文字）',
         [py, os.path.join(t, 'patch_status_bar.py'), a.out, '--check']),
        ('patch_title_page.py（标题画面）',
         [py, os.path.join(t, 'patch_title_page.py'), a.out, '--check']),
    ]
    results = [(label, run_check(cmd)) for label, cmd in checks]
    ok = all(rc == 0 for _, rc in results)
    print('\n=== 自检结果 ===')
    for label, rc in results:
        print(f'  {"✓" if rc == 0 else "✗"} {label}')
    print(f'=== self-check {"PASSED" if ok else "FAILED"} '
          f'in {time.time() - t_check:.1f}s ===', flush=True)
    if not ok:
        failed = '、'.join(label.split('（')[0] for label, rc in results if rc)
        print()
        print(f'!! 打包失败：{failed} 没通过（上面带 ✗ / PROBLEM 的那几行就是原因）。')
        print(f'   {a.out} 已经写出，但内容不完整，别拿去测试。')
        print('   译文超长/缺字快速点名：python3 tools/check_slots.py')
        print('   改好译文后重新 make；只想复查现有 ROM：make verify')
        raise SystemExit(1)


def run_check(cmd):
    """跑一个自检工具，输出原样透传（失败的行要留在屏幕上），返回退出码。"""
    print(f'\n$ {" ".join(os.path.basename(c) for c in cmd[:2])}', flush=True)
    return subprocess.run(cmd).returncode


def run_step(label, cmd, out):
    """跑打包的一步；**失败就立刻中止整个流程**。

    以前每步都是 subprocess.run(check=True)：出错只丢一坨 CalledProcessError 调用栈，
    而菜单 / 标题 / 字库 / 自检这些后面的步骤还会继续往下跑。现在一旦某步返回非 0，
    立刻停在这里，并说清「后面一步都没跑」。
    """
    print(f'\n== {label}')
    print('   running:', ' '.join(cmd[1:]), flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print()
        print(f'!! 打包中止：{label} 失败（退出码 {rc}）。')
        print(f'   这一步之后的流程一步都没跑；{out} 不是完整版本，别拿去测试。')
        raise SystemExit(1)
    return rc


if __name__ == '__main__':
    main()
