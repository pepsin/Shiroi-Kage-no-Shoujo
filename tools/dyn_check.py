#!/usr/bin/env python3
"""dyn_check.py — 可复现的动态回归：无头 mGBA 跑固定路线，比对不变量与证据哈希。

为什么要有它
------------
本工程静态侧已经很严（`make verify` 六项自检、槽位预检、存档校验），
但**动态侧一个可复现的基线都没有**：`work/` 全被 gitignore，即时存档和截图
跑完就丢，历史验证都是「人工敲一遍 harness + 人看结论」。于是
`docs/存档卡死分析.md` 那类「只有走到某个分支才会暴露」的问题，只能靠人撞见。

这个脚本把动态验证变成可入库、可重复、可 diff 的东西：

  * 路线 = 从冷启动开始的**按键脚本 + 帧数**（不需要提交 `.ssN`，因为模拟器是
    确定性的：同一 ROM + 同一按键序列 → EWRAM/IWRAM/VRAM 逐字节一致，已实测）；
  * 观测 = 描述符（池基址/缓冲/keyB/keyCount）、游标、当前行、entry 号/场景号，
    外加 EWRAM/IWRAM/VRAM/截图 的 sha256；
  * 基线 = `tests/dyn/baseline.json`（入库）；
  * 判定 = **不变量必须成立**（`live` / `entry_is` / `ewram_nonzero_min` …）；
    哈希漂移只报 `CHANGED`，加 `--strict` 才当失败——因为改一句译文本来就会让
    证据哈希变，不该每次都红。

差分模式（`"rom": "both"`）：同一条按键路线在**日文原版**和**简中补丁**上各跑一遍，
要求两边落到同一个 entry / 同样的 keyCount / 同一行。这正是「静默分支损坏」
（分发表/跳转表被写坏、越界读改道）的最小探测器——不需要事先知道正确剧情，
只要两份 ROM 行为一致。

用法
----
  python3 tools/dyn_check.py list                 # 列出规格里的检查
  python3 tools/dyn_check.py run                  # 跑全部并比对基线（退出码 0/1）
  python3 tools/dyn_check.py run --only cn-boot   # 只跑一条
  python3 tools/dyn_check.py run --update         # 用当前结果刷新基线
  python3 tools/dyn_check.py run --strict         # 证据哈希漂移也算失败
  python3 tools/dyn_check.py run --json           # 机器可读

规格文件：tests/dyn/checks.json（入库）。按键写法：
  "A@120:4"           第 120 帧按下 A，按 4 帧
  "A@5000-5600:4:40"  5000..5600 每 40 帧按一次 A（连按的紧凑写法）
"""

import argparse
import copy
import hashlib
import json
import os
import re
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import state_inspect as si   # noqa: E402  （复用 engine()/glyph_text()/read_row()，只读）
import entry_id              # noqa: E402
from harness_utils import expand_keys, run_harness, HarnessError

HARNESS = os.path.join(ROOT, 'work', 'bin', 'gbarun_dbg')
SPEC = os.path.join(ROOT, 'tests', 'dyn', 'checks.json')
BASELINE = os.path.join(ROOT, 'tests', 'dyn', 'baseline.json')
OUTDIR = os.path.join(ROOT, 'work', 'dyn')

