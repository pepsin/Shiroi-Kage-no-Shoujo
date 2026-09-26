# 侦探神宫寺三郎 — 白影的少女 · GBA 汉化工程

基于 **日文原版 ROM** 的完整汉化工具链与翻译工作区。
（原版：`Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba`，8 MB，8 位游戏码 BT3J）

---

## 一、现在就能做的事

**生成中文 ROM**（译文已完成，字库已扩容，一条命令）：

```bash
python3 tools/build_rom.py --out out.gba
```

校验：

```bash
python3 tools/verify_rom.py out.gba     # ROM 内文本 ↔ 主表逐条比对
```

当前状态：**100,850 行已译**（`verify_rom.py` 全量往返一致），
标题画面与日文原版**逐像素一致**。

> 译文与流程详见 `docs/翻译作业流程.md`；打包细节（扩表 / 编码约定 / 验证）
> 详见 `docs/打包流程.md`。

**继续翻译/修改译文**：直接改 `data/translation.tsv` 的 `translation` 列，
再跑一次 `build_rom.py` 即可。

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
│   ├── build_jp_map.py            （旧）汇总转写 → glyph_map，已停用
│   ├── render_master.py           全部字形总图（校对用）
│   ├── mapio.py                   glyph_map.csv 读写入口
│   ├── render_sheets.py           渲染字形表
│   └── font_sheet.py              字形校验图
├── legacy/                                   # 模拟器验证链备份（64 KB）
│   ├── gbarun.c                   无头 mGBA harness 源码（需自行编译 libmgba）
│   └── bios/ gba_bios.zip         GBA BIOS
└── work/                                     # 运行时缓存（可删，会自动重建）
```

**可安全删除**：`work/`（缓存）、`legacy/`（验证链备份；删掉后需重新写 harness 才能做模拟器级验证）。
`data/` 与 `docs/` 是成果，不要删。

---

## 三、关键结论速查

| 项 | 值 |
|---|---|
| 日文原版字形表 | ROM 文件 **0x66F440**（FAT 条目 850），1704 字形 |
| 中文版字形表 | **0x800000**，**3201 字形**（原 1704 + 新做 1368 + 备用 129） |
| 字形格式 | 16×16，4bpp，0x80 字节/字，tile 序 TL,TR,BL,BR |
| 文本编码 | `字符 = MAP[code-1]`（code ≥ 0x20）；`字符 = MAP[code]`（code < 0x20 标点/数字）<br>位图实证：`を` 在表下标 0x08B、剧本用码位 0x08C |
| 剧本规模 | **100,850 条字符串**，453 个条目 |
| 已译 | **100,850 行**（全表） |
| 译文用字 | **2381** 个不同汉字；**1369** 个日文字库没有、全部新做（`棚` 重码占 2 槽，故 3072 个有字槽） |
| 空槽 | 129 个（备用） |
| ROM 内往返 | **100,850 / 100,850 完全一致** |
| 汉字排列 | JIS X 0208 一级汉字按读音序子集（有跳字） |

详见 `docs/技术说明.md`、`docs/打包流程.md`。

---

## 四、字库扩容（已实施）

译文需要 2381 个字形，日文表只有 1704 槽，**1369 个要新做**。

**做法**：新字**全部追加在表尾**（不覆盖任何原槽，避免破坏标题画面等资源），
整表放到 **文件 0x800000**，FAT 850 指向新表——**不需要改任何代码**。
ROM 尾部原有 198 KB 空区装不下 ~410 KB 的新表，因此扩了文件；
GBA 头部没有 ROM 尺寸字段，扩文件是安全的。

字库改用点阵字体 **Zpix（最像素）**，11×11 位图直取，不再缩放轮廓字体
（轮廓字体缩到 11×11 会断笔画、粗细不匀）。

新字形用系统字体渲染成游戏原生格式：

```bash
python3 tools/font_patch.py render 汉 /tmp/han.png    # 预览渲染效果
python3 tools/font_patch.py capacity <charset.txt>    # 检查缺字
```

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
