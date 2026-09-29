
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
python3 tools/romdbg.py where --state work/dbg/xxx.ss1            # 报告存档里正在演哪一行
```

原理与细节见 `docs/调试器.md`。要点：

* 引擎把剧本解压到 EWRAM `0x02030300`，行偏移表在池内（**可能奇数对齐**），
  游标在 IWRAM `0x03007C40` / `0x03007C44`。
* ⚠️ 本工具**没有**「跳到任意一行」的功能，也**不要**再去做「文本级注入」：把目标行
  塞进当前已载入剧本的行表，看着像跳过去了，但背景/在场人物/分支标志都还是当前那一幕的，
  只会掩盖真问题（卡死、花屏、剧情不对都可能被它骗过去）。要看某句话只能在游戏里正常走到。
* 真正的剧本入口是 `loadScript(keyCount,keyA,keyB)`（0x08004EE4），钥匙查
  ROM `file 0x15C004` 的 509 条分发表：`python3 tools/romdbg.py dispatch --entry 500`。
  还缺「槽号 ↔ 场景」的对应关系，见 `docs/调试器.md` 第四节。

注意：文本框一次显示 2 行，**A 的第一下只补完打字机，第二下才翻页**。


## 编译产物命名规则
- 编译的 rom 始终命名为“侦探神宫寺三郎 - 白影的少女 (简中).gba”
