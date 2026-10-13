"""Read-only choice screenshots and an offline review gallery (stdlib only).

The harness captures the rendered frame immediately before each input and at
segment boundaries. Classification uses the game's UI mode, script identity,
and visible sprite layout; no OCR, AI, injected inputs or modified RAM.
"""
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import struct
import zlib

import entry_id
from game_observe import monochrome
from harness_utils import HarnessError

KINDS = {'choice': '对话选项', 'inference': '推理选项', 'command': '行动菜单',
         'inventory': '物品选择', 'map': '地图选择'}
PPM_HEADER = b'P6\n240 160\n255\n'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def png_from_ppm(ppm):
    """Losslessly store the actual emulator pixels without an imaging dependency."""
    if not ppm.startswith(PPM_HEADER) or len(ppm) != len(PPM_HEADER) + 240 * 160 * 3:
        raise HarnessError('审核截图不是完整的 240×160 RGB 画面')
    pixels = ppm[len(PPM_HEADER):]
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    scanlines = b''.join(b'\0' + pixels[y*720:(y+1)*720] for y in range(160))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', 240, 160, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b''))


def glyph_sprites(vram, oam):
    """Visible 16×16 glyph masks; palette/highlight changes are irrelevant."""
    sprites = []
    for i in range(128):
        a, b, c, _ = struct.unpack_from('<4H', oam, i * 8)
        x, y = b & 511, a & 255
        if a & 0x300 or ((a >> 14) & 3, (b >> 14) & 3) != (0, 1) or x >= 240 or y >= 160:
            continue
        offset = 0x10000 + (c & 1023) * 32
        mask = monochrome(vram[offset:offset + 128])
        if len(mask) == 128 and any(mask):
            sprites.append((x, y, mask))
    return sorted(sprites)


def classify(ui, entry, sprites):
    ys = {y for _, y, _ in sprites}
    if ui == 4 and any(y >= 130 for y in ys):
        return 'choice'
    if ui == 2:
        return 'command'
    if ui == 3 and 46 in ys:
        return 'inventory'
    if ui == 0 and entry is not None:
        if 316 <= entry <= 416 and len({y for y in ys if 20 <= y < 98}) >= 2:
            return 'inference'
        if 282 <= entry <= 296:
            return 'map'
    return None


