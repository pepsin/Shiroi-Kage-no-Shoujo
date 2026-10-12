#!/usr/bin/env python3
"""llm_width.py - 用外部 LLM（DeepSeek）辅助核定剧本 VM opcode 的操作数宽度。

分工：本地生成「工作包」（handler 反汇编 + 流上下文 + 已知约束），LLM 只做
精读判断；拿回规则后由 tools/script_vm.py 的 walker 本地验证，错了不采用。

用法：
    python3 tools/llm_width.py pkg            # 生成工作包到 work/ds/pkg_*.md
    python3 tools/llm_width.py ask            # 调 API（读 DEEPSEEK_API_KEY 或 work/.deepseek_key）
    python3 tools/llm_width.py apply          # 解析回答 → 打印建议的 OPCODES 行

密钥：环境变量 DEEPSEEK_API_KEY，或文件 work/.deepseek_key（work/ 已被 gitignore）。
"""
import json
import os
import struct
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import script_vm as vm  # noqa: E402

OUTDIR = os.path.join(ROOT, 'work', 'ds')
API_URL = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-chat'

# 待核定的 opcode：未解的 + 前缀推导存疑的 + 跳转语义
TARGET_OPS = [0x40, 0x41, 0x42, 0x43, 0x4A, 0x51, 0x52, 0x53, 0x54, 0x55,
              0x5C, 0x5D, 0x5E, 0x5F, 0x60, 0x61, 0x62, 0x63, 0x64, 0x65,
              0x6B, 0x6C, 0x6F, 0x70, 0x9F]

PREAMBLE = """你在帮我逆向一个 GBA 游戏（ARM Thumb）的剧本字节码 VM。请根据 handler 函数的反汇编，判断该 opcode 从字节流里读几个操作数字节。

## VM 背景

- 字节码流 = opcode 字节 + 操作数。分派器读 1 字节 opcode，调用 handler；handler 用流读取函数读操作数。
- 流读取函数（读到必前进游标）：
  - `bl 0x08018dec` = read_u8（+1 字节）
  - `bl 0x08018e08` = read_u16（+2 字节）
  - `bl 0x08018e74` = read_u24（+3 字节）
  - `bl 0x08018e24` = read_u32（+4 字节）
- 辅助函数（也会读流）：`bl 0x0801c828` = +2 字节；`bl 0x0801c7f0` = +1 字节；`bl 0x0801c870` = 读 u8 得 count，再读 count×2 字节（变长）；`bl 0x0801c8c0` = 不读流。
- 下面其它 `bl 0x........` 调用均为不读流的普通函数。
- handler 可能含条件分支：某些路径多读、某些少读。请给出**各路径的读法**。
- handler 也可能写游标变量（跳转）：`str` 到 0x0200E014（游标）。跳转不影响"操作数宽度"，但请标注「此 op 会跳转」。

## 已核定的样例（校准用）

- op 0x01：`bl 0x08018e08; bl 0x08018dec` → 3 字节（u16 行号 + u8）
- op 0x82：read_u16 + read_u8 → 3 字节
- op 0x50：read_u24 + read_u16 + read_u24 → 8 字节
- op 0x2E：无读取 → 0 字节
- op 0x91：read_u8 ×3 + read_u16 → 5 字节

## 回答格式（严格遵守，每个 op 一行）

`OP<TAB>RULE<TAB>说明`

其中 OP 是十六进制 opcode（如 5A）；RULE 是：
- `w3` 固定 3 字节操作数（0..14）
- `var:描述` 变长（例如 `var:u8 flag 非0再读u16`）
- `modes:00=2,01=8` 按第一个操作数字节（子命令）分派的宽度表

说明栏写一句依据（哪条路径、哪些读取）。不确定就直说，别编。

## 流上下文校验线索

每个 op 后面附上它在真实字节码流里的出现样例（`...` 包围）。流里高频结构：
`01 xx xx xx` = 显示一行（u16 行号 + u8）；`2e`/`2f` = 翻页（0 操作数）；
`82 xx xx xx` = 延时；`45` = 终止。正确的宽度应让相邻这些结构对齐。
"""


