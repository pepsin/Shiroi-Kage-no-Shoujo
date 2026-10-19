#!/usr/bin/env python3
"""Replay selected verified route segments and capture dialogue plus choices.

No memory writes, text injection or extra inputs. Requires checkpoints from a
replay of the exact ROM/route; both the incoming RAM and the resulting evidence
must match. Run autoplay first. Output: work/dialogue-review/index.html.
"""
import argparse
import csv
import html
import json
from pathlib import Path
import shutil
import struct

from autoplay import DEFAULT_ROM, ROOT, digest, load_route
from choice_review import png_from_ppm
import entry_id
from game_observe import SpriteReader
import gbtext
from harness_utils import HarnessError, run_harness
from state_inspect import read_state
from work_paths import AUTOPLAY, WORK, fixed_run

OUT = WORK / 'dialogue-review'


def numbers(spec):
    values = set()
    for token in spec.split(','):
        parts = token.split('-')
        if len(parts) == 1:
            lo = hi = int(parts[0])
        elif len(parts) == 2:
            lo, hi = map(int, parts)
        else:
            raise ValueError('段号使用 39-41,53 这样的格式')
        if lo < 1 or hi < lo:
            raise ValueError('段号范围必须为正且递增')
        values.update(range(lo, hi + 1))
    return sorted(values)


def check_checkpoint(path, expected):
    state = read_state(path)
    for region, key in [('wram', 'ewram'), ('iwram', 'iwram'), ('vram', 'vram')]:
        if digest(state[region]) != expected[key]:
            raise ValueError(f'断点不属于该路线：{path} / {key}')


def review(route_path, checkpoint_dir, rom_path, segments, entries):
    route, route_hash = load_route(route_path)
    rom = rom_path.read_bytes()
    if digest(rom) != route['rom_sha256']:
        raise ValueError('ROM 与路线不一致；请先重新验证并独立回放')
    if max(segments) > len(route['segments']):
        raise ValueError('段号超过路线范围')
    index = entry_id.RomIndex(rom)
    with (ROOT / 'work/glyph_map.ext.csv').open() as f:
        glyph_map = {int(r['code'], 16): r['char'] for r in csv.DictReader(f)}
    reader = SpriteReader(gbtext.load_entry(rom, 850), glyph_map, rom)
    records, seen, cache = [], set(), {}
    with fixed_run(OUT, inputs=(route_path, checkpoint_dir, rom_path)):
        for number in segments:
            segment = route['segments'][number - 1]
            state = checkpoint_dir / f'segment-{number-1:04d}.ss1' if number > 1 else None
            if state:
                check_checkpoint(state, route['segments'][number - 2]['sha256'])
            raw = OUT / 'raw'
            evidence, _ = run_harness(ROOT / 'work/bin/gbarun_dbg', rom_path,
                segment['frames'], segment['keys'], OUT / f'segment-{number:04d}',
                state=state, timeout=120, choice_capture=raw, dialogue_capture=True)
            if any(digest(evidence[k]) != v for k, v in segment['sha256'].items()):
                raise HarnessError(f'第 {number} 段与严格回放证据不一致')
            for snapshot in sorted(raw.glob('frame-*.bin')):
                data = snapshot.read_bytes()
                ew, iw = data[:0x40000], data[0x40000:0x48000]
                vram, oam = data[0x48000:0x60000], data[0x60000:]
                descriptor = ew[0x3F24:0x3F3C]
                offset = struct.unpack_from('<I', descriptor, 8)[0] - 0x02000000
                key = descriptor + ew[offset:offset + entry_id.DEFAULT_WINDOW]
                if key not in cache:
                    match, _ = entry_id.resolve_from_ram(index, ew)
                    cache[key] = match.eid if match.structural_complete and not match.ambiguous else None
                entry = cache[key]
                if entries and entry not in entries:
                    continue
                text = ' / '.join(t for t in reader.text({'vram': vram, 'oam': oam}).values() if t.strip())
                signature = (entry, text)
                if not text.strip() or signature in seen:
                    continue
                seen.add(signature)
                frame = int(snapshot.stem.split('-')[1])
                filename = f's{number:03d}-f{frame:05d}-e{entry}.png'
                (OUT / filename).write_bytes(png_from_ppm(snapshot.with_suffix('.ppm').read_bytes()))
                records.append(dict(segment=number, frame=frame, entry=entry,
                    pool_offset=struct.unpack_from('<I', iw, 0x7C44)[0], text=text, file=filename))
            shutil.rmtree(raw)
            print(f'已核对第 {number} 段，累计 {len(records)} 张画面', flush=True)
        if digest(rom_path.read_bytes()) != route['rom_sha256']:
            raise ValueError('运行期间 ROM 被修改')
        report = dict(status='VERIFIED_SEGMENTS', rom_sha256=route['rom_sha256'],
                      route_sha256=route_hash, segments=segments, records=records,
                      note='实际按键前画面；局部复跑，不作为独立冷启动通关证据。文字识别仅供检索，以图片为准。')
        (OUT / 'index.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        with (OUT / 'screens.tsv').open('w') as f:
            writer = csv.writer(f, delimiter='\t', lineterminator='\n')
            writer.writerow(['segment', 'frame', 'entry', 'text', 'file'])
            writer.writerows([r[k] for k in ['segment', 'frame', 'entry', 'text', 'file']] for r in records)
        cards = ''.join('<figure><img loading="lazy" src="' + r['file'] + '"><figcaption>' +
                       html.escape(f"段 {r['segment']} / 帧 {r['frame']} / e{r['entry']}：{r['text']}") +
                       '</figcaption></figure>' for r in records)
        (OUT / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>对白上下文审核</title>'
            '<style>body{background:#222;color:#eee;font:16px system-ui;margin:24px}'
            'main{display:flex;flex-wrap:wrap;gap:20px}figure{width:480px;margin:0}'
            'img{width:100%;image-rendering:pixelated}figcaption{margin:12px 0}</style>'
            '<h1>对白上下文审核</h1><p>' + html.escape(report['note']) + '</p><main>' + cards + '</main>')
        print(OUT / 'index.html')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--segments', required=True, help='例如 39-41,53,79')
    parser.add_argument('--entries', help='只保留这些条目的画面，例如 84,504,115')
    parser.add_argument('--route', type=Path, default=ROOT / 'tests/autoplay/route.json')
    parser.add_argument('--checkpoints', type=Path, default=AUTOPLAY / 'checkpoints')
    parser.add_argument('--rom', type=Path, default=DEFAULT_ROM)
    args = parser.parse_args()
    try:
        review(args.route, args.checkpoints, args.rom, numbers(args.segments),
               set(map(int, args.entries.split(','))) if args.entries else set())
    except (ValueError, OSError, HarnessError) as exc:
        print(f'FAIL: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
