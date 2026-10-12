#!/usr/bin/env python3
"""ds_translate.py - 用 DeepSeek 按「句组」整句重译，产出项目标准补丁。

动机：主表逐显示行存储，游戏把一句话拆成多行；逐行机翻导致同屏语义重复、
语序直译。现在有了 `sent`（句组编号，见 data/scene_lines.tsv 与
docs/技术说明.md 第十二节），可以把整句交给 DS 重译，再把译文分配回各行。

硬规则（与 tm.py verify / apply 同一套，本地校验不过就不进补丁）：
  * 每行码位 ≤ 该行日文行预算（slots_used，控制码算 1）
  * 每组标点（种类/数量）不得超过日文（。/， 同类、可删不可加）
  * 无假名（ー・ 除外）；人名保留日文原写法
  * ``\\xXXXX`` 控制码原样保留；用字必须在字库内（work/glyph_map.ext.csv）

用法：
    python3 tools/ds_translate.py export [--batches N] [--groups-per-batch 50]
    python3 tools/ds_translate.py ask                 # 调 API（读 .env.vars）
    python3 tools/ds_translate.py verify              # 本地校验 + 写补丁
"""
import collections
import csv
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import tm  # noqa: E402

LINES = os.path.join(ROOT, 'data', 'scene_lines.tsv')
SCENES = os.path.join(ROOT, 'data', 'scenes.tsv')
MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
EXTMAP = os.path.join(ROOT, 'work', 'glyph_map.ext.csv')
OUTDIR = os.path.join(ROOT, 'work', 'ds')
PATCH = os.path.join(ROOT, 'work', 'scenes', 'patch_ds_full.tsv')
API_URL = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-chat'

PROMPT_SYS = """你是游戏本地化译审，把日文侦探游戏台词重译成简体中文。风格：冷硬派、简短、口语自然，杜绝翻译腔（不用"关于/进行/有着/的存在/所谓的"，日语被动改中文主动）。"""

PROMPT_RULES = """## 输入
每块是一句被拆成多行显示的日文台词。行格式：`rid<TAB>预算<TAB>日文`。
块末 `机翻初稿:` 是逐行机翻（**语序照搬日文，是错的，仅供参考字面意思**）。

## 头号任务：按中文语序重排
日语谓语在句末、宾语在前，中文相反。**整句先按中文自然语序重排，再把译文分配回各行。**
需要重排的典型（不限于）：
* `…と／すぐに想像できた`（想法内容在前、认知动词在后）→ 中文先把"想到/明白/觉得"提前；
* `…のは。／つい今朝の事だった`（は提示的话题后置）→ 中文按时间/因果正过来；
* `…ために／…へとやって来た`（目的/方位后置）→ 中文先"为了…"再动作。
分行示例（每行 ≤ 预算，标点位置随意落在哪行）：
```
【日文】この突然の訃報が／飛び込んで来たのは。／つい今朝の事だった，
【正确】就在今天早上／这讣告就那么／突然地传了过来。
【日文】俺は不敵に笑っている春菜を見て／よからぬ事を考えているのだろうと／すぐに想像できた，
【正确】看着不怀好意笑着的春菜／我马上就能想到她又在打／什么坏主意，
【日文】疲れている時ほど／ここに戻って来ると落ち着きを／感じる，
【正确】越是疲惫的时候／一回到这里就越能／安心，
```
同一屏里上下行语义重复（把同一个意思说两遍）也是病，整句译就不会有。

## 硬规则（违反即作废）
1. 输出每行格式 `rid<TAB>译文`，只输出这些行，不要别的。
2. 每行译文字数 ≤ 该行预算（`\\xFFC2` 这种控制码算 1 字）。
3. 标点 `、。，！？「」『』・：；“”‘’（）〈〉《》‥…□○■×＋＜＞％` 整块总数不得超过日文原句（可以少，不能多）；`，`/`。` 位置随中文语序自由落。
4. 人名保留日文原字形（神宮寺、洋子、熊野、亜希、哲司、静江、春菜、徳子、貴之、拓朗、悠、礼子、香澄、まなみ→真奈美 等），不简体化。
5. `“““`（省略号标记）按原样保留，数量与日文一致；`\\xXXXX` 控制码原样不动；`』様＋`/`』さん＋` 构件保留。
6. 不要出现任何假名。
7. 叙事视角：神宮寺第一人称"俺"→"我"；洋子叫"洋子"；熊野叫"熊野"。
"""


