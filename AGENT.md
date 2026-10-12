
## 翻译原则
- 不动任何标点符号，保持原样
- 尽可能使用原有文本字数
- 翻译目标是日语至简体中文
- 另外人名不需要翻译，保留原文
- 本游戏可能有同一段文字有不同实例的情况，所以同一段内容已翻译并不代表该段内容在游戏中出现的所有情况都有对应翻译

## 测试

可以使用 mgba 进行游戏的测试，这个游戏通过 start，select，a，b，上下左右，LR 可以进入不同的菜单和剧情。
你可以通过 mgba 具体的配置文件模拟操作这个游戏来进行更好的 debug

### 剧本调试器（romdbg）

```bash
python3 tools/romdbg.py list --entry 500 --start 195 --count 16   # 按引擎播放顺序列出对白行
python3 tools/romdbg.py find --text 病死的                        # 反查在哪一条目/第几行
python3 tools/romdbg.py play --state work/dbg/xxx.ss1 --frames 1500 \
        --keys "A@120:4 A@240:4" --shot-every 60                   # 无头复跑 + 逐步截图
python3 tools/romdbg.py where --state work/dbg/xxx.ss1            # 报告存档里的条目/场景/行
python3 tools/state_inspect.py entry work/dbg/xxx.ss1             # 只报条目号/场景号（不用跑模拟器）
```

原理与细节见 `docs/调试器.md`。要点：

* 引擎把剧本解压到 EWRAM `0x02030300`，行偏移表在池内（**可能奇数对齐**），
  游标在 IWRAM `0x03007C40` / `0x03007C44`。
* 「现在演哪个条目」由 `tools/entry_id.py` 反查：描述符 `0x02003F24` 的
  keyB(`+0x10`)/keyCount(`+0x14`) 当钥匙筛 ROM 分发表候选，再比结构区
  `[0x000,0x7A0)`。`where` 现在直接报 `e438 / 场景 438.0 / 438:0`。
* ⚠️ 本工具**没有**「跳到任意一行」的功能，也**不要**再去做「文本级注入」：把目标行
  塞进当前已载入剧本的行表，看着像跳过去了，但背景/在场人物/分支标志都还是当前那一幕的，
  只会掩盖真问题（卡死、花屏、剧情不对都可能被它骗过去）。要看某句话只能在游戏里正常走到。
* 真正的剧本入口是 `loadScript(keyCount,keyA,keyB)`（0x08004EE4），钥匙查
  ROM `file 0x15C004` 的 509 条分发表：`python3 tools/romdbg.py dispatch --entry 500`。
  还缺「槽号 ↔ 场景」的对应关系，见 `docs/调试器.md` 第四节。

注意：文本框一次显示 2 行，**A 的第一下只补完打字机，第二下才翻页**。

### 动态回归（改了译文/字库/打包后跑）

```bash
make check-dyn        # 追加条目静态检查 + 冷启动复跑固定路线，比对不变量与证据哈希
make test-unit        # 测试工具自身回归，无需 ROM 或模拟器
make test             # 单测 + 静态/索引/存档检查 + 动态路线
make autoplay         # 独立完整游玩回放，正常按键输入，无需 AI；逐段严格校验
make dyn-list         # 列出检查规格；make dyn-update 刷新基线
```

规格 `tests/dyn/checks.json`、基线 `tests/dyn/baseline.json` 都入库；证据落在
`work/dyn/`。不变量不成立 → FAIL；只改了译文导致哈希漂移 → `PASS*`（加 `--strict` 才算失败）。
模拟器是确定性的（同 ROM + 同按键 → EWRAM/IWRAM/VRAM 逐字节一致），所以路线用
「冷启动 + 按键脚本」表达，不需要提交 `.ssN`。设计与加检查的步骤见 `docs/动态测试.md`。
`--strict` 将缺失基线/规格变化也视为失败；`--update` 仅在全部选中用例通过后写入，
局部刷新保留其他用例。不得用刷新基线来掩盖执行或不变量失败。


## 编译产物命名规则
- 编译的 rom 始终命名为“侦探神宫寺三郎 - 白影的少女 (简中).gba”
