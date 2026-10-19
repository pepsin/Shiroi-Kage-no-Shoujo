# PSP / gpSP 兼容性验证（2026-10-15）

## 结论与适用范围

已在桌面 libretro gpSP 核心复现并修复一个 **ROM 容量导致的 EEPROM 写档问题**。
11,753,808 字节的汉化版能启动、能进入剧情，也能读取事先有效的存档，但在本次
正常按键保存路线中未写出有效存档。仅在文件尾补 `0xFF` 到 16 MiB 后，保存与
冷启动读档通过。

后续还修好了本机 PPSSPP 的缺失截图目录与空白菜单问题，并通过 **PSP 版
gpSP-J 110507** 实际启动三份对照 ROM，均进入开场剧情。在这个 PSP 核心中，
旧汉化版和 16 MiB 版均通过游戏内保存、正常退出和全新 PPSSPP 进程读档；
16 MiB 版也能直接读取旧版生成的存档。本次没有发现两版的存读档结果差异，
没有复现桌面 libretro gpSP 的旧版写档故障。该嵌套测试仍不能替代 PSP 实机验证。

反馈目前只有“无法读取”，没有 PSP 型号、模拟器版本、报错画面或发生步骤。
因此不能断言用户遇到的就是这个问题，也没有证据证明所有 PSP mGBA/gpSP 版本
已兼容。特别是“文件列表无法打开 ROM”和“游戏不能读写存档”是两种不同故障。

## 实测环境与结果