# ------------------------------------------------------------------ 数据
def load_groups():
    """→ [(scene, [(rid, entry, idx, len, jp, cn, page), ...])]，按场景顺序。"""
    lens = {}
    with open(MASTER, encoding='utf-8', newline='') as f:
        for r in csv.reader(f, delimiter='\t'):
            if len(r) >= 6 and r[0].isdigit():
                lens[(r[0], r[1])] = int(r[3] or 0)
    kinds = {}
    for r in csv.DictReader(open(SCENES, encoding='utf-8'), delimiter='\t'):
        kinds[r['scene']] = r['kind']
    groups = collections.OrderedDict()
    with open(LINES, encoding='utf-8', newline='') as f:
        for r in csv.reader(f, delimiter='\t'):
            if r[0] == 'rid' or len(r) < 10:
                continue
            if kinds.get(r[3]) != 'scene':
                continue
            groups.setdefault(r[9], []).append(
                (r[0], r[1], r[2], lens.get((r[1], r[2]), 99), r[6], r[7], r[8]))
    return [(g[0][0].split(':')[0], g) for g in groups.values() if len(g) >= 2]


def build_batch(groups):
    parts = []
    for _, lines in groups:
        parts.append('```')
        for rid, e, i, ln, jp, cn, pg in lines:
            parts.append(f'{rid}\t{ln}\t{jp}')
        parts.append('机翻初稿: ' + ' / '.join(cn for _, _, _, _, _, cn, _ in lines))
        parts.append('```')
    return PROMPT_RULES + '\n## 待译句组\n\n' + '\n'.join(parts)