class ChoiceReview:
    def __init__(self, directory, rom, route_hash, *, completed=0, resume=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'index.json'
        self.index = entry_id.RomIndex(rom)
        self.entry_cache = {}
        self.identity = {'version': 1, 'rom_sha256': sha(rom), 'route_sha256': route_hash}
        if resume and completed:
            self.data = json.loads(self.path.read_text())
            if any(self.data.get(k) != v for k, v in self.identity.items()):
                raise ValueError('选项截图索引与当前 ROM／路线不一致')
            if self.data['completed_segments'] < completed:
                raise ValueError('选项截图缺少已完成分段；请从新目录冷启动')
            # A crash can happen after gallery publication but before progress.json.
            # Roll only this run's uncommitted gallery records back before replay.
            kept = []
            for item in self.data['screenshots']:
                name = item['file']
                if Path(name).name != name or not name.endswith('.png'):
                    raise ValueError('选项截图索引中的文件名无效')
                item['occurrences'] = [o for o in item['occurrences'] if o['segment'] <= completed]
                if not item['occurrences']:
                    (self.directory / name).unlink(missing_ok=True)
                    continue
                if sha((self.directory / name).read_bytes()) != item['sha256']:
                    raise ValueError(f'审核截图缺失或损坏：{name}')
                kept.append(item)
            self.data['screenshots'] = kept
        else:
            if self.path.exists():
                raise ValueError('选项截图目录已有索引；请使用 --resume 或新输出目录')
            self.data = {**self.identity, 'screenshots': []}
        self.by_signature = {x['signature']: x for x in self.data['screenshots']}
        self.publish(completed)

    def resolve(self, ewram):
        pool, _, buf, _, key_b, rows = struct.unpack_from('<6I', ewram, 0x3F24)
        if not 0x02020000 <= pool < 0x02040000 or not 0x02000000 <= buf < 0x02040000:
            return None
        start = buf - 0x02000000
        # Several map scripts share an identical structural header; resolution
        # also compares the text window. Cache exactly that input, not just the
        # header, or separate scripts would acquire the first one's entry ID.
        key = (key_b, rows, ewram[start:start + entry_id.DEFAULT_WINDOW])
        if key not in self.entry_cache:
            match = entry_id.resolve_from_ram(self.index, ewram)[0]
            self.entry_cache[key] = match.eid if match.structural_complete and not match.ambiguous else None
        return self.entry_cache[key]

    def consume(self, raw, segment, preceding_frames):
        for snapshot in sorted(Path(raw).glob('frame-*.bin')):
            frame = int(snapshot.stem.split('-')[1])
            data = snapshot.read_bytes()
            if len(data) != 0x60400:
                raise HarnessError('选项快照长度错误')
            ewram, vram, oam = data[:0x40000], data[0x48000:0x60000], data[0x60000:]
            ui = struct.unpack_from('<I', ewram, 0x15160)[0]
            entry = self.resolve(ewram)
            sprites = glyph_sprites(vram, oam)
            kind = classify(ui, entry, sprites)
            if kind is None:
                continue
            ppm = snapshot.with_suffix('.ppm').read_bytes()
            # Text choices deduplicate highlight changes, while keeping every
            # newly scrolled page and each separate script instance.
            visual = (b''.join(struct.pack('<2H', x, y) + mask for x, y, mask in sprites)
                      if kind in ('choice', 'inference') else ppm)
            signature = sha(f'{kind}:{entry}:'.encode() + visual)
            occurrence = {'segment': segment, 'frame': frame,
                          'total_frame': preceding_frames + frame}
            item = self.by_signature.get(signature)
            if item is None:
                number = len(self.data['screenshots']) + 1
                name = f'{number:05d}-{kind}-e{entry if entry is not None else "unknown"}.png'
                png = png_from_ppm(ppm)
                (self.directory / name).write_bytes(png)
                item = {'file': name, 'kind': kind, 'entry': entry, 'signature': signature,
                        'sha256': sha(png), 'occurrences': []}
                self.data['screenshots'].append(item)
                self.by_signature[signature] = item
            item['occurrences'].append(occurrence)
        self.publish(segment)

    def publish(self, completed, status='IN_PROGRESS'):
        self.data.update(completed_segments=completed, status=status,
                         counts=dict(Counter(x['kind'] for x in self.data['screenshots'])))
        atomic_json(self.path, self.data)
        cards = []
        for number, item in enumerate(self.data['screenshots'], 1):
            occurrence = item['occurrences'][0]
            label = f'{number:04d} · {KINDS[item["kind"]]} · e{item["entry"]}'
            details = (f'段 {occurrence["segment"]} / 段内帧 {occurrence["frame"]} / '
                       f'总帧 {occurrence["total_frame"]} · 出现 {len(item["occurrences"])} 次')
            name = html.escape(item['file'], quote=True)
            cards.append(f'<figure data-kind="{item["kind"]}"><a href="{name}"><img loading="lazy" '
                         f'src="{name}" alt="{html.escape(label)}"></a><figcaption>{label}<br>'
                         f'<small>{details}</small></figcaption></figure>')
        filters = ''.join(f'<option value="{key}">{label}</option>' for key, label in KINDS.items())
        page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>选项画面审核</title>'
                '<style>body{font:16px system-ui;background:#17191e;color:#eee;margin:24px}'
                'main{display:flex;flex-wrap:wrap;gap:18px}figure{margin:0;padding:12px;background:#252832}'
                'img{width:480px;max-width:100%;image-rendering:pixelated}small{color:#bbc1cd}'
                'figcaption{margin-top:8px}select{font:inherit;margin:12px 0 24px}</style>'
                f'<h1>选项画面审核 · {len(cards)} 张</h1><p>状态：{status}；完成 {completed} 段。'
                '仅包含本路线实际遇到的画面；推理列表保留滚动后新显示的页。'
                '同一脚本内只改变高亮的文字选项会合并，完整出现位置见 index.json。</p>'
                f'<select aria-label="画面类型"><option value="">全部</option>{filters}</select>'
                '<main>' + ''.join(cards) + '</main><script>document.querySelector("select").onchange=e=>'
                'document.querySelectorAll("figure").forEach(x=>x.hidden=!!e.target.value&&'
                'x.dataset.kind!==e.target.value)</script></html>')
        temporary = self.directory / 'index.html.tmp'
        temporary.write_text(page)
        temporary.replace(self.directory / 'index.html')

    def finish(self, segments, status):
        self.publish(segments, status)
        return {'screenshots': len(self.data['screenshots']), 'counts': self.data['counts'],
                'index': str((self.directory / 'index.html').resolve())}
