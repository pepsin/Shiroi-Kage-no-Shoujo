# ============================================================================
#  侦探神宫寺三郎 — 白影的少女 · GBA 汉化工程
#
#  一条 `make` 走完全流程：
#      scene_lines 手改译文 → 合并回主表 → 重建场景索引 → 打包 → 全量校验
#
#  常用：
#      make                 全流程（等于 make all）
#      make rom             合并→索引→打包（不校验）
#      make verify          只校验现有 ROM
#      make scenes          列场景        make show S=3.0     读一个场景
#      make jump E=500 I=204  生成可在 mGBA 载入的跳转存档
#      make help            全部目标
#
#  注意：打包只读 data/translation.tsv；data/scene_lines.tsv 是派生文件，
#        所以 `make` 会先把你写在 scene_lines 上的改动合并回主表（幂等，无改动就跳过）。
# ============================================================================

PY        ?= python3
MGBA_HOME ?= /opt/homebrew
MAKE      ?= make

ROM       := 侦探神宫寺三郎 - 白影的少女 (简中).gba
JP_ROM    := Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba
MASTER    := data/translation.tsv
LINES     := data/scene_lines.tsv
DBG       := work/bin/gbarun_dbg

# 传给少数目标的可选参数，例如: make lint LINT_ARGS="--entries 1-56 --lines"
LINT_ARGS ?=
TOP       ?= 20
E         ?=
I         ?=
S         ?=
T         ?=
STATE     ?=
KEYS      ?=
FRAMES    ?= 1200

.DEFAULT_GOAL := all

# ---------------------------------------------------------------- 主流程
.PHONY: all merge index rom verify

# 校验步骤复用同一段配方：`make` 跑完打包后校验，`make verify` 单独跑
define run_verify
	@echo "== 全量校验 $(ROM)"
	@$(PY) tools/verify_rom.py "$(ROM)"
	@echo
	@$(PY) tools/verify_glyphs.py "$(ROM)"
endef

all: rom                                     ## 全流程：合并 → 索引 → 打包 → 校验
	$(run_verify)
	@echo "== 完成：$(ROM)"

verify:                                      ## 只校验现有 ROM（不重打包）
	$(run_verify)

rom: index                                   ## 合并→索引→打包（build_rom 内含自检）
	@echo "== [3/4] 打包 $(ROM)"
	@$(PY) tools/build_rom.py --out "$(ROM)"

index: merge                                 ## 重建 data/scenes.tsv / scene_lines.tsv
	@echo "== [2/4] 重建场景索引"
	@$(PY) tools/scene_index.py build

merge:                                       ## 把 scene_lines 上的手改译文合并回主表
	@echo "== [1/4] 合并 scene_lines → $(MASTER)"
	@$(PY) tools/merge_scene_edits.py
	@$(PY) tools/merge_scene_edits.py --check >/dev/null 2>&1 \
		&& echo "   两边一致" || { echo "!! 合并后仍不一致，见上面输出"; exit 1; }

# ---------------------------------------------------------------- 检查
.PHONY: check lint qa scenes show entries find
check:                                       ## 索引完整性（覆盖 / 顺序）
	@$(PY) tools/scene_index.py check

lint:                                        ## 文风 lint（可用 LINT_ARGS= 传参）
	@$(PY) tools/style_lint.py --top $(TOP) $(LINT_ARGS)

qa:                                          ## 译文 QA 汇总
	@$(PY) tools/qa_translation.py --summary

scenes:                                      ## 场景清单（SCENE_KIND=scene 可过滤）
	@$(PY) tools/scene_index.py list $(if $(SCENE_KIND),--kind $(SCENE_KIND),)

show:                                        ## 读一个场景：make show S=3.0
	@test -n "$(S)" || { echo '用法: make show S=3.0'; exit 2; }
	@$(PY) tools/scene_index.py show $(S)

entries:                                     ## 列某条目的对白行：make entries E=500 [I=204]
	@test -n "$(E)" || { echo '用法: make entries E=500 [I=204]'; exit 2; }
	@$(PY) tools/romdbg.py list --entry $(E) \
		$(if $(I),--start $(I) --count 20,)

find:                                        ## 反查译文：make find T=病死的
	@test -n "$(T)" || { echo '用法: make find T=病死的'; exit 2; }
	@grep -n -- "$(T)" $(MASTER) | head -20

# ---------------------------------------------------------------- 实机调试
.PHONY: dbg where jump play
dbg:                                         ## 编译无头调试 harness（需要 libmgba）
	@mkdir -p work/bin
	clang -O2 -o $(DBG) legacy/gbarun_dbg.c \
		-I$(MGBA_HOME)/include -L$(MGBA_HOME)/lib -lmgba -lm -lpthread
	@echo "== 已编译 $(DBG)"

where:                                       ## 看存档演到哪一行：make where STATE=x.ss1
	@test -n "$(STATE)" || { echo '用法: make where STATE=work/dbg/base.ss1'; exit 2; }
	@$(PY) tools/romdbg.py where --state "$(STATE)"

jump:                                        ## 跳到某行存出 .ss1：make jump E=500 I=204
	@test -n "$(E)" -a -n "$(I)" || { echo '用法: make jump E=500 I=204'; exit 2; }
	@$(PY) tools/romdbg.py jump --entry $(E) --index $(I)

play:                                        ## 无头复跑并截图：make play STATE=x.ss1 KEYS="A@120:4"
	@test -n "$(STATE)" || { echo '用法: make play STATE=x.ss1 KEYS="A@120:4"'; exit 2; }
	@$(PY) tools/romdbg.py play --state "$(STATE)" --frames $(FRAMES) \
		--keys "$(KEYS)" --shot-every 60 --out work/dbg/play

# ---------------------------------------------------------------- 杂项
.PHONY: backup clean distclean env help
backup:                                      ## 备份现有 ROM 到 work/backup/
	@mkdir -p work/backup
	@cp -p "$(ROM)" "work/backup/$$(date +%Y%m%d-%H%M%S)-$(ROM)"
	@ls -lt work/backup | head -4

clean:                                       ## 清掉 work/ 缓存（data/ 不动）
	rm -rf work/cache work/batches work/play work/dbg
	@echo "== 已清 work/ 缓存（data/ 与 ROM 未动）"

distclean: clean                             ## 连 ROM 一起清掉
	rm -f "$(ROM)" work/font_patched.gba
	@echo "== 已删除构建产物"

env:                                         ## 检查依赖是否齐备
	@echo "python3 : $$($(PY) -V 2>&1)"
	@echo "make    : $$($(MAKE) -v 2>&1 | head -1)"
	@test -f "$(JP_ROM)" && echo "日文原版: OK" || echo "日文原版: !! 缺少 $(JP_ROM)"
	@test -x $(DBG) && echo "调试 harness: OK ($(DBG))" || echo "调试 harness: 未编译（make dbg）"
	@test -d $(MGBA_HOME)/lib && echo "libmgba : $(MGBA_HOME)" || echo "libmgba : !! 未找到（make dbg 会失败）"
	@test -f data/glyph_png/manifest.tsv && echo "字形底稿: OK" || echo "字形底稿: 缺失（python3 tools/export_glyphs.py）"

help:                                        ## 显示全部目标
	@echo "侦探神宫寺三郎 — 白影的少女 · 构建目标"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}' || true
	@echo
	@echo "变量：E=条目 I=下标 S=场景 T=文本 STATE=存档 KEYS=按键 TOP=条数"
