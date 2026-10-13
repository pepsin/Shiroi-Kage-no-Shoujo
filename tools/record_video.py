#!/usr/bin/env python3
"""把自动游玩路线复跑并录成视频（默认第 1–2 天，约 5 分钟）。

复用 tests/autoplay/route.json 里已验证的按键路线：逐段复跑、每段结尾比对
末帧哈希（不偏离已验证路线），按固定间隔截图，最后用 ffmpeg 合成 mp4。
中间产物（截图 / 即时存档）落在 work/video/，成功后自动清理。

天数与路线的对应（docs/调试器.md）：3月11日 = e1、3月12日 = e16、3月13日 = e57，
即第 1–9 段是第 1 天，第 10–25 段是第 2 天（第 25 段结尾停在 3/12 夜）。

用法：
    python3 tools/record_video.py                      # 第 1–2 天 → 项目根目录 mp4
    python3 tools/record_video.py --last-segment 40 --out work/video/out.mp4
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

ROOT = Path(__file__).resolve().parents[1]
ROM = ROOT / '侦探神宫寺三郎 - 白影的少女 (简中).gba'
HARNESS = ROOT / 'work/bin/gbarun_dbg'
ROUTE = ROOT / 'tests/autoplay/route.json'
WORK = ROOT / 'work/video'
DEFAULT_OUT = ROOT / '白影的少女-自动游玩-第1-2天.mp4'
SHOT_RE = re.compile(r'seg(\d{4})\.ppm_shot(\d{6})\.ppm$')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_segments(route, last_segment, shot_every, raw):
    """逐段复跑并周期截图；每段末帧与路线基线比对。返回各段截图清单。"""
    state = None
    for number, segment in enumerate(route['segments'][:last_segment], 1):
        prefix = raw / f'seg{number:04d}.ppm'
        save = raw / f'seg{number:04d}.ss1'
        events = expand_keys(segment['keys'], segment['frames'])
        env = dict(os.environ)
        env['GBARUN_NO_AUTOSAVE'] = '1'
        env['GBARUN_SHOT_EVERY'] = str(shot_every)
        env['GBARUN_SAVE'] = str(save)
        if state:
            env['GBARUN_STATE'] = str(state)
        else:
            env.pop('GBARUN_STATE', None)
        cmd = [str(HARNESS), str(ROM), str(segment['frames']), str(prefix)] + events
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
        if proc.returncode:
            print(proc.stdout + proc.stderr, file=sys.stderr)
            raise SystemExit(f'第 {number} 段运行失败')
        final = Path(str(prefix) + '_final.ppm')
        if digest(final) != segment['sha256']['ppm']:
            raise SystemExit(f'第 {number} 段偏离已验证路线（末帧哈希不符）')
        if not save.is_file():
            raise SystemExit(f'第 {number} 段未写出即时存档')
        state = save
        print(f'[{number}/{last_segment}] OK {segment.get("label", "")}', flush=True)
    shots = []
    for p in raw.glob('seg*.ppm_shot*.ppm'):
        m = SHOT_RE.match(p.name)
        if m:
            shots.append((int(m.group(1)), int(m.group(2)), p))
    shots.sort()
    if not shots:
        raise SystemExit('没有截到任何画面')
    return [p for _, _, p in shots]


def encode(shots, out, fps, scale):
    """把截图按序喂给 ffmpeg（image2pipe，避免上万文件的改名搬运）。"""
    width, height = 240 * scale, 160 * scale
    cmd = [
        'ffmpeg', '-y', '-framerate', str(fps),
        '-f', 'image2pipe', '-vcodec', 'ppm', '-i', '-',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-vf', f'scale={width}:{height}:flags=neighbor',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '19', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', '-movflags', '+faststart', str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for shot in shots:
        proc.stdin.write(Path(shot).read_bytes())
    proc.stdin.close()
    if proc.wait():
        raise SystemExit('ffmpeg 编码失败')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--last-segment', type=int, default=25,
                   help='录到路线第几段（默认 25 = 第 1–2 天）')
    p.add_argument('--shot-every', type=int, default=32, help='每多少帧截一张（默认 32）')
    p.add_argument('--fps', type=int, default=30, help='输出帧率（默认 30）')
    p.add_argument('--scale', type=int, default=4, help='放大倍数（默认 4 → 960x640）')
    p.add_argument('--out', type=Path, default=DEFAULT_OUT, help='输出 mp4 路径')
    p.add_argument('--keep-work', action='store_true', help='保留 work/video/ 中间产物')
    args = p.parse_args(argv)

    if not shutil.which('ffmpeg'):
        raise SystemExit('需要 ffmpeg（brew install ffmpeg）')
    if not HARNESS.is_file():
        raise SystemExit('调试 harness 未编译：先跑 make dbg')
    route = json.loads(ROUTE.read_text())
    if digest(ROM) != route['rom_sha256']:
        raise SystemExit('ROM 与路线版本不一致；先 make rom 并重新验证路线')
    if not 0 < args.last_segment <= len(route['segments']):
        raise SystemExit(f'段号超出范围（路线共 {len(route["segments"])} 段）')

    raw = WORK / 'raw'
    shutil.rmtree(WORK, ignore_errors=True)
    raw.mkdir(parents=True)
    print(f'== 复跑第 1–{args.last_segment} 段并截图（每 {args.shot_every} 帧一张）')
    shots = record_segments(route, args.last_segment, args.shot_every, raw)
    seconds = len(shots) / args.fps
    print(f'== 共 {len(shots)} 帧，编码 {args.out.name}（约 {seconds / 60:.1f} 分钟）')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    encode(shots, args.out, args.fps, args.scale)
    if not args.keep_work:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f'== 完成：{args.out}')


if __name__ == '__main__':
    main()
