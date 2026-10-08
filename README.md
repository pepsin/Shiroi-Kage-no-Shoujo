# 侦探神宫寺三郎 — 白影的少女 · GBA 汉化工程

## 提示
本项目为兴趣项目，业余时间开发，本人非专业翻译或者专业游戏开发者，产出项目有不足之处全怪 AI，语句不通顺之处请提交 PR 修改或者在项目 issue 出提出。

## 版权与免责声明

- 本项目是**非营利性的爱好者学习/研究项目**，与原作开发商、发行商及任何权利方均无关联，也未获得其授权或认可。
- 本仓库的**工具链代码与原创文档**以 [MIT License](LICENSE) 发布。
- 《侦探神宫寺三郎 — 白影的少女》游戏本体（包括但不限于剧本原文、程序、美术、音乐）的著作权归原权利方所有。本仓库**不包含、不分发**原版 ROM、GBA BIOS 及任何取自原作的版权数据；生成的中文 ROM 属于构建产物，同样不分发。使用本工具链需**自行合法持有**日文原版 ROM（校验信息见下文「准备工作」）。
- 中文译文是基于原作剧本的衍生翻译，原文部分的著作权归原权利方所有；译文仅供学习交流，**不得用于任何商业用途**。
- 中文字库使用点阵字体 **Zpix（最像素）**，著作权归作者 SolidZORO 所有（[项目地址](https://github.com/SolidZORO/zpix-pixel-font)）。该字体对个人项目免费，商业使用需另行取得作者授权。
- 若任何权利方认为本项目侵犯其合法权益，请通过 issue 联系，本人将及时移除相关内容。

## 准备工作

基于 **日文原版 ROM** 的完整汉化工具链与翻译工作区。
（原版：`Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba`，8 MB，8 位游戏码 BT3J）

**原版 ROM 校验**（合作前请先核对，用错 ROM 全部对不上）：

| 项 | 值 |
|---|---|
| 文件大小 | 8,388,608 字节（8 MB） |
| CRC32 | `3C21D7E0` |
| SHA256 | `bdcea43d1ef5a46336a33b54d9bb2b1a501d033dd04672af7cd6dcfcafd57dc2` |

自检命令（macOS / Linux）：

```bash
shasum -a 256 "Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba"
python3 -c "import zlib;print('%08X'%(zlib.crc32(open('Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba','rb').read())&0xFFFFFFFF))"
```

---

## 一、现在就能做的事

**生成中文 ROM**（译文已完成，字库已扩容，一条命令）：

```bash
make            # 合并 scene_lines 手改 → 重建索引 → 打包 → 全量校验
```

只想要其中一步：

```bash
make rom        # 合并 → 槽位预检 → 索引 → 打包（不校验）
make slots      # 只做槽位预检：译文码位数有没有超过原文（1 秒，出错就先别打包）
make verify     # 只校验现有 ROM（文本往返 / 写入 / 字库 / 引擎关键区）
make save       # 验证存档会不会被游戏判成「无法读取」（make save SAV=x.sav 指定文件）
make help       # 全部目标
```

<details><summary>不用 make 时的等价命令</summary>

```bash
python3 tools/merge_scene_edits.py      # 若在 data/scene_lines.tsv 上改过译文
python3 tools/check_slots.py            # 打包前 1 秒预检：译文装不装得进原槽位
python3 tools/scene_index.py build
python3 tools/build_rom.py --out "侦探神宫寺三郎 - 白影的少女 (简中).gba"
python3 tools/verify_rom.py "侦探神宫寺三郎 - 白影的少女 (简中).gba"
python3 tools/save_check.py --selftest  # 存档格式 / 校验和
```
</details>

> **为什么译文不能比原文长**：打包时译文是**写回原字符串的槽位**的
> （槽位 = 原文码位数 + 1 个结尾 `00`）。超长的那一行会被跳过、ROM 里保留日文原文，
> 于是校验必然失败。`make slots` / `tools/check_slots.py` 会一秒内按行点名，
> 告诉你「e2:145 至少改短 1 个码位」，不用等两分钟的打包。

> **出错即停**：流程里任何一步发现错误都会**立刻停下，绝不带着坏数据往下走**。
> - 第 1 步合并：译文超长 / 含假名 / 标点不符 → 不写主表，直接停；
> - 第 2 步预检：译文装不进原槽位 → 不打包，现有 ROM 不动；
> - 打包中任何一步（回写剧本 / 菜单 / 标题 / 字库）失败 → 停在那一
>   步，后面的步骤一步都不跑；自检发现问题则报出 ✗ 清单并以非 0 退出。
>
> 所以「`make` 通过了」= 合并、预检、打包、六项自检全过。

> **存档与汉化互不影响**：游戏判断「存档损坏」只看存档自己的魔数 `0x0DECADE0`
> 加加法校验和，**不读 ROM**；存档里也不含字形码位文本。汉化要守的是 ROM 侧两条：
> 字符串槽位不许移位、剧本分发表（`file 0x15C004`，在 e0 数据里）不许被覆盖——
> 两条都在打包自检里（`verify_writes` 的写入范围、`verify_rom` 的「引擎关键区」）。
> 格式与「损坏」判定链见 `docs/存档格式.md`；检查存档用 `make save`。

当前状态：**101,795 行已译**（`verify_rom.py` 全量往返一致），
并按剧情把全表切成 **795 个场景**（`data/scenes.tsv`，见 `docs/翻译作业流程.md` 第六节），
标题画面与日文原版**逐像素一致**。

> 译文与流程详见 `docs/翻译作业流程.md`；打包细节（扩表 / 编码约定 / 验证）
> 详见 `docs/打包流程.md`。

**继续翻译/修改译文**：推荐改 `data/scene_lines.tsv`（按剧情顺序排好，`make` 会
合并回主表并顺手做 `translation_rules.md` 的硬性规则校验，不合格就停下不写）；
也可以直接改 `data/translation.tsv` 的 `translation` 列，再跑一次 `build_rom.py`
（这条路只有槽位预检，超长会被拦住）。

---

## 二、目录结构

```
├── Tantei Jinguuji Saburou ... (Japan).gba   # 原版 ROM（工作基准）
├── data/                                     # ★ 重要产物
│   ├── translation.tsv          ★ 翻译工作台（101,795 条显示行）
│   ├── scenes.tsv / scene_lines.tsv  场景推进索引（795 个场景；每行 → 场景/位置/跨行标志）
│   ├── glyph_map.json           ★ 码 → 字 映射（1704 条）
│   ├── entry_catalog.tsv          FAT 条目结构普查（1430 条）
│   ├── free_glyph_slots.json      可复用字形槽（148 个）
│   ├── source_sample.txt          日文原文抽样
│   ├── trans_jp_00..17.txt        字形表转写原始数据
│   └── sheets_jp/                 字形表图片（校对用）
├── docs/                                     # 文档
│   ├── 技术说明.md                ROM 结构 / 码位约定 / 字库格式
│   ├── 调试器.md                  ★ 剧情跳转调试器（romdbg）原理与用法
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
├── legacy/                                   # 无头 mGBA harness 源码（需自行编译 libmgba）
│   ├── gbarun_dbg.c               ★ 调试套件：逐帧 poke/read/watch/search + 截图
│   ├── gbarun.c  gbarun_state.c  gbarun_shot.c  autoplay.c
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
| 剧本规模 | **101,795 条显示行**，455 个条目 / **795 个场景** |
| 已译 | **101,795 行**（全表） |
| 译文用字 | **2381** 个不同汉字；**1369** 个日文字库没有、全部新做（`棚` 重码占 2 槽，故 3072 个有字槽） |
| 空槽 | 129 个（备用） |
| ROM 内往返 | **101,795 / 101,795 完全一致** |
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
| `python3 tools/scene_index.py build --report` | 重建场景索引 `data/scenes.tsv` / `scene_lines.tsv` |
| `python3 tools/scene_index.py show 3.0` | 按剧情顺序读一个场景（日文 ｜ 现译） |
| `make` / `make help` | 一条命令跑完整流程；目标清单见 `make help` |
| `python3 tools/merge_scene_edits.py` | 把 `scene_lines.tsv` 上的手改译文合并回主表（打包前必做） |
| `python3 tools/romdbg.py list --entry 500` | 按引擎播放顺序列对白行（实机文本） |
| `python3 tools/romdbg.py play --state x.ss1 --keys "A@120:4"` | 无头复跑 + 逐步截图 |
| `python3 tools/romdbg.py where --state x.ss1` | 报告存档里正在演哪一行 |
| `python3 tools/export_scene_context.py --entries 1-20 --out work/scenes/e1_20.tsv` | 导出整场景复核对 |
| `python3 tools/import_script.py --master ... --rom ... --out ...` | 回写生成中文 ROM |
| `python3 tools/import_script.py ... --report-only` | 只报告不写盘（预检译文） |
| `python3 tools/lz77.py selftest` | 压缩器自测 |
| `python3 tools/build_jp_map.py` | 从 `trans_jp_*.txt` 重建映射 |
| `python3 tools/font_patch.py slots` | 列出可复用字形槽 |

---
