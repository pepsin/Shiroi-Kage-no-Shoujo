#!/usr/bin/env python3
"""scene_lookup.py — 把「条目 + 行下标」翻成「哪一幕、第几句、译文是什么」（只读）。

`entry_id.py` 反查出 entry 号之后，还需要这层才能把它挂到项目自己的坐标系上：

    data/scenes.tsv      场景号 ↔ (entry, idx_from, idx_to)、类型、地点、说话人
    data/scene_lines.tsv rid / entry / idx / scene / jp_text / translation

用法（作为库）：

    import scene_lookup
    lk = scene_lookup.Lookup()
    lk.scene_of(438, 0)   -> {'scene': '438.0', 'kind': 'scene',
                              'rid': '438:0', 'jp': '哲司“““', 'cn': '哲司“““'}
    lk.scenes_of(438)     -> [('438.0', 'scene')]

实现说明：scene_lines.tsv 按需扫描并缓存查询过的条目；实际文本偏移到译文行号
的索引首次查询时建立。引擎行号不能直接当成译文表 idx 使用。
"""

import csv
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENES = os.path.join(ROOT, 'data', 'scenes.tsv')
LINES = os.path.join(ROOT, 'data', 'scene_lines.tsv')
MASTER = os.path.join(ROOT, 'data', 'translation.tsv')


class Lookup:
    def __init__(self, scenes_path=SCENES, lines_path=LINES, master_path=MASTER):
        self.scenes_path = scenes_path
        self.lines_path = lines_path
        self.master_path = master_path
        self._offsets = None
        self._scenes_by_entry = None
        self._rows_by_entry = {}

    # ---------------------------------------------------------------- scenes
    def _load_scenes(self):
        if self._scenes_by_entry is not None:
            return
        by = {}
        if os.path.exists(self.scenes_path):
            with open(self.scenes_path, encoding='utf-8') as f:
                for r in csv.DictReader(f, delimiter='\t'):
                    try:
                        eid = int(r['entry'])
                        lo = int(r['idx_from'])
                        hi = int(r['idx_to'])
                    except (KeyError, ValueError):
                        continue
                    by.setdefault(eid, []).append(
                        {'scene': r.get('scene', ''), 'kind': r.get('kind', ''),
                         'lo': lo, 'hi': hi})
        for v in by.values():
            v.sort(key=lambda d: (d['lo'], d['scene']))
        self._scenes_by_entry = by

    def scenes_of(self, entry):
        """→ [(场景号, 类型)]，按行区间排序。"""
        self._load_scenes()
        return [(d['scene'], d['kind']) for d in self._scenes_by_entry.get(entry, [])]

    def scene_covering(self, entry, idx):
        """→ 行区间包含 idx 的场景号（scenes.tsv 的 idx_from..idx_to）。"""
        if entry is None or idx is None:
            return None
        self._load_scenes()
        for d in self._scenes_by_entry.get(entry, []):
            if d['lo'] <= idx <= d['hi']:
                return d['scene']
        return None

    # ----------------------------------------------------------------- rows
    def _load_rows(self, entry):
        if entry in self._rows_by_entry:
            return self._rows_by_entry[entry]
        rows = {}
        if os.path.exists(self.lines_path):
            with open(self.lines_path, encoding='utf-8') as f:
                for r in csv.DictReader(f, delimiter='\t'):
                    if r.get('entry') != str(entry):
                        continue
                    try:
                        rows[int(r['idx'])] = {
                            'rid': r.get('rid', ''),
                            'scene': r.get('scene', ''),
                            'jp': r.get('jp_text', ''),
                            'cn': r.get('translation', ''),
                            'page': r.get('page', ''),
                        }
                    except (KeyError, ValueError):
                        continue
        self._rows_by_entry[entry] = rows
        return rows

    def scene_of(self, entry, idx):
        """Look up a translation-table index (NOT the engine's line index)."""
        if entry is None or idx is None:
            return None
        return self._load_rows(entry).get(idx)

    def at_offset(self, entry, line):
        """Map an engine pool-relative text offset to a translation row.

        Pool-scanned entries store entry-relative offsets in the master TSV;
        table-extracted entries store pool-relative offsets. Short/untranslated
        engine strings may have no master row and must remain unresolved.
        """
        if entry is None or line is None:
            return None
        if self._offsets is None:
            offsets = {}
            if os.path.exists(self.master_path):
                with open(self.master_path, encoding='utf-8', newline='') as f:
                    for fields in csv.reader(f, delimiter='\t'):
                        if len(fields) < 7 or not fields[0].isdigit():
                            continue
                        eid, idx, off = map(int, fields[:3])
                        if len(fields) > 7 and fields[7] == 'pool':
                            off -= 0x7A0
                        offsets[(eid, off)] = idx
            self._offsets = offsets
        return self.scene_of(entry, self._offsets.get((entry, line)))

    def best_scene(self, entry, idx):
        """→ 场景号：优先用行表，其次用 scenes.tsv 的行区间，最后退到该条目第一个场景。"""
        row = self.scene_of(entry, idx)
        if row and row.get('scene'):
            return row['scene']
        s = self.scene_covering(entry, idx)
        if s:
            return s
        ids = self.scenes_of(entry)
        return ids[0][0] if ids else None