ROMS = {
    'cn': os.path.join(ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba'),
    'jp': os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba'),
}

DESC = 0x02003F24
DESC_LEN = 0x18            # pool, romptr, buf, buf2, keyB, keyCount
CURSOR = 0x03007C40
CURSOR_LEN = 8             # 游标 + 当前行偏移
MAX_SAMPLES = 20           # GBARUN_FRAME_READ 的缓冲只有 1024 字节，别超
GUARD_HINT = ('缺少 {p}，先编译 harness：\n  make dbg')


# ----------------------------------------------------------------- 小工具
def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 16), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def sample_frames(frames, every):
    if not every or every <= 0:
        return []
    regular = range(every, frames - 1, every)
    count = len(regular) + 1
    indices = (range(count) if count <= MAX_SAMPLES else
               [i * (count - 1) // (MAX_SAMPLES - 1) for i in range(MAX_SAMPLES)])
    return [regular[i] if i < len(regular) else frames - 1 for i in indices]


# --------------------------------------------------------------- 输出解析
_READ_RE = re.compile(r'\[read\] 0x([0-9A-Fa-f]{8}) len=(\d+):(.*?)(?=\n\[|\Z)', re.S)


def parse_freads(out):
    """harness 的 GBARUN_FRAME_READ 输出 → {frame: {addr: bytes}}。"""
    res = {}
    for chunk in out.split('[fread] f=')[1:]:
        m = re.match(r'\d+', chunk)
        if not m:
            continue
        frame = int(m.group(0))
        recs = res.setdefault(frame, {})
        for rm in _READ_RE.finditer(chunk):
            addr = int(rm.group(1), 16)
            ln = int(rm.group(2))
            toks = []
            for line in rm.group(3).split('\n'):
                line = line.strip()
                if re.match(r'^0x[0-9A-Fa-f]{8}\b', line):
                    line = line[10:].strip()
                toks += [t for t in line.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', t)]
            if len(toks) >= ln:
                recs[addr] = bytes(int(t, 16) for t in toks[:ln])
    return res


class Snap:
    """让 state_inspect.engine() 能直接吃 RAMSNAP 出来的裸区域。"""

    def __init__(self, wram, iwram, vram):
        self.reg = {'wram': wram, 'iwram': iwram, 'vram': vram}


# ------------------------------------------------------------------ 跑一次
def run_once(name, rom_tag, rom_path, chk, outdir):
    frames = int(chk['frames'])
    last = frames - 1
    prefix = os.path.join(outdir, f'{name}.{rom_tag}')
    samples = sample_frames(frames, chk.get('sample_every', 0))

    spec_str = ''
    if samples:
        frag = []
        for f in samples:
            frag.append(f'0x{DESC:08X}:{DESC_LEN}@{f}')
            frag.append(f'0x{CURSOR:08X}:{CURSOR_LEN}@{f}')
        spec_str = ';'.join(frag)
        # harness 里 GBARUN_FRAME_READ 的缓冲固定 1024 字节，超了会被静默截断
        while len(spec_str) > 900 and samples:
            samples = sorted(set(samples[::2] + [last]))
            frag = []
            for f in samples:
                frag.append(f'0x{DESC:08X}:{DESC_LEN}@{f}')
                frag.append(f'0x{CURSOR:08X}:{CURSOR_LEN}@{f}')
            spec_str = ';'.join(frag)
    try:
        evidence, out = run_harness(HARNESS, rom_path, frames, chk.get('keys', []),
                                    prefix, frame_read=spec_str,
                                    timeout=chk.get('timeout', 60))
    except (HarnessError, ValueError) as exc:
        return {'error': str(exc)}, {}, ''
    ew, iw, vram = (evidence[k] for k in ('ewram', 'iwram', 'vram'))
    obs = {'returncode': 0}

    obs['ewram_nonzero'] = sum(1 for b in ew if b)
    obs['iwram_nonzero'] = sum(1 for b in iw if b)

    e = si.engine(Snap(ew, iw, vram))
    obs.update(live=bool(e['live']), rows=e['rows'], key_b=e['key_b'],
               cursor=e['cur'], line=e['line'], idx=e['idx'])
    if e['live']:
        if rom_tag == 'cn':
            # JP 的字形映射与简中不同，解出来是另一套字，只会误导；不记
            codes = si.read_row(ew, e['pool'] - 0x02000000 + e['line'])
            obs['line_text'] = si.glyph_text(codes)
        m = entry_id.resolve_from_ram(_rom_index(rom_path), ew,
                                      key_count=e['rows'], key_b=e['key_b'])[0]
        # A guessed or tied entry must never satisfy an exact route invariant.
        obs['entry'] = m.eid if m.structural_complete and not m.ambiguous else None
        obs['entry_conf'] = f'{m.structural_hits}/{m.structural_total}'
        if obs['entry'] is not None:
            lk = _lookup()
            row = lk.at_offset(m.eid, e['line'])
            obs['scene'] = row['scene'] if row else lk.best_scene(m.eid, None)
            obs['rid'] = row['rid'] if row else None
            obs['row_jp'] = row['jp'] if row else None
            obs['row_cn'] = row['cn'] if row else None

    # ---- 采样时间序列（用来判断「有没有停住」）
    reads = parse_freads(out)
    missing = [f for f in samples if len(reads.get(f, {}).get(DESC, b'')) != DESC_LEN
               or len(reads.get(f, {}).get(CURSOR, b'')) != CURSOR_LEN]
    if missing:
        return {'error': f'采样数据缺失：帧 {missing}'}, {}, out
    series = []
    for f in sorted(reads):
        rec = reads[f]
        d = rec.get(DESC)
        c = rec.get(CURSOR)
        if not d or not c:
            continue
        pool, romptr, buf, buf2, key_b, rows = struct.unpack_from('<6I', d, 0)
        cur, line = struct.unpack_from('<2I', c, 0)
        live = 0x02020000 <= pool < 0x02040000 and line < 0x20000
        series.append({'f': f, 'pool': pool, 'rows': rows, 'key_b': key_b,
                       'cursor': cur, 'line': line, 'live': live})
    obs['series'] = series
    positions = [(s['pool'], s['rows'], s['key_b'], s['cursor'], s['line'])
                 for s in series if s['live']]
    obs['cursor_changes'] = sum(a != b for a, b in zip(positions, positions[1:]))

    hashes = {
        'ewram': hashlib.sha256(ew).hexdigest(),
        'iwram': hashlib.sha256(iw).hexdigest(),
        'vram': hashlib.sha256(vram).hexdigest(),
        'shot': hashlib.sha256(evidence['ppm']).hexdigest(),
        'samples': hashlib.sha256(json.dumps(series, sort_keys=True).encode()).hexdigest(),
    }
    return obs, hashes, out


_ROM_CACHE = {}
_INDEX_CACHE = {}
_LOOKUP_CACHE = [None]


def _rom_bytes(path):
    if path not in _ROM_CACHE:
        _ROM_CACHE[path] = open(path, 'rb').read()
    return _ROM_CACHE[path]


def _rom_index(path):
    if path not in _INDEX_CACHE:
        _INDEX_CACHE[path] = entry_id.RomIndex(_rom_bytes(path))
    return _INDEX_CACHE[path]


def _lookup():
    if _LOOKUP_CACHE[0] is None:
        import scene_lookup
        _LOOKUP_CACHE[0] = scene_lookup.Lookup()
    return _LOOKUP_CACHE[0]


# ------------------------------------------------------------------ 判定
def check_invariants(chk, runs):
    """→ [(是否通过, 说明)]。runs = {rom_tag: obs}。"""
    inv = chk.get('invariants', {})
    out = [(False, f'[{tag}] {obs["error"]}') for tag, obs in runs.items() if 'error' in obs]
    out += [(False, f'[{tag}] harness 退出码 {obs.get("returncode")}')
            for tag, obs in runs.items() if not obs.get('error') and obs.get('returncode') != 0]
    for key, want in inv.items():
        if key.startswith('same_'):
            if key not in ('same_entry', 'same_rows', 'same_rid') or want is not True:
                out.append((False, f'{key}: 未知的差分不变量或值不是 true'))
                continue
            if set(runs) != {'jp', 'cn'}:
                out.append((False, f'{key}: 差分不变量需要 rom=both'))
                continue
            a, b = runs['jp'], runs['cn']
            field = key[len('same_'):]
            va, vb = a.get(field), b.get(field)
            out.append((va is not None and vb is not None and va == vb,
                        f'{key}: {va} vs {vb}'))
            continue
        for tag, obs in runs.items():
            ok, why = _one_invariant(key, want, obs, tag)
            out.append((ok, f'[{tag}] {why}'))
    return out


def _one_invariant(key, want, obs, tag='cn'):
    # 文本不变量只在简中 ROM 上判：JP 的字形映射不同，解出来本来就是另一套字
    if key == 'line_contains' and tag != 'cn':
        return True, f'{key} 跳过（文本不变量只对 cn 判定）'
    if key == 'live':
        got = obs.get('live')
        return got == want, f'live: {got}（期望 {want}）'
    if key == 'entry_is':
        got = obs.get('entry')
        ok = (got in want) if isinstance(want, list) else (got == want)
        return ok, f'entry: e{got}（期望 {want}）'
    if key == 'rows_is':
        got = obs.get('rows')
        return got == want, f'keyCount: {got}（期望 {want}）'
    if key == 'row_is':
        got = obs.get('rid')
        return got == want, f'当前行: {got}（期望 {want}）'
    if key == 'line_contains':
        got = obs.get('line_text') or ''
        return want in got, f'当前行文本含「{want}」: {got!r}'
    if key == 'ewram_nonzero_min':
        got = obs.get('ewram_nonzero', 0)
        return got >= want, f'EWRAM 非零字节 {got}（要求 ≥{want}；0 = 白屏/清零）'
    if key == 'no_stall':
        need = want if type(want) is int else 2
        got = obs.get('cursor_changes', 0)
        return got >= need, f'游标变化次数 {got}（要求 ≥{need}，判定「按键没推进」）'
    return False, f'{key}: 未知的不变量'


# ------------------------------------------------------------------ 报告
def fmt_report(results, strict):
    lines = []
    failed = 0
    for r in results:
        name = r['name']
        if r.get('skipped'):
            lines.append(f'  {name:<28} SKIP  {r["skipped"]}')
            continue
        bad = [w for ok, w in r['invariants'] if not ok]
        drift = r['drift']
        if bad:
            status = 'FAIL'
            failed += 1
        elif drift and strict:
            status = 'FAIL'
            failed += 1
        elif drift:
            status = 'PASS*'
        else:
            status = 'PASS'
        detail = '  '.join(r['summary'])
        lines.append(f'  {name:<28} {status}  {detail}')
        for w in bad:
            lines.append(f'      ✗ {w}')
        if drift and not bad:
            for d in drift:
                lines.append(f'      ~ 证据漂移: {d}'
                             + ('（--strict 下算失败）' if strict else '（--update 可刷新）'))
    return lines, failed


def load_spec():
    if not os.path.exists(SPEC):
        raise ValueError(f'缺少规格文件 {SPEC}')
    with open(SPEC, encoding='utf-8') as f:
        spec = json.load(f)
    validate_spec(spec)
    return spec


def validate_spec(spec):
    if not isinstance(spec, dict) or spec.get('version') != 1:
        raise ValueError('检查规格 version 必须为 1')
    checks = spec.get('checks')
    if not isinstance(checks, list) or not checks:
        raise ValueError('checks 必须是非空列表')
    names = set()
    allowed = {'name', 'why', 'rom', 'frames', 'keys', 'sample_every', 'invariants', 'timeout'}
    for chk in checks:
        if not isinstance(chk, dict) or set(chk) - allowed:
            raise ValueError(f'检查含未知字段（仅支持冷启动路线）：{chk}')
        name = chk.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name):
            raise ValueError(f'无效检查名：{name!r}')
        if name in names:
            raise ValueError(f'重复检查名：{name}')
        names.add(name)
        if chk.get('rom', 'cn') not in ('jp', 'cn', 'both'):
            raise ValueError(f'{name}: rom 必须是 jp/cn/both')
        frames = chk.get('frames')
        if type(frames) is not int or not 0 < frames <= 10_000_000:
            raise ValueError(f'{name}: frames 必须在 1..10000000 内')
        every = chk.get('sample_every', 0)
        if type(every) is not int or every < 0:
            raise ValueError(f'{name}: sample_every 必须是非负整数')
        timeout = chk.get('timeout', 60)
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError(f'{name}: timeout 必须在 (0, 3600] 秒内')
        expand_keys(chk.get('keys', []), frames)
        inv = chk.get('invariants')
        if not isinstance(inv, dict) or not inv:
            raise ValueError(f'{name}: invariants 必须是非空对象')
        for key, value in inv.items():
            if key == 'live':
                valid = type(value) is bool
            elif key == 'entry_is':
                vals = value if isinstance(value, list) else [value]
                valid = bool(vals) and all(type(v) is int and 0 <= v < 1024 for v in vals)
            elif key in ('rows_is', 'ewram_nonzero_min'):
                valid = type(value) is int and value >= 0
            elif key == 'row_is':
                valid = isinstance(value, str) and re.fullmatch(r'\d+:\d+', value)
            elif key == 'line_contains':
                valid = isinstance(value, str) and bool(value) and chk.get('rom', 'cn') != 'jp'
            elif key == 'no_stall':
                valid = every > 0 and (value is True or (type(value) is int and value > 0))
            elif key in ('same_entry', 'same_rows', 'same_rid'):
                valid = chk.get('rom') == 'both' and value is True
            else:
                valid = False
            if not valid:
                raise ValueError(f'{name}: 无效不变量 {key}={value!r}')


def load_baseline():
    if not os.path.exists(BASELINE):
        return {}
    with open(BASELINE, encoding='utf-8') as f:
        base = json.load(f)
    if not isinstance(base, dict) or not isinstance(base.get('checks'), dict):
        raise ValueError('基线必须包含 checks 对象')
    for entry in base['checks'].values():
        if not isinstance(entry, dict) or not isinstance(entry.get('hashes'), dict):
            raise ValueError('基线检查必须包含 hashes 对象')
        if any(not isinstance(h, dict) for h in entry['hashes'].values()):
            raise ValueError('基线每个 ROM 的 hashes 必须是对象')
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    l = sub.add_parser('list', help='列出规格里的检查')
    l.set_defaults(func=cmd_list)

    r = sub.add_parser('run', help='跑检查并比对基线')
    r.add_argument('--only', default='', help='只跑名字匹配的（子串）')
    mode = r.add_mutually_exclusive_group()
    mode.add_argument('--update', action='store_true', help='全部选中检查成功后刷新对应基线')
    mode.add_argument('--strict', action='store_true', help='证据哈希漂移或基线缺失也算失败')
    r.add_argument('--json', action='store_true', help='输出 JSON')
    r.set_defaults(func=cmd_run)
    a = ap.parse_args()
    try:
        return a.func(a)
    except (ValueError, OSError) as exc:
        if getattr(a, 'json', False):
            print(json.dumps({'error': str(exc), 'checks': [], 'failed': 1}, ensure_ascii=False))
        else:
            print(f'!! {exc}', file=sys.stderr)
        return 2


def cmd_list(a):
    spec = load_spec()
    for chk in spec['checks']:
        roms = 'jp+cn' if chk.get('rom', 'cn') == 'both' else chk.get('rom', 'cn')
        print(f"{chk['name']:<28} rom={roms:<6} frames={chk['frames']:<6} "
              f"keys={' '.join(chk.get('keys', [])) or '-'}")
    return 0


def cmd_run(a):
    spec = load_spec()
    selected = [c for c in spec['checks'] if not a.only or a.only in c['name']]
    if not selected:
        raise ValueError(f'没有匹配的检查：{a.only}')
    if not os.path.isfile(HARNESS) or not os.access(HARNESS, os.X_OK):
        raise ValueError(GUARD_HINT.format(p=HARNESS))
    base = load_baseline()
    os.makedirs(OUTDIR, exist_ok=True)
    new_base = copy.deepcopy(base) if base else {'checks': {}}
    new_base['version'] = spec['version']
    new_base['rom_hash'] = {}
    for tag, p in ROMS.items():
        h = sha256_file(p)
        if h:
            new_base['rom_hash'][tag] = h

    results = []
    for chk in selected:
        name = chk['name']
        want_roms = chk.get('rom', 'cn')
        tags = ['jp', 'cn'] if want_roms == 'both' else [want_roms]
        runs, hashes = {}, {}
        for tag in tags:
            path = ROMS.get(tag, tag)
            if not os.path.exists(path):
                runs[tag] = {'error': f'ROM 不存在: {path}'}
                continue
            try:
                obs, h, _out = run_once(name, tag, path, chk, OUTDIR)
            except (OSError, ValueError, struct.error) as exc:
                obs, h = {'error': f'观测解析失败：{exc}'}, {}
            runs[tag] = obs
            hashes[tag] = h
            if 'error' in obs and not a.json:
                print(f'[warn] {name}.{tag}: {obs["error"]}', file=sys.stderr)

        inv = check_invariants(chk, runs)
        check_hash = hashlib.sha256(json.dumps(chk, sort_keys=True).encode()).hexdigest()
        entry = {'check_hash': check_hash,
                 'rom_hash': {tag: new_base['rom_hash'].get(tag) for tag in tags},
                 'hashes': hashes, 'obs': {t: {k: v for k, v in o.items()
                                               if k not in ('series', 'error')}
                                           for t, o in runs.items()}}
        new_base['checks'][name] = entry

        old = base.get('checks', {}).get(name, {})
        drift = []
        if old.get('check_hash') != check_hash:
            drift.append('检查规格变更或缺少规格指纹')
        for tag in hashes:
            for region, h in (hashes[tag] or {}).items():
                oh = (old.get('hashes', {}).get(tag) or {}).get(region)
                if oh != h or not oh:
                    drift.append(f'{tag}.{region}' + ('（缺少基线）' if not oh else ''))

        summary = []
        for tag, o in runs.items():
            if 'error' in o:
                summary.append(f'[{tag}] 无数据')
            elif o.get('entry') is not None:
                summary.append(f'[{tag}] e{o["entry"]} {o.get("scene")} {o.get("rid")}')
            else:
                summary.append(f'[{tag}] live={o.get("live")} rows={o.get("rows")}')
        results.append({'name': name, 'invariants': inv, 'drift': drift,
                        'summary': summary, 'obs': runs})

    valid = all(ok for r in results for ok, _ in r['invariants'])
    updated = False
    if a.update and valid:
        # Partial refreshes retain every unselected check and its own ROM hash.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(BASELINE), prefix='.baseline-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(new_base, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.write('\n')
            os.replace(tmp, BASELINE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        updated = True
        for result in results:
            result['drift'] = []

    lines, failed = fmt_report(results, a.strict)
    if a.json:
        print(json.dumps({'checks': [{'name': r['name'],
                                      'pass': all(ok for ok, _ in r['invariants'])
                                              and not (a.strict and r['drift']),
                                      'invariants': [{'ok': ok, 'detail': w}
                                                     for ok, w in r['invariants']],
                                      'drift': r['drift'],
                                      'summary': r['summary'], 'obs': r['obs']} for r in results],
                          'failed': failed, 'baseline_updated': updated}, ensure_ascii=False, indent=1))
        return 1 if failed else 0

    print('== 动态检查（冷启动复跑，证据在 work/dyn/）')
    print('\n'.join(lines))
    if a.update:
        print(f'== 已刷新基线 {os.path.relpath(BASELINE, ROOT)}' if updated else
              '!! 检查失败，基线未改动')
    if not base and not a.update:
        print('  (还没有基线；跑 `python3 tools/dyn_check.py run --update` 建立)')
    print(f'== {len(results)} 条检查，{failed} 条失败')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
