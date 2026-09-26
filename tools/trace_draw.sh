#!/bin/bash
# Trace what the game reads/writes while a screen is (re)drawn.
#
#   trace_draw.sh <savestate> <frames> "<keys>" [outprefix]
#
# Loads the state, injects the keys (GBARUN_KEYS syntax: frame:KEY:hold;...),
# and logs:
#   * every CPU store into OBJ VRAM   -> /tmp/td_store.txt
#   * every CPU read of the font table and of the appended area
#                                     -> /tmp/td_load.txt
# so we can see which glyph index / tile slot is written where, and by which PC.
set -u
STATE="$1"; FRAMES="${2:-600}"; KEYS="${3:-}"; PFX="${4:-/tmp/td}"
cd "$(dirname "$0")/.."
rm -f /tmp/td_store.txt /tmp/td_load.txt /tmp/gbarun_mem.txt
GBARUN_STATE="$STATE" GBARUN_KEYS="$KEYS" \
GBARUN_LOGSTORE=0x06010000:0x06018000 \
GBARUN_LOGLOAD=0x08800000:0x08C10000 \
  work/bin/gbarun_state out.gba "$FRAMES" "$PFX" >/dev/null 2>&1
[ -f /tmp/gbarun_mem.txt ] && mv /tmp/gbarun_mem.txt /tmp/td_load.txt
echo "stores -> /tmp/td_store.txt ($( [ -f /tmp/td_store.txt ] && wc -l < /tmp/td_store.txt || echo 0 ) lines)"
echo "loads  -> /tmp/td_load.txt  ($( [ -f /tmp/td_load.txt ] && wc -l < /tmp/td_load.txt || echo 0 ) lines)"
