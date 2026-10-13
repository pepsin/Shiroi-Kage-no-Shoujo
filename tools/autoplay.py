#!/usr/bin/env python3
"""Replay a verified, normal-input route without AI, a browser, or network access.

The manifest contains controller input, not RAM writes. Each segment starts from
the preceding checkpoint. A partial route can be tested but never reports CLEAR.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from harness_utils import HarnessError, expand_keys, run_harness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROM = ROOT / '侦探神宫寺三郎 - 白影的少女 (简中).gba'
SHA = re.compile(r'[0-9a-f]{64}')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def load_route(path):
    raw = Path(path).read_bytes()
    route = json.loads(raw)
    if not isinstance(route, dict) or route.get('version') != 1:
        raise ValueError('路线版本必须为 1')
    if route.get('status') not in ('partial', 'complete'):
        raise ValueError('路线必须明确标记 partial 或 complete')
    if not SHA.fullmatch(str(route.get('rom_sha256', ''))):
        raise ValueError('路线缺少有效 ROM SHA256')
    segments = route.get('segments')
    if not isinstance(segments, list) or not segments:
        raise ValueError('路线不能为空')
    for i, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f'第 {i + 1} 段不是对象')
        if type(segment.get('frames')) is not int or not 0 < segment['frames'] <= 100000:
            raise ValueError(f'第 {i + 1} 段帧数无效')
        events = expand_keys(segment.get('keys'), segment['frames'])
        for event in events:
            start, hold = map(int, event.split('@')[1].split(':'))
            if start + hold > segment['frames']:
                raise ValueError('按键不能跨越分段边界')
        expected = segment.get('sha256', {})
        if not isinstance(expected, dict) or any(
                k not in ('ewram', 'iwram', 'vram', 'ppm') or not SHA.fullmatch(str(v))
                for k, v in expected.items()):
            raise ValueError('无效证据哈希')
        if not all(k in expected for k in ('ewram', 'iwram', 'vram', 'ppm')):
            raise ValueError(f'第 {i + 1} 段必须有全部内存和截图证据哈希')
    if route['status'] == 'complete':
        ending = route.get('ending')
        if not isinstance(ending, dict) or not ending.get('description'):
            raise ValueError('完整路线必须注明实际观察到的结局')
        if not SHA.fullmatch(str(ending.get('ppm_sha256', ''))):
            raise ValueError('完整路线必须包含结局画面哈希')
        if segments[-1]['sha256'].get('ppm') != ending['ppm_sha256']:
            raise ValueError('终点证据与结局画面不一致')
    return route, digest(raw)


def replay(route, route_hash, rom, harness, out, *, resume=False, timeout=120):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    # A previous success report must not survive a failing retry.
    (out / 'result.json').unlink(missing_ok=True)
    actual_rom = digest(Path(rom).read_bytes())
    if actual_rom != route['rom_sha256']:
        raise ValueError('ROM 与路线版本不一致；不能沿用旧路线证明新 ROM 通关')
    journal_path = out / 'progress.json'
    completed = 0
    previous = None
    if resume:
        progress = json.loads(journal_path.read_text())
        if progress.get('route_sha256') != route_hash or progress.get('rom_sha256') != actual_rom:
            raise ValueError('断点与当前路线/ROM 不一致')
        completed = progress.get('completed_segments')
        if type(completed) is not int or not 0 <= completed <= len(route['segments']):
            raise ValueError('断点段号无效')
        if completed:
            previous = out / f'segment-{completed:04d}.ss1'
            if digest(previous.read_bytes()) != progress.get('state_sha256'):
                raise ValueError('断点文件损坏或被替换')
    elif journal_path.exists():
        raise ValueError('输出目录已有运行记录；用 --resume 续跑，或另选 --out 目录')
    for number, segment in enumerate(route['segments'][completed:], completed + 1):
        prefix = out / f'segment-{number:04d}'
        checkpoint = prefix.with_suffix('.ss1')
        evidence, _ = run_harness(harness, rom, segment['frames'], segment['keys'],
                                  prefix, state=previous, save=checkpoint, timeout=timeout)
        mismatch = [ext for ext, expected in segment['sha256'].items()
                    if digest(evidence[ext]) != expected]
        if mismatch:
            raise HarnessError(f'第 {number} 段偏离已验证路线：{", ".join(mismatch)}；证据：{prefix}')
        atomic_json(journal_path, {'route_sha256': route_hash, 'rom_sha256': actual_rom,
                                  'completed_segments': number,
                                  'state_sha256': digest(checkpoint.read_bytes())})
        previous = checkpoint
        print(f'[{number}/{len(route["segments"])}] {segment.get("label", "PASS")}', flush=True)
    # Resume of an already completed run still verifies its final screenshot.
    final = out / f'segment-{len(route["segments"]):04d}.ppm'
    if digest(final.read_bytes()) != route['segments'][-1]['sha256']['ppm']:
        raise HarnessError('终点截图缺失或不匹配')
    status = 'CLEAR' if route['status'] == 'complete' else 'PARTIAL'
    result = {'status': status, 'route_sha256': route_hash, 'rom_sha256': actual_rom,
              'segments': len(route['segments']),
              'frames': sum(s['frames'] for s in route['segments']),
              'ending': route.get('ending'), 'screenshot': str(final.resolve())}
    atomic_json(out / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--route', type=Path, default=ROOT / 'tests/autoplay/route.json')
    p.add_argument('--rom', type=Path, default=DEFAULT_ROM)
    p.add_argument('--harness', type=Path, default=ROOT / 'work/bin/gbarun_dbg')
    p.add_argument('--out', type=Path, help='证据目录；默认每次创建新的运行目录')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--allow-partial', action='store_true', help='调试未完成路线；结果只能为 PARTIAL')
    args = p.parse_args(argv)
    try:
        if args.resume and args.out is None:
            raise ValueError('--resume 必须用 --out 指定要恢复的运行目录')
        if args.out is None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            args.out = ROOT / 'work/autoplay' / f'{stamp}-{uuid.uuid4().hex[:8]}'
        route, fingerprint = load_route(args.route)
        if route['status'] != 'complete' and not args.allow_partial:
            raise ValueError('路线尚未通关；调试请显式使用 --allow-partial')
        result = replay(route, fingerprint, args.rom, args.harness, args.out, resume=args.resume)
    except (OSError, ValueError, HarnessError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    return 0 if result['status'] == 'CLEAR' else 3


if __name__ == '__main__':
    sys.exit(main())