- macOS arm64，mGBA 0.10.5，项目原有无头 harness。
- [libretro/gpSP](https://github.com/libretro/gpsp) 提交
  `5819380c2ffb0900219d700a382ee68c464ebb99`，解释器模式，内置开源替代 BIOS，
  关闭跳帧和动态重编译。不是 PSP MIPS 动态重编译器。
- 各次运行均用独立进程、新建证据目录与空 EEPROM；没有加载玩家 `.sav`、即时存档
  或注入游戏内存。读档轮次仅加载前一轮正常游戏操作写出的 EEPROM。

| 对照 | 启动、开场文字 | 正常保存后的 EEPROM | 冷启动读取有效存档 |
|---|---|---|---|
| 日文原版，8 MiB | 正常 | 槽 0 有效 | 正常回到 e1 葬礼调查 |
| 原汉化版，11,753,808 字节 | 正常 | 无有效槽位 | 能读取外部提供的有效槽 0 |
| 同一汉化版，仅补齐 16 MiB | 正常 | 槽 0 有效 | 能读取自己刚保存的槽 0，回到 e1 |
| 原汉化版＋临时修正 gpSP 映射边界 | 正常 | 槽 0 有效 | 本项用于映射因果对照，未另做冷启动读档 |

保存输入来自 `tests/dyn/checks.json` 中的 `cn-funeral-to-reception`（17,608 帧）。
跨核心时固定按键时序会有差异：gpSP 不一定落到该路线名称中的“应接室”。这里的
通过条件是 **存档头校验、槽 0 校验及有存档标志有效，随后新进程读档回到 e1**，
不是沿用 mGBA 的终点断言，也不把进程正常退出当作游戏验证成功。

修复后的 mGBA 5 条动态路线全部通过，包括 77,253 帧的完整书斋调查路线；所有
证据哈希与现有基线一致，无需更新基线。`make rom`、`make verify` 和 81 项
`make test-unit` 均通过。这次没有重新跑完整通关路线，不沿用旧 ROM 哈希的通关结论。

## gpSP 中的原因

[该版本的 `map_rom_entry`](https://github.com/libretro/gpsp/blob/5819380c2ffb0900219d700a382ee68c464ebb99/gba_memory.c#L2431)
按 32 KiB 页镜像 ROM。旧循环只限制镜像起点 `mcount`，没有限制
`mcount + idx`，从而可能把本应留给 EEPROM 的地址也映射成 ROM。

当前未补齐 ROM 向上取整为 **359 页**。在 `0x0C000000` 窗口，
`mcount=359`、`idx=153` 的和为 512，映射落到 **`0x0D000000`**，
这个地址用于本游戏的 EEPROM。实测原汉化版加载后该页有直接 ROM 映射，
而原版 8 MiB 和补齐 16 MiB 的版本都没有。

因果对照只在 `work/compat/gpsp/` 下载的第三方源码中临时修改循环上界：

```c
// 两个循环分别从 mcount < 1024、mcount < 512 改为：
mcount + idx < 1024
mcount + idx < 512
```

重新编译后，未经补齐的 ROM 也能保存；它在 17,608 帧路线采集的所有截图、
EWRAM、IWRAM、最终 EEPROM，与“未修改 gpSP＋16 MiB ROM”**逐字节相同**。
这项修改只用于定位，不属于本仓库发布补丁。玩家无需替换模拟器核心即可尝试
新的 16 MiB 构建。

## 构建修正

`tools/build_rom.py` 在全部资源与字形写入完成后调用 `tools/rom_size.py`，
追加 5,023,408 字节 `0xFF`。不修改之前的任一字节，不替换资源后的零 guard。
`verify_rom.py` 拒绝非 16 MiB 的发布产物，超过 16 MiB 的输入则中止构建，
防止截断或未经验证地改为 32 MiB 卡带地址布局。

| 产物 | SHA-256 |
|---|---|
| 修复前 11,753,808 字节 | `bc1d21666c4c120fd081a2ab072702682f91fd64cf182ca0797fa142c2ba1beb` |
| 修复后 16,777,216 字节 | `be05afcab46d58b51642474c98ac3d933d03a3243db9829865bf7cdf1efca79d` |

完整重新构建与直接补齐旧文件得到的结果相同。根目录中文 ROM 已更新，玩家存档未改动。

## 重复运行 gpSP 保存／读档检查

第三方核心、BIOS、ROM、截图均不入库。依赖本地 C 编译器及 gpSP 源码中的
`libretro.h`；本仓库保存最小前端和按键驱动器。

```bash
git clone https://github.com/libretro/gpsp.git work/compat/gpsp
git -C work/compat/gpsp checkout 5819380c2ffb0900219d700a382ee68c464ebb99
make -C work/compat/gpsp -j4 platform=osx HAVE_DYNAREC=0 CC=clang CXX=clang++
mkdir -p work/bin
clang -O2 -Iwork/compat/gpsp/libretro/libretro-common/include \
    legacy/libretro_probe.c -o work/bin/libretro_probe
python3 tools/check_gpsp.py --core work/compat/gpsp/gpsp_libretro.dylib
```

应使用**未修改映射代码**的核心验收 ROM 修复。此次定位时修改过的核心位于
`work/compat/gpsp/`；原始核心副本为 `work/compat/gpsp-original.dylib`。
`--rom` 可指定对照 ROM。每次证据保存在新建的
`work/compat/gpsp-check/run-*/`，包含按帧输入、日志、截图、RAM、8 KiB EEPROM
和带 ROM/核心哈希的 `summary.json`。保存失败返回非零，不继续假报读档成功。

## PPSSPP 初次尝试及后续修正

实际下载并运行了 [PPSSPP 1.20.4 官方 macOS 版](https://www.ppsspp.org/download/)，
尝试嵌套运行作者发布的
[TempGBA4PSP-mod 20160527](https://github.com/phoe-nix/TempGBA4PSP-mod/releases/tag/20160527)
和 [Single-game v2.0](https://github.com/GrabowskiDev/TempGBA4PSP-Single-game/releases/tag/v2.0)。
使用开源替代 BIOS；压缩包中的其他 BIOS 未解压使用。
这是初次测试的条件；后续 gpSP-J 的 BIOS 对照见下文。

旧程序在长宿主路径下出现目录解析错误；改为 `/private/tmp/shiroi-psp/tg/`
短路径后，直接启动版在日文原版对照上仍退回 PPSSPP 主菜单。日志含
`Unknown syscall`、未实现的 `scePowerLock`/`sceImposeSetHomePopup` 等。
这些是嵌套测试环境的限制线索，不能把未进入游戏直接归因于汉化 ROM，
也没有把此次尝试记为 PSP 兼容通过。

尚需用户反馈确认：PSP 型号、模拟器具体版本、原版能否运行，以及“无法读取”
发生于 ROM 文件选择还是游戏内读档。旧版 PSP 文件列表还应单独尝试短英文文件名
和未压缩 `.gba`；这只是排查方法，本次未证明中文文件名是故障原因。
生成的 ROM 仍按项目规则使用中文文件名。

本地初次对照图：`work/compat/gpsp-comparison.png`、`save-comparison.png`、
`load-result.png`；详细日志、PPSSPP 测试包及核心编译记录也保留在 `work/compat/`。

### 目录与图形修复

用户实际操作后，TempGBA 明确显示
`ERROR [snapshot_directory] Specified directory is invalid.`，回退目录为 `umd0:/`。
核对配置与 `load_dir_cfg()` 后，定位到 `dir.ini` 指定的 `ms0:/PICTURE` 不存在。
这里的 snapshot 是**截图目录**，与 `save_state_directory`（即时存档目录）不同。

已创建 `/Users/xiechunlin/.config/ppsspp/PICTURE`，无需修改目录权限。重新启动后该
报错消失。还将测试包独立复制到 PPSSPP 的标准记忆棒路径
`~/.config/ppsspp/PSP/GAME/TempGBA`；此时仍有空白菜单，说明目录问题与图形显示
问题需要分别处理，不能仅用早先的系统调用日志解释全部现象。

按用户提供的 [Brewology 下载页](https://psp.brewology.com/downloads/download.php?id=12859&mcid=1)，
增加 **gpSP-J 110507 signed by flofrucht** 对照，独立安装到
`~/.config/ppsspp/PSP/GAME/gpSP-J`。RAR SHA-256：
`94a3c896594e3a342587e2323c44be6ea6c5a3c794fbcd192bd9cf9289fa169b`。

这个版本拒绝前述开源替代 BIOS，显示“BIOS 文件有问题，请重新导出”，尚未读取
游戏 ROM。换用此前已下载的 TempGBA 发行包内 16 KiB BIOS 后通过启动检查；
BIOS SHA-256 为 `fd2547724b505f487e6dcb29ec2ecff3af35a841a77ab2e85fd87350abd36570`，
原开源 BIOS 在测试目录备份为 `gba_bios.open-source.bin`。两者均不入库或发布。

gpSP-J 同样出现空白菜单，但在 PPSSPP 启用 `SoftwareRenderer = True` 后，目录
及 `CN-16M.GBA`、`CN-RAW.GBA`、`JP.GBA` 文件列表正常显示；后续启动不再依赖
临时 `--appendconfig` 参数，确认设置已保存。这是本机 PPSSPP 图形渲染的对照结果，
不能外推为 PSP 实机也需要同样设置。

新增测试文件和启动日志在 `work/compat/gpsp-j-110507/`；`setup.json`、
`bios-comparison.json` 记录来源、文件哈希及 BIOS 对照。三个英文 ROM 文件名只是
隔离测试用的副本名，根目录发布产物仍按项目规则使用中文文件名。

### gpSP-J 嵌套启动结果

修正上述环境后，在 PPSSPP 内正常按键选择 ROM、进入标题、开始新游戏，实测：

| 测试副本 | 容量 | 标题菜单与开场剧情 |
|---|---|---|
| `jp.gba` | 8 MiB | 正常，显示日文对白 |
| `cn-raw.gba` | 11,753,808 字节 | 正常，显示中文对白，确认键可推进 |
| `cn-16m.gba` | 16 MiB | 正常，显示中文对白 |

三份 ROM 哈希与本文前述原版/修复前/修复后产物一致，见
`work/compat/gpsp-j-110507/boot-results.json`。当前键位为 `X` 确认、`Z` 返回、
空格 Start、方向键移动；`S` 对应 PSP 三角键，用于打开 gpSP-J 菜单。

启动测试之后的保存—重启—读档结果见下节。仍未做完整通关；三份测试副本使用
短英文名，未验证 PSP 文件列表对中文文件名的支持情况。

### gpSP-J 游戏内存档与冷启动读档

使用相同的 PPSSPP、gpSP-J 和 BIOS，分别从无对应 `.sav` 的状态开始新游戏，
正常按键走完葬礼开场、推理 NO.1，进入游戏内保存菜单，将进度存入 FILE 01。
没有使用任何即时存档、内存修改或存档校验修复。两份 ROM 逐字节比较确认：
差别只有末尾追加的 5,023,408 字节 `0xFF`。

gpSP-J 的默认设置显示 `バックアップの更新: 終了時のみ`（仅退出时更新备份）。
实测游戏内保存后，磁盘目录暂时没有对应 `.sav`；经 `S` 打开模拟器菜单并执行
`gpSPの終了` 后，才写出 `save/CN-RAW.sav` 或 `save/CN-16M.sav`。
随后退出整个 PPSSPP，再启动新进程，通过标题页“继续游戏”读取 FILE 01。
因此按此默认设置测试或游玩时，应通过 gpSP-J 自己的退出菜单完成写回。

| 对照 | 正常退出后的文件 | 头与槽 0 校验 | 全新 PPSSPP 进程读档 |
|---|---|---|---|
| 旧汉化版，11,753,808 字节 | 8,192 字节；游戏时间 2 分 | 均有效，槽 0 标志存在 | 成功回到 3/11 昼、告别式会场调查菜单 |
| 16 MiB 版 | 8,192 字节；游戏时间 6 分 | 均有效，槽 0 标志存在 | 成功回到同一调查场景 |
| 16 MiB 版读取旧版新建 `.sav` 副本 | 原样复制，未转换或重签 | 沿用旧版有效内容 | 成功识别 2 分进度并回到调查场景，无损坏提示 |

两次人工操作耗时及推理过程有差异，不以存档文件哈希相同为通过条件。实际文件
仅有 7 个字节不同，格式均为本作的 8 KiB EEPROM；游戏内读取和独立校验均通过。
跨版本读取后正常退出，旧版存档副本的内容仍逐字节不变。

证据目录：`work/compat/gpsp-j-110507/save-roundtrip/`。`summary.json` 记录 ROM、
BIOS、EBOOT 和存档哈希、检验值及可见游戏结果；`save-check.txt` 为存档检查输出，
其他日志对应独立启动轮次。主要存档副本：

- `cn-raw-created.sav`：SHA-256 `e60a8e7cb2b0e98a7afcd6cfaeecce917dc1cec80664614b6f5dbee2697050d3`。
- `cn-16m-created.sav`：SHA-256 `335c1bde5cf343c0aaae9fd8d0dd90925eb340144cb658ee812bb2ecbf235aee`。

测试结束后已恢复跨版本测试前的 `CN-16M.sav`，保留两版各自创建的测试进度。
根目录及 `work/campus-save/` 中此前登记的六份玩家存档均未改变。

本结论只覆盖 **PPSSPP 1.20.4 内的 gpSP-J 110507、开场首次保存点**。不能把桌面
libretro gpSP 的映射缺陷直接归因到该 PSP 核心，也不能据此解释所有 PSP 型号、
其他 gpSP/mGBA 版本或玩家未提供的损坏存档。16 MiB 补齐仍保留其已验证的桌面
gpSP 兼容作用；在本次 gpSP-J 对照中，两版均可保存和读取。
