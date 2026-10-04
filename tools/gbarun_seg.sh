#!/bin/bash
# gbarun_seg.sh <rom> <sav> <inState|-> <frames> <outPrefix> <shotEvery> [keys...]
# 分段驱动：从 <inState> 起跑 <frames> 帧，按 [keys...] 按键，末尾存 <outPrefix>.ss3
# 存完可以用 tools/state_inspect.py 看游标，或看 <outPrefix>_shot*.ppm 截图。
cd "$(dirname "$0")/.." || exit 1
ROM="$1"; SAV="$2"; IN="$3"; FR="$4"; OUT="$5"; EVERY="$6"; shift 6
ARGS=()
[ "$IN" != "-" ] && ARGS+=(GBARUN_STATE="$IN")
ARGS+=(GBARUN_NO_AUTOSAVE=1 GBARUN_SAVFILE="$SAV" GBARUN_SHOT_EVERY="$EVERY" GBARUN_SAVE="$OUT.ss3")
env "${ARGS[@]}" ./work/bin/gbarun_dbg "$ROM" "$FR" "$OUT.ppm" "$@" 2>&1 | tail -3