def disasm_text(addr, size=0x200):
    lines = []
    for ins in vm.disasm(addr, size):
        lines.append(f'{ins.address:08X}: {ins.mnemonic:9} {ins.op_str}')
        if (ins.mnemonic == 'pop' and 'pc' in ins.op_str) or \
           (ins.mnemonic == 'bx' and ins.op_str == 'lr'):
            break
    return '\n'.join(lines)


def stream_contexts(op, n=3):
    """找该 op 字节在剧情流里的出现样例（含前后文）。"""
    out = []
    for eid, bc, nstr in vm.iter_table_entries():
        start = 0x15
        while True:
            i = bc.find(bytes([op]), start)
            if i < 0:
                break
            ctx = bc[max(0x15, i - 10):i + 18].hex(' ')
            out.append(f'e{eid} @{i:#06x}: ... {ctx} ...')
            start = i + 1
            if len(out) >= n:
                return out
    return out


def build_package(ops):
    parts = [PREAMBLE]
    tab = vm.load_op_table()
    for op in ops:
        haddr = tab.get(op)
        if not haddr:
            continue
        parts.append(f'\n## op 0x{op:02X}  (handler @ 0x{haddr:08X})\n')
        parts.append('```')
        parts.append(disasm_text(haddr))
        parts.append('```')
        ctxs = stream_contexts(op)
        if ctxs:
            parts.append('流内出现样例：')
            parts.append('```')
            parts.extend(ctxs)
            parts.append('```')
    return '\n'.join(parts)


def load_key():
    k = os.environ.get('DEEPSEEK_API_KEY')
    if k:
        return k.strip()
    for p in (os.path.join(ROOT, 'work', '.deepseek_key'),
              os.path.join(ROOT, '.env.vars')):
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line.startswith('DEEPSEEK_API_KEY='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
                if p.endswith('.deepseek_key') and line:
                    return line
    return None


def cmd_pkg(a):
    os.makedirs(OUTDIR, exist_ok=True)
    ops = a.ops or TARGET_OPS
    txt = build_package(ops)
    out = os.path.join(OUTDIR, 'pkg_all.md')
    open(out, 'w', encoding='utf-8').write(txt)
    print(f'wrote {out} ({len(txt)} chars, {len(ops)} ops)')


def cmd_ask(a):
    key = load_key()
    if not key:
        print('缺少密钥：设 DEEPSEEK_API_KEY 或写 work/.deepseek_key')
        return 1
    pkg = os.path.join(OUTDIR, 'pkg_all.md')
    if not os.path.exists(pkg):
        cmd_pkg(a)
    prompt = open(pkg, encoding='utf-8').read()
    body = json.dumps({
        'model': MODEL,
        'messages': [
            {'role': 'system', 'content': '你是严谨的逆向工程师，只输出要求格式的内容。'},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.0,
        'max_tokens': 8000,
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
    })
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    text = resp['choices'][0]['message']['content']
    out = os.path.join(OUTDIR, 'resp_all.txt')
    open(out, 'w', encoding='utf-8').write(text)
    print(f'wrote {out}')
    print(text[:2000])
    return 0


def cmd_apply(a):
    """解析 resp_all.txt → 打印可粘贴进 script_vm.py 的行。"""
    out = os.path.join(OUTDIR, 'resp_all.txt')
    for line in open(out, encoding='utf-8'):
        line = line.strip()
        if not line or '\t' not in line:
            continue
        p = line.split('\t')
        try:
            op = int(p[0], 16)
        except ValueError:
            continue
        rule = p[1].strip()
        print(f'0x{op:02X}: {rule}  # {p[2] if len(p) > 2 else ""}')


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pkg')
    p.add_argument('--ops', type=lambda s: [int(x, 16) for x in s.split(',')],
                   default=None)
    p.set_defaults(func=cmd_pkg)
    sub.add_parser('ask').set_defaults(func=cmd_ask)
    sub.add_parser('apply').set_defaults(func=cmd_apply)
    a = ap.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())
