# 侦探神宫寺三郎 — 白影的少女 · GBA 汉化工程

基于 **日文原版 ROM** 的完整汉化工具链与翻译工作区。
（原版：`Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba`，8 MB，8 位游戏码 BT3J）

---

## 一、现在就能做的事

**翻译**：打开 `data/translation.tsv`，填最后一列 `translation`。

```bash
# 1) 查看待翻译内容（示例）
head -20 data/translation.tsv

# 2) 翻译完成后生成中文 ROM
python3 tools/import_script.py \
  --master data/translation.tsv \
  --rom "Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba" \
  --out out.gba
```

如果译文用到了日文字库里没有的字，先做字形补充（见第四节）。

---

## 二、目录结构

```
├── Tantei Jinguuji Saburou ... (Japan).gba   # 原版 ROM（工作基准）
├── data/                                     # ★ 重要产物
│   ├── translation.tsv          ★ 翻译工作台（45488 条）
│   ├── glyph_map.json           ★ 码 → 字 映射（1704 条）
│   ├── entry_catalog.tsv          FAT 条目结构普查（1430 条）
│   ├── free_glyph_slots.json      可复用字形槽（148 个）
│   ├── source_sample.txt          日文原文抽样
│   ├── trans_jp_00..17.txt        字形表转写原始数据
│   └── sheets_jp/                 字形表图片（校对用）
├── docs/                                     # 文档
│   ├── 技术说明.md                ROM 结构 / 码位约定 / 字库格式
│   ├── 汉化方案设计.md            字库扩容方案与容量测算
│   ├── 剧本导出报告.md            导出成果与验证
│   └── archive/                   中间过程文档
├── tools/                                    # 工具链
│   ├── export_script.py           扫描条目 / 导出翻译表
│   ├── import_script.py           译文 → 中文 ROM
│   ├── font_patch.py              字形槽管理 / 新字形生成
│   ├── gbtext.py                  ROM 文本编解码（底层）
│   ├── lz77.py                    LZ77 压缩（腾空间 / 回写）
│   ├── build_jp_map.py            汇总转写 → glyph_map.json
│   ├── render_sheets.py           渲染字形表
│   └── font_sheet.py              字形校验图
├── legacy/                                   # 历史/可选（可删）
│   ├── mgba/                      libmgba 构建树（模拟器验证用，94 MB）
│   ├── shots/ bios/               早期截图与 BIOS
│   └── tools/                     早期字形取证/模拟器脚本
└── work/                                     # 运行时缓存（可删，会自动重建）
```

**可安全删除**：`work/`（缓存）、`legacy/`（历史；删掉后无法再做模拟器级验证）。
`data/` 与 `docs/` 是成果，不要删。

---

## 三、关键结论速查

| 项 | 值 |
|---|---|
| 字形表位置 | ROM 文件 **0x66F440**（FAT 条目 850），1704 字形 |
| 字形格式 | 16×16，4bpp，0x80 字节/字，tile 序 TL,TR,BL,BR |
| 文本编码 | `字符 = MAP[code+1]`（code ≥ 0x20）；`字符 = MAP[code]`（code < 0x20 标点/数字） |
| 剧本规模 | **45488 条字符串**，190 个条目，约 47 万字符 |
| 字库用字 | 剧本实际使用 **1524** 个字形 |
| 可复用空槽 | **148 个** |
| 编解码往返 | **29477 条 0 失败** |
| 汉字排列 | JIS X 0208 一级汉字按读音序子集（有跳字） |

详见 `docs/技术说明.md`。

---

## 四、字形扩容（译文出现新字时）

实测：常用简体字符集里约 **275 个字**日文字库没有，而空槽只有 148 个。

```bash
# 检查某字符集缺多少字
python3 tools/font_patch.py capacity <charset.txt>

# 列出可复用槽位
python3 tools/font_patch.py slots

# 预览某个字的渲染效果
python3 tools/font_patch.py render 汉 /tmp/han.png

# 把新字形写入空槽（可批量）
python3 tools/font_patch.py inject out.gba "汉=0x026" "语=0x02E"
```

**方案**（详见 `docs/汉化方案设计.md`）：
1. **先做槽位复用**（148 个，零风险，不动表结构）
2. 不够时再**整表搬迁扩容**——字形表是 FAT 资源加载，代码里无硬编码地址，
   可搬到 ROM 尾部空区并更新 FAT 850；空间可由 `lz77.py` 重压其他资源腾出
3. 翻译时尽量选用现有字库已有字形，可显著减少新字数量

---

## 五、工具速查

| 命令 | 作用 |
|---|---|
| `python3 tools/export_script.py scan` | 普查全部 FAT 条目结构 |
| `python3 tools/export_script.py extract` | 生成 `data/translation.tsv` |
| `python3 tools/import_script.py --master ... --rom ... --out ...` | 回写生成中文 ROM |
| `python3 tools/import_script.py ... --report-only` | 只报告不写盘（预检译文） |
| `python3 tools/lz77.py selftest` | 压缩器自测 |
| `python3 tools/build_jp_map.py` | 从 `trans_jp_*.txt` 重建映射 |
| `python3 tools/font_patch.py slots` | 列出可复用字形槽 |

---
