# 独立自动游玩

`route.json` 记录从冷启动开始的正常手柄输入和分段证据，不含 ROM、内存写入或即时存档。
执行器仅需要 Python 3、项目 ROM 和 libmgba 调试 harness，不调用 AI、浏览器或网络服务。

当前完整路线覆盖 3 月 11 日至 3 月 17 日主线、最终战斗和问答、结局与制作人员名单，
终点为通关密码 **NSYS** 画面。共 124 段、1,489,738 帧，包含探索时的尝试与返回。
它是固定版本的确定性通关回放，不是任意存档的通用解谜器。
已在 mGBA 0.10.5 上独立冷启动验收：124 段全部匹配，结果 `CLEAR`；
ROM、路线与终点画面的哈希见 [verification.json](verification.json)。

运行前需准备上述中文 ROM，以及 libmgba 的头文件和库。`make autoplay` 会按需
编译 `work/bin/gbarun_dbg`；默认库路径为 `/opt/homebrew`，其他安装位置使用
`make autoplay MGBA_HOME=/实际安装前缀`。运行时不依赖翻译表、字形识别或探索脚本。

```bash
make autoplay
python3 tools/autoplay.py --out work/my-playthrough
python3 tools/autoplay.py --out work/my-playthrough --resume
```

默认每次创建新的 `work/autoplay/<时间>-<运行号>/` 目录。指定目录已有记录时，
执行器要求明确续跑，避免覆盖已有验收证据。恢复时校验路线、ROM 和断点的 SHA256。

## 选项截图审核

`make autoplay` 默认保存本次路线实际遇到的对话选项、推理选项（含滚动后出现的新页）、
行动菜单、物品选择和地图选择。打开本次输出目录中的 `choice-review/index.html`，
可按类型筛选、放大查看；原始 240×160 PNG 不经重绘，索引列出脚本条目、分段和帧号
（帧号从 0 开始）。
`index.json` 还记录重复出现的位置、图片 SHA256 和运行状态。未完成的运行显示
`IN_PROGRESS` 或 `PARTIAL`，不会被标成已通关。

```bash
make autoplay AUTOPLAY_ARGS='--out work/option-review'
# 审核页：work/option-review/choice-review/index.html
# 只跑验证、关闭截图：
make autoplay AUTOPLAY_ARGS='--no-choice-capture'
```

截图仅使用模拟器已经渲染的画面：在下一次按键前一帧及分段末尾读取 UI／剧本状态，
识别普通选项和推理列表，不注入按键、改 RAM、暂停或增加游戏帧数。相同脚本内，
只改变高亮的文字选项合并为一张；滚动后的不同内容、不同脚本实例分别保留。
它覆盖当前路线实际显示过的页面，不代表已经遍历所有支线或未滚动到的选项。

整个 `choice-review/` 目录均由 `.gitignore` 排除，自定义 `--out` 时也适用；
截图和审核索引不提交。原始采集快照在每段处理后清理。审核不需要 Pillow、OCR 或 AI。
续跑必须保持原来的截图设置，并校验已保存图片；旧版无截图的运行请使用
`--resume --no-choice-capture`，如需完整截图则在新目录从冷启动重跑。

## 通关验证

每段结束核对 EWRAM、IWRAM、VRAM 和画面，偏离即失败；保留日志、截图和最后通过的断点。
`result.json` 中只有 `CLEAR` 才代表该完整路线到达经观察确认的结局画面。
开发中的路线标为 `partial`，默认拒绝作为通关路线运行；显式加 `--allow-partial`
才可检查已有前缀，结果为 `PARTIAL`，退出码 3。失败退出码 1，通关退出码 0。

路线绑定一份确切 ROM。修改译文、字库或打包后，旧证据不能直接证明新版本通关。
需核对新版本实际玩法、重新取得证据，再从冷启动回放验收。

译文或图片修改后，使用同一输入序列验证新 ROM：

```bash
python3 tools/revalidate_autoplay.py --out work/revalidate-new \
  --candidate work/route-new.json
make autoplay AUTOPLAY_ARGS='--route work/route-new.json --out work/replay-new'
```

第一步必须从冷启动跑完并匹配原通关画面，才生成候选；新目录和新候选文件均不得
覆盖既有证据，失败不更新路线。第一步结果为 `CANDIDATE_REQUIRES_REPLAY`。
第二步仍由原执行器逐段严格验证全部 RAM 和画面，只有第二次运行得到 `CLEAR`，
才把候选替换为 `tests/autoplay/route.json` 并更新 `verification.json`。
若终点画面本身有意修改，不能通过刷新旧终点哈希绕过失败；应正常游玩、观察新终点，
按下述记录流程重新导出和独立验收。

## 记录与更新

探索时每次实际输入记为 `route.jsonl`，包含连续 step、frames、keys，
并保存对应 `state-NNNN.ss1` 和含 `rom_sha256` 的 `session.json`。
`tools/export_autoplay.py` 将这些记录合并为最多 100000 帧、4096 个按键事件以内的分段，
从实际存档取出 RAM 和画面哈希。导出默认仍是 partial；只有已实际观察到结局时才使用
`--ending` 注明所见结局，之后必须用独立执行器再验证一次完整冷启动回放。

```bash
python3 tools/export_autoplay.py work/playthrough-session tests/autoplay/route.json
```

导出工具需要 Pillow；独立回放执行器和选项截图都不需要 Pillow。字形识别、攻略解析和探索控制器
都不参与最终固定路线回放。