def load_key():
    k = os.environ.get('DEEPSEEK_API_KEY')
    if k:
        return k.strip()
    p = os.path.join(ROOT, '.env.vars')
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line.startswith('DEEPSEEK_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None


def call_ds(prompt):
    key = load_key()
    if not key:
        raise SystemExit('缺少 DEEPSEEK_API_KEY（写进 .env.vars）')
    body = json.dumps({
        'model': MODEL,
        'messages': [
            {'role': 'system', 'content': PROMPT_SYS},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.2,
        'max_tokens': 8000,
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
    })
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.loads(r.read())
    return resp['choices'][0]['message']['content']


# ------------------------------------------------------------------ 命令
def cmd_export(a):
    os.makedirs(OUTDIR, exist_ok=True)
    groups = load_groups()
    n = 0
    for b in range(a.batches):
        chunk = groups[b * a.groups_per_batch:(b + 1) * a.groups_per_batch]
        if not chunk:
            break
        txt = build_batch(chunk)
        path = os.path.join(OUTDIR, f'tl_b{b:02d}.md')
        open(path, 'w', encoding='utf-8').write(txt)
        n += len(chunk)
        print(f'wrote {path} ({len(chunk)} 组, {len(txt)} chars)')
    print(f'共 {n} 组 / 总组数 {len(groups)}')


def cmd_ask(a):
    import glob
    import threading
    tasks = []
    for path in sorted(glob.glob(os.path.join(OUTDIR, 'tl_b*.md'))):
        out = path.replace('tl_b', 'tl_resp_b')
        if os.path.exists(out) and not a.force:
            continue
        tasks.append((path, out))
    print(f'{len(tasks)} 个批次待请求')
    lock = threading.Lock()
    done = [0]

    def worker(q):
        while True:
            with lock:
                if not q:
                    return
                path, out = q.pop()
            try:
                text = call_ds(open(path, encoding='utf-8').read())
                with lock:
                    open(out, 'w', encoding='utf-8').write(text)
                    done[0] += 1
                    print(f'[{done[0]}/{len(tasks)}] wrote {out}', flush=True)
            except Exception as ex:
                with lock:
                    tasks_failed.append((path, str(ex)))
                    print(f'FAIL {path}: {ex}', flush=True)
    tasks_failed = []
    threads = [threading.Thread(target=worker, args=(tasks,), daemon=True)
               for _ in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if tasks_failed:
        print(f'{len(tasks_failed)} 个批次失败，重跑 ask 即可续上')


def norm_cn(rid, jp, cn):
    """套用 tm.py 的称呼/书写规范（NORM_RULES + ALWAYS_SUB + NORM_FIXED），
    并把控制码统一成导入器认的 ``\\xXXXX`` 拼写（DS 常抄 JP 源的 [XXXX] 拼写，
    那会被当成 6 个字符挤爆码位预算）。"""
    cn = tm.CTRL_CODE.sub(lambda m: '\\x' + m.group(0)[2:6] if not m.group(0).startswith('\\x') else m.group(0), cn)
    new = tm.NORM_FIXED.get(rid, cn)
    if '貴之君達' in jp:
        new = new.replace('小貴之', '貴之')
    elif '貴之君' in jp and '小貴之' not in new:
        new = new.replace('貴之', '小貴之')
    if '君子' not in new:
        for a_, b_ in tm.ALWAYS_SUB:
            new = new.replace(a_, b_)
    for keys, subs in tm.NORM_RULES:
        if any(k in jp for k in keys):
            for a_, b_ in subs:
                new = new.replace(a_, b_)
    return new


def ext_charset():
    cs = set()
    for r in csv.DictReader(open(EXTMAP, encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            cs.add(ch)
    return cs


def cmd_verify(a):
    import glob
    charset = ext_charset()
    groups = {}
    for _, lines in load_groups():
        for rid, e, i, ln, jp, cn, pg in lines:
            groups[rid] = (e, i, ln, jp)
    sent_of = {}
    for _, lines in load_groups():
        for rid, *_ in lines:
            sent_of[rid] = lines
    proposed = {}
    for path in sorted(glob.glob(os.path.join(OUTDIR, 'tl_resp_b*.md'))):
        for line in open(path, encoding='utf-8'):
            line = line.rstrip('\n')
            if '\t' not in line:
                continue
            rid, cn = line.split('\t', 1)
            proposed[rid.strip()] = cn.strip()
    for rid in list(proposed):
        if rid in groups:
            proposed[rid] = norm_cn(rid, groups[rid][3], proposed[rid])
    print(f'DS 回稿 {len(proposed)} 行')
    ok_rows, rejects = [], []
    group_cn = collections.defaultdict(dict)
    for rid, cn in proposed.items():
        if rid not in groups:
            rejects.append((rid, cn, 'rid 未知'))
            continue
        e, i, ln, jp = groups[rid]
        errs = tm.check_pair(jp, cn, rid)
        cn_plain = tm.CTRL_CODE.sub('', cn)
        bad = [ch for ch in cn_plain if ch not in charset and not ch.isspace()]
        if bad:
            errs.append('字库外字符: ' + ''.join(bad[:6]))
        # 控制码整体保留检查（JP 源 [XXXX] 拼写 vs CN \xXXXX 拼写先归一）
        jp_norm = tm.CTRL_CODE.sub(lambda m: '\\x' + m.group(0)[2:6]
                                   if not m.group(0).startswith('\\x')
                                   else m.group(0), jp)
        for m in tm.CTRL_CODE.findall(jp_norm):
            if m not in cn:
                errs.append(f'控制码丢失 {m}')
        if errs:
            rejects.append((rid, cn, '; '.join(errs)))
        else:
            ok_rows.append((e, i, cn, rid))
            group_cn[tuple(x[0] for x in sent_of[rid])][rid] = cn
    # 句组级标点：只许少不许加
    group_bad = set()
    for gkey, cnmap in group_cn.items():
        pairs = []
        for rid in gkey:
            if rid not in groups:
                continue
            jp = groups[rid][3]
            cn = cnmap.get(rid)
            if cn is None:
                # 组内有行没过单行校验 → 整组不进补丁（避免半组新旧混排）
                pairs = None
                break
            pairs.append((jp, cn))
        if pairs is None:
            group_bad.update(gkey)
            continue
        added, lost = tm.check_group_punct(pairs)
        if added:
            group_bad.update(gkey)
            for rid in gkey:
                rejects.append((rid, cnmap.get(rid, ''), f'组级标点超加 {dict(added)}'))
    final = [(e, i, cn) for e, i, cn, rid in ok_rows if rid not in group_bad]
    os.makedirs(os.path.dirname(PATCH), exist_ok=True)
    with open(PATCH, 'w', encoding='utf-8') as f:
        f.write('# DS 整句重译（句组级，ds_translate.py verify 通过）\n')
        for e, i, cn in sorted(final, key=lambda x: (int(x[0]), int(x[1]))):
            f.write(f'{e}\t{i}\t{cn}\n')
    with open(os.path.join(OUTDIR, 'tl_reject.tsv'), 'w', encoding='utf-8') as f:
        for rid, cn, why in rejects:
            f.write(f'{rid}\t{why}\t{cn}\n')
    print(f'通过 {len(final)} 行 → {PATCH}')
    print(f'拒收 {len(rejects)} 行（含整组连坐 {len(group_bad)} 行）→ work/ds/tl_reject.tsv')


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('export')
    e.add_argument('--batches', type=int, default=2)
    e.add_argument('--groups-per-batch', type=int, default=50)
    e.set_defaults(func=cmd_export)
    k = sub.add_parser('ask')
    k.add_argument('--force', action='store_true')
    k.add_argument('--workers', type=int, default=4)
    k.set_defaults(func=cmd_ask)
    sub.add_parser('verify').set_defaults(func=cmd_verify)
    a = ap.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())
