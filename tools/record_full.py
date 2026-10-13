#!/usr/bin/env python3
"""全程复跑路线并录像，同时为新 ROM 重采路线基线（译文改版后用）。

译文改动会让 ROM 哈希和沿途画面哈希必然漂移，record_video.py / autoplay.py
的严格比对会拒绝运行。本脚本把"重采基线"和"录像"合并为一次通过：
每段复跑时既按间隔截图，又在末帧抓 RAM 快照，逐段记录新哈希；
--finalize 把新基线原子写回 tests/autoplay/route.json（含结局画面哈希）。

用法：
    python3 tools/record_full.py --to 60                      # 第 1–60 段
    python3 tools/record_full.py --from-segment 61 --to 124   # 续跑到通关
    python3 tools/record_full.py --finalize                   # 刷新 route.json
    python3 tools/record_full.py --encode --out 全程.mp4      # 合成视频
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_utils import expand_keys  # noqa: E402
from record_video import SHOT_RE, digest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROM = ROOT / '侦探神宫寺三郎 - 白影的少女 (简中).gba'
HARNESS = ROOT / 'work/bin/gbarun_dbg'
ROUTE = ROOT / 'tests/autoplay/route.json'
WORK = ROOT / 'work/video-full'


def run_segments(route, first, last, shot_every):
    """复跑 [first, last] 段，截图 + 末帧 RAM 快照，逐段落盘新哈希。"""
    raw = WORK / 'raw'
    hashes = WORK / 'hashes'
    if first == 1:
        shutil.rmtree(WORK, ignore_errors=True)
    raw.mkdir(parents=True, exist_ok=True)
    hashes.mkdir(parents=True, exist_ok=True)
    state = None
    if first > 1:
        state = raw / f'seg{first - 1:04d}.ss1'
        if not state.is_file():
            raise SystemExit(f'缺少上一段存档：{state}（先跑前一段区间）')
    for number in range(first, last + 1):
        segment = route['segments'][number - 1]
        prefix = raw / f'seg{number:04d}.ppm'
        snap = raw / f'seg{number:04d}.snap'
        save = raw / f'seg{number:04d}.ss1'
        events = expand_keys(segment['keys'], segment['frames'])
        env = dict(os.environ)
        env['GBARUN_NO_AUTOSAVE'] = '1'
        env['GBARUN_SHOT_EVERY'] = str(shot_every)
        env['GBARUN_RAMSNAP'] = f"{segment['frames'] - 1}:{snap}"
        env['GBARUN_SAVE'] = str(save)
        if state:
            env['GBARUN_STATE'] = str(state)
        else:
            env.pop('GBARUN_STATE', None)
        cmd = [str(HARNESS), str(ROM), str(segment['frames']), str(prefix)] + events
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240)
        if proc.returncode:
            print(proc.stdout + proc.stderr, file=sys.stderr)
            raise SystemExit(f'第 {number} 段运行失败')
        final = Path(str(prefix) + '_final.ppm')
        new = {ext: digest(Path(f'{snap}.{ext}')) for ext in ('ewram', 'iwram', 'vram')}
        new['ppm'] = digest(final)
        old = segment['sha256']
        diff = [k for k in ('ewram', 'iwram', 'vram', 'ppm') if old.get(k) != new[k]]
        (hashes / f'seg{number:04d}.json').write_text(json.dumps(new, indent=2))
        note = f' 证据漂移: {",".join(diff)}' if diff else ' 证据一致'
        print(f'[{number}/{len(route["segments"])}] {segment.get("label", "")}{note}',
              flush=True)
        if not save.is_file():
            raise SystemExit(f'第 {number} 段未写出即时存档')
        state = save


def finalize(route):
    """把采集到的新哈希写回路线（原子替换），并核对段数完整。"""
    hashes = WORK / 'hashes'
    files = sorted(hashes.glob('seg*.json'))
    if len(files) != len(route['segments']):
        raise SystemExit(f'哈希不全：{len(files)}/{len(route["segments"])} 段，不能刷新基线')
    route['rom_sha256'] = digest(ROM)
    for i, path in enumerate(files):
        route['segments'][i]['sha256'] = json.loads(path.read_text())
    last = route['segments'][-1]['sha256']['ppm']
    if route.get('status') == 'complete':
        route.setdefault('ending', {})['ppm_sha256'] = last
    tmp = ROUTE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(route, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(ROUTE)
    print(f'== 已刷新 {ROUTE}（{len(files)} 段基线 + ROM 哈希）')


def encode(out, fps, scale):
    raw = WORK / 'raw'
    shots = []
    for p in raw.glob('seg*.ppm_shot*.ppm'):
        m = SHOT_RE.match(p.name)
        if m:
            shots.append((int(m.group(1)), int(m.group(2)), p))
    shots.sort()
    if not shots:
        raise SystemExit('没有截到任何画面')
    cmd = [
        'ffmpeg', '-y', '-framerate', str(fps),
        '-f', 'image2pipe', '-vcodec', 'ppm', '-i', '-',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-vf', f'scale={240 * scale}:{160 * scale}:flags=neighbor',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', '-movflags', '+faststart', str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for _, _, shot in shots:
        proc.stdin.write(Path(shot).read_bytes())
    proc.stdin.close()
    if proc.wait():
        raise SystemExit('ffmpeg 编码失败')
    minutes = len(shots) / fps / 60
    print(f'== 完成：{out}（{len(shots)} 帧，约 {minutes:.1f} 分钟）')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--from-segment', type=int, default=1)
    p.add_argument('--to', type=int, default=None)
    p.add_argument('--shot-every', type=int, default=32)
    p.add_argument('--finalize', action='store_true', help='把采集的哈希写回 route.json')
    p.add_argument('--encode', action='store_true', help='用已截图合成视频')
    p.add_argument('--fps', type=int, default=60, help='输出帧率（默认 60 = 旧视频 2 倍速）')
    p.add_argument('--scale', type=int, default=4)
    p.add_argument('--out', type=Path, default=ROOT / '白影的少女-自动游玩-全程-2x.mp4')
    args = p.parse_args(argv)

    route = json.loads(ROUTE.read_text())
    total = len(route['segments'])
    if args.finalize:
        finalize(route)
        return
    if args.encode:
        if not shutil.which('ffmpeg'):
            raise SystemExit('需要 ffmpeg（brew install ffmpeg）')
        args.out.parent.mkdir(parents=True, exist_ok=True)
        encode(args.out, args.fps, args.scale)
        return
    last = args.to or total
    if not 0 < args.from_segment <= last <= total:
        raise SystemExit(f'段号超出范围（路线共 {total} 段）')
    if not HARNESS.is_file():
        raise SystemExit('调试 harness 未编译：先跑 make dbg')
    print(f'== 复跑第 {args.from_segment}–{last} 段（每 {args.shot_every} 帧截图 + 末帧快照）')
    run_segments(route, args.from_segment, last, args.shot_every)


if __name__ == '__main__':
    main()
