#!/usr/bin/env python3
"""Cold-boot gpSP, save through normal input, then reload in a fresh process.

Build legacy/libretro_probe.c against the tested core's libretro.h first.
This is a desktop core test, not proof of PSP hardware compatibility.
Evidence goes into a fresh directory beneath work/compat/gpsp-check/.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from entry_id import resolve_from_ram
from harness_utils import expand_keys
from save_check import verdict

ROOT = Path(__file__).resolve().parents[1]
BUTTONS = dict(B=0, SELECT=2, START=3, UP=4, DOWN=5, LEFT=6, RIGHT=7,
               A=8, L=10, R=11)


def write_keys(path, frames, keys):
    masks = [0] * frames
    for event in expand_keys(keys, frames):
        key, span = event.split('@')
        start, hold = map(int, span.split(':'))
        for frame in range(start, min(start + hold, frames)):
            masks[frame] |= 1 << BUTTONS[key]
    path.write_text('\n'.join(map(str, masks)) + '\n', encoding='ascii')


def run(runner, core, rom, out, frames, keys, save=None):
    out.mkdir()
    keyfile = out / 'input.keys'
    write_keys(keyfile, frames, keys)
    cmd = [str(runner), str(core), str(rom), str(out), str(keyfile)]
    if save:
        cmd.append(str(save))
    with (out / 'run.log').open('w') as log:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                       timeout=120, check=True)
    prefix = out / f'frame-{frames:06}'
    for ext, size in [('ewram', 0x40000), ('iwram', 0x8000),
                      ('ppm', len(b'P6\n240 160\n255\n') + 240 * 160 * 3)]:
        if prefix.with_suffix('.' + ext).stat().st_size != size:
            raise ValueError(f'不完整的 {ext} 证据')
    return prefix


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--core', type=Path, required=True)
    ap.add_argument('--runner', type=Path, default=ROOT / 'work/bin/libretro_probe')
    ap.add_argument('--rom', type=Path,
                    default=ROOT / '侦探神宫寺三郎 - 白影的少女 (简中).gba')
    a = ap.parse_args()
    a.core, a.runner, a.rom = (p.resolve() for p in (a.core, a.runner, a.rom))
    for p in (a.core, a.runner, a.rom):
        if not p.is_file():
            ap.error(f'找不到输入：{p}')
    base = ROOT / 'work/compat/gpsp-check'
    base.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    summary = dict(rom=str(a.rom), core=str(a.core),
                   rom_sha256=hashlib.sha256(a.rom.read_bytes()).hexdigest(),
                   core_sha256=hashlib.sha256(a.core.read_bytes()).hexdigest(),
                   passed=False)
    try:
        checks = json.loads((ROOT / 'tests/dyn/checks.json').read_text())['checks']
        route = next(c for c in checks if c['name'] == 'cn-funeral-to-reception')
        prefix = run(a.runner, a.core, a.rom, out / 'save', route['frames'], route['keys'])
        # gpSP's libretro save buffer reserves 128 KiB for every cartridge.
        # This game's EEPROM is the first 8 KiB. Never touch a player save.
        data = prefix.with_suffix('.sav').read_bytes()
        if len(data) != 0x20000:
            raise ValueError('gpSP 存档缓冲区长度异常')
        data = data[:8192]
        save = out / 'cartridge.sav'
        save.write_bytes(data)
        code, header, slots, message = verdict(data)
        summary['save_verdict'] = message
        if code or not header['sum_ok'] or not slots[0]['ok'] or not (header['mem'][8] & 1):
            raise ValueError(f'保存失败：{message}')
        prefix = run(a.runner, a.core, a.rom, out / 'reload', 8500,
                     ['START@5000:4', 'DOWN@5300:4', 'A@5500:4',
                      'A@6000:4', 'A@6300:4', 'LEFT@6600:4', 'A@6800:4'], save)
        match, _ = resolve_from_ram(a.rom.read_bytes(), prefix.with_suffix('.ewram').read_bytes())
        summary['reload_entry'] = match.eid
        if match.eid != 1 or not match.structural_complete or match.ambiguous:
            raise ValueError(f'读档后未回到葬礼调查场景：{match}')
        summary['passed'] = True
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        summary['error'] = str(exc)
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'证据：{out}')
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
