#!/usr/bin/env python3
"""Package an observed cold-start key trace and checkpoint evidence for replay.

Exporting is not verification: run autoplay.py on the result from a clean boot.
No copyrighted ROM, RAM dump, or save state is included in the manifest.
"""
import argparse
import json
from pathlib import Path

from autoplay import atomic_json, digest, load_route
from harness_utils import expand_keys
from state_inspect import read_state


def compact(events):
    result = []
    i = 0
    while i < len(events):
        button, start, hold = events[i]
        j = i + 1
        if j < len(events) and events[j][0] == button and events[j][2] == hold:
            period = events[j][1] - start
            j += 1
            while (period > 0 and j < len(events) and events[j][0] == button
                   and events[j][2] == hold and events[j][1] - events[j-1][1] == period):
                j += 1
            if period > 0 and j - i >= 3:
                result.append(f'{button}@{start}-{events[j-1][1]}:{hold}:{period}')
                i = j
                continue
        result.append(f'{button}@{start}:{hold}')
        i += 1
    return result


def export(session, output, *, through=None, ending=None, segment_frames=12000):
    from PIL import Image
    session = Path(session)
    metadata = json.loads((session / 'session.json').read_text())
    through = through if through is not None else metadata['step']
    records = [json.loads(line) for line in (session / 'route.jsonl').read_text().splitlines()]
    records = [r for r in records if r['step'] <= through]
    if [r['step'] for r in records] != list(range(1, through + 1)) or not records:
        raise ValueError('路线必须从第 1 步冷启动开始，且记录完整连续')
    segments = []
    elapsed, events = 0, []
    for r in records:
        for key in expand_keys(r['keys'], r['frames']):
            button, timing = key.split('@')
            start, hold = map(int, timing.split(':'))
            # Original invocations release all keys at their boundary.
            events.append((button, elapsed + start, min(hold, r['frames'] - start)))
        elapsed += r['frames']
        if elapsed >= segment_frames or r is records[-1] or len(events) >= 3500:
            checkpoint = session / f'state-{r["step"]:04d}.ss1'
            state = read_state(checkpoint)
            with Image.open(checkpoint) as im:
                if im.size != (240, 160):
                    raise ValueError('checkpoint screenshot size is not 240x160')
                ppm = b'P6\n240 160\n255\n' + im.convert('RGB').tobytes()
            evidence = {'ewram': state['wram'], 'iwram': state['iwram'],
                        'vram': state['vram'], 'ppm': ppm}
            obs = r.get('obs', {})
            segments.append({'label': f'step {r["step"]}, e{obs.get("entry")}, {obs.get("rid")}',
                             'frames': elapsed, 'keys': compact(events),
                             'sha256': {k: digest(v) for k, v in evidence.items()}})
            elapsed, events = 0, []
    route = {'version': 1, 'status': 'complete' if ending else 'partial',
             'rom_sha256': metadata['rom_sha256'], 'segments': segments}
    if ending:
        route['ending'] = {'description': ending, 'ppm_sha256': segments[-1]['sha256']['ppm']}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, route)
    load_route(output)
    print(f'{route["status"]}: {len(segments)} segments, {sum(s["frames"] for s in segments)} frames')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('session', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--through', type=int)
    p.add_argument('--ending', help='仅在实际观察到结局后填写；导出后必须重新冷启动验证')
    args = p.parse_args()
    export(args.session, args.output, through=args.through, ending=args.ending)
