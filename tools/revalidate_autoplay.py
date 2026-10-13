#!/usr/bin/env python3
"""Cold-run existing controller inputs on a rebuilt ROM and stage new evidence.

This never overwrites the accepted route. Intermediate hashes may change with
translation, but the known terminal screen must still match exactly. A staged
candidate must then pass a separate cold run with tools/autoplay.py before it
can replace tests/autoplay/route.json.
"""
import argparse
import copy
from pathlib import Path
import sys

from autoplay import ROOT, DEFAULT_ROM, atomic_json, digest, load_route
from harness_utils import HarnessError, run_harness
from work_paths import REVALIDATE, fixed_run


def revalidate(route_path, rom, harness, out, candidate, *, timeout=120):
    route, route_hash = load_route(route_path)
    if route['status'] != 'complete':
        raise ValueError('重新验证必须以已通关路线为起点')
    candidate, out = Path(candidate), Path(out)
    if candidate.resolve() == Path(route_path).resolve() or candidate.exists():
        raise ValueError('候选必须写入新文件，不能覆盖已有路线')
    # Require fresh evidence and a real cold boot, including after interruption.
    out.mkdir(parents=True, exist_ok=False)
    rom_hash = digest(Path(rom).read_bytes())
    updated = copy.deepcopy(route)
    updated['rom_sha256'] = rom_hash
    previous = None
    for number, segment in enumerate(updated['segments'], 1):
        prefix = out / f'segment-{number:04d}'
        checkpoint = prefix.with_suffix('.ss1')
        evidence, _ = run_harness(harness, rom, segment['frames'], segment['keys'],
                                  prefix, state=previous, save=checkpoint, timeout=timeout)
        segment['sha256'] = {k: digest(evidence[k]) for k in ('ewram', 'iwram', 'vram', 'ppm')}
        previous = checkpoint
        print(f'[{number}/{len(updated["segments"])}] {segment.get("label", "captured")}', flush=True)
    if updated['segments'][-1]['sha256']['ppm'] != route['ending']['ppm_sha256']:
        raise HarnessError('新 ROM 未到达已验证的通关画面；不生成候选或更新基线')
    if digest(Path(rom).read_bytes()) != rom_hash:
        raise HarnessError('运行期间 ROM 被修改；不生成候选')
    candidate.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(candidate, updated)
    _, candidate_hash = load_route(candidate)
    result = {'status': 'CANDIDATE_REQUIRES_REPLAY', 'cold_start': True,
              'source_route_sha256': route_hash, 'rom_sha256': rom_hash,
              'candidate_sha256': candidate_hash, 'segments': len(updated['segments']),
              'frames': sum(s['frames'] for s in updated['segments']), 'ending': updated['ending']}
    atomic_json(out / 'result.json', result)
    print(f'已到达通关画面。候选：{candidate}；仍需 autoplay 独立冷启动严格验收。', flush=True)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--route', type=Path, default=ROOT / 'tests/autoplay/route.json')
    p.add_argument('--rom', type=Path, default=DEFAULT_ROM)
    p.add_argument('--harness', type=Path, default=ROOT / 'work/bin/gbarun_dbg')
    args = p.parse_args(argv)
    try:
        route, _ = load_route(args.route)
        if route['status'] != 'complete' or not args.rom.is_file() or not args.harness.is_file():
            raise ValueError('需要完整路线、现有 ROM 和已编译的 harness')
        with fixed_run(REVALIDATE, inputs=(args.route, args.rom, args.harness)):
            revalidate(args.route, args.rom, args.harness, REVALIDATE / 'checkpoints',
                       REVALIDATE / 'route.candidate.json')
    except (OSError, ValueError, HarnessError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
