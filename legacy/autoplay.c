/* autoplay.c - scripted headless mGBA driver for playthrough testing.
 *
 * Unlike gbarun_shot (absolute frame numbers given on the command line), this
 * reads a *script file* with relative timing, so a playthrough can be written
 * as a readable sequence of button presses and screenshots:
 *
 *   wait 120              run 120 frames
 *   press A               tap A (4 frames held, 12 frames gap)
 *   press A 6 30          tap A: hold 6 frames, gap 30 frames
 *   hold START 8          hold START for 8 frames
 *   mash A 20             tap A 20 times
 *   shot 01_title         write <outdir>/01_title.png
 *   save work/play/ck1.ss checkpoint the emulator (mGBA savestate)
 *   load work/play/ck1.ss resume from checkpoint
 *   msg hello             print a line to stderr
 *
 * Reactive commands (the script is loaded into memory and executed with a
 * program counter, so control flow works):
 *
 *   label <name>                    marker, no-op
 *   goto <name>                     jump to label (unknown label = fatal)
 *   if_live <label> / if_nlive <l>  jump if the script engine is / is not live
 *   if_eq <addr> <val> <label>      read u32 at addr, jump if == val (0x ok)
 *   if_ne <addr> <val> <label>      read u32 at addr, jump if != val
 *   wait_eq <addr> <val> <frames>   run frame by frame until u32(addr) == val
 *   wait_ne <addr> <val> <frames>   ... until != val
 *   wait_chg <addr> <frames>        ... until u32(addr) differs from the
 *                                   value sampled when the command started
 *   wait_live <frames>              wait until the script engine is "live"
 *                                   (pool in EWRAM 0x02020000..0x02040000 and
 *                                   current line offset < 0x20000)
 *   expect_entry <N>                wait_live(600), then reverse-lookup the
 *                                   current (keyCount,keyB) in the ROM
 *                                   dispatch table; mismatch = fatal
 *   mash_stall <KEY> <frames> [w]   dialogue advancer: tap KEY (hold 4 / gap
 *                                   16), then wait up to w frames (default 40)
 *                                   for the cursor pair (0x03007C40,0x03007C44)
 *                                   to change; 2 consecutive taps without a
 *                                   change = stall (printed, not fatal).
 *                                   Starts with wait_live(1200); gives up
 *                                   quietly if the engine never goes live.
 *                                   Page-full turns of this game need ~110
 *                                   frames before the cursor moves: use
 *                                   "mash_stall A 6000 150" for long runs.
 *   pick <N>                        menu navigation: UP x8, DOWN xN, A
 *                                   (tap 4/12, 8 frames between steps)
 *                                   NOTE: the game's ring menus WRAP, so this
 *                                   is unreliable; prefer nav.
 *   nav <idx> [max]                 ring-menu navigation via the highlight
 *                                   cursor u32 @0x0200DF94: DOWN until
 *                                   highlight == idx (wraps), then A.
 *                                   Failure = fatal (timeout path).
 *   navc <idx> [max]                same for choice lists (@0x02012F88)
 *   navsoft <idx> [max]             like navc, but a silent no-op when no
 *                                   prompt is open and never fatal -- use it
 *                                   to unwind the search screen's submenus
 *   okmenu                          if a prompt is open, pick item 0 + A
 *   drive <frames> <a1,a2,...> [c]   unattended pusher for free-roam phases:
 *                                   choice prompt -> pick item c (default 1);
 *                                   command menu (uiflag=2 + cursor moves) ->
 *                                   take the next action of the rotation and A;
 *                                   anything else -> hold A 40 frames (long
 *                                   dialogue pages eat a 4-frame tap)
 *   r32 <addr> [label]              one-shot read, printed to stderr
 *   if_frame_gt <frame> <label>     jump once the frame budget is spent
 *   turbo <N>                       fast-forward (see below)
 *   log_lines <file>                per-frame: when live and the cursor pair
 *                                   changed, append "frame\tentryId\tcursor\ttext"
 *                                   (UTF-8, decoded via the glyph map) to file.
 *                                   "log_lines off" stops; a new file switches.
 *   shot_live <dir>                 additionally write <dir>/f<frame>.png on
 *                                   every cursor change; "shot_live off" stops
 *
 * wait_* / wait_live / expect_entry timeouts print [timeout], write
 * <outdir>/_timeout.png plus a full memory dump, and exit(3).  expect_entry
 * mismatch prints the actual entryId, writes _expect_fail.png plus a dump,
 * and exit(4).
 *
 * Fast-forward ("mGBA 加速"):
 *   turbo <N>        N=0: normal software rendering.  N>0: detach the software
 *                    renderer and let mGBA's dummy renderer absorb
 *                    VRAM/palette/OAM writes (the same trick the frontend's
 *                    fast-forward uses), plus set the core's frameskip field.
 *                    Emulation is bit-identical -- only the picture is stale,
 *                    and every `shot` re-attaches the software renderer and
 *                    repaints before saving the PNG, so screenshots are exact.
 *                    Measured on M2: ~3300 fps plain, ~3500 fps turbo (the
 *                    GBA interpreter, not the renderer, is the bottleneck);
 *                    AUTOPLAY_FRAMESKIP=<N> does the same from the env.
 *                    The real speedup for a playthrough comes from driving
 *                    waits off memory instead of fixed frame counts
 *                    (mash_stall / wait_eq) -- a fixed "wait 600" costs the
 *                    same whether the text is done or not.
 *
 * Usage:
 *   autoplay <rom> <script> [outdir]
 *
 * Environment:
 *   GBARUN_STATE=<file>   load a savestate before the script
 *   GBARUN_BIOS=<file>    use a real BIOS (usually leave unset)
 *   GBARUN_FORCE_SAVE=N   force the cartridge save type
 *   GBARUN_NO_AUTOSAVE=1  do not autoload the .sav next to the ROM
 *   AUTOPLAY_GLYPHMAP=<f> glyph CSV for text decode (default data/glyph_map.csv;
 *                           use work/glyph_map.ext.csv for the patched CN ROM)
 */
#include <mgba/core/core.h>
#include <mgba/core/interface.h>
#include <mgba/core/serialize.h>
#include <mgba/gba/core.h>
#include <mgba/internal/gba/gba.h>
#include <mgba/internal/gba/video.h>
#include <mgba/internal/gba/renderers/video-software.h>
#include <mgba/internal/gba/input.h>
#include <mgba/internal/gba/savedata.h>
#include <mgba-util/vfs.h>

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <zlib.h>

#define WIDTH 240
#define HEIGHT 160

/* Script engine state addresses (see tools/state_inspect.py). */
#define ADDR_DESC     0x02003F24  /* +0x00 pool base, +0x10 keyB, +0x14 keyCount */
#define ADDR_CURSOR   0x03007C40  /* u32, offset of last-read row +2 */
#define ADDR_LINE     0x03007C44  /* u32, current line offset inside the pool */
#define ADDR_MENU     0x02003EE0  /* u32, menu flags (stall diagnostics) */
#define ADDR_INEDGE   0x02003EEC  /* u16, A/B edge bits (stall diagnostics) */

/* Dispatch table: ROM file offset 0x15C004, 509 * 8 bytes
 * { s16 entryId, s16 keyCount, s16 keyA, s16 keyB } (tools/romdbg.py). */
#define DISPATCH_FILE  0x15C004
#define DISPATCH_COUNT 509

/* mGBA logs every BIOS SWI at its default level; a playthrough emits thousands
 * of lines.  Silence everything unless AUTOPLAY_LOG=1. */
#include <mgba/core/log.h>
#include <stdarg.h>
static void quietLog(struct mLogger* log, int cat, enum mLogLevel level,
                     const char* fmt, va_list args) {
	(void) log; (void) cat; (void) level; (void) fmt; (void) args;
}
static struct mLogger g_quiet = { quietLog };

static color_t g_frame[WIDTH * HEIGHT];
static struct mCore* g_core;
static struct GBA* g_gba;
static long g_frameNo;

/* ------------------------------------------------------------- glyph map -- */
/* data/glyph_map.csv: code(hex),dec,char -- char is one UTF-8 character and
 * may be quoted when it contains a comma.  Decoding a codepoint c:
 * idx = (c < 0x20) ? c : c - 1; look up MAP[idx]; miss = "<XXX>". */
static char** g_glyphMap;
static size_t g_glyphMapSize;

static void loadGlyphMap(const char* path) {
	FILE* f = fopen(path, "r");
	if (!f) {
		fprintf(stderr, "[glyphmap] cannot open %s (text decode disabled)\n", path);
		return;
	}
	char line[512];
	while (fgets(line, sizeof(line), f)) {
		char* p1 = strchr(line, ',');
		if (!p1) continue;
		char* p2 = strchr(p1 + 1, ',');
		if (!p2) continue;
		unsigned idx = strtoul(line, NULL, 16);
		if (idx >= g_glyphMapSize) {
			size_t ns = idx + 256;
			char** nm = realloc(g_glyphMap, ns * sizeof(*nm));
			if (!nm) break;
			for (size_t i = g_glyphMapSize; i < ns; ++i) nm[i] = NULL;
			g_glyphMap = nm;
			g_glyphMapSize = ns;
		}
		char* val = p2 + 1;
		val[strcspn(val, "\r\n")] = '\0';
		if (*val == '"') {
			/* quoted field: "" is an escaped quote, closing " ends it */
			char* s = val + 1;
			char* d = val;
			while (*s) {
				if (*s == '"') {
					if (s[1] == '"') { *d++ = '"'; s += 2; continue; }
					break;
				}
				*d++ = *s++;
			}
			*d = '\0';
		}
		free(g_glyphMap[idx]);
		g_glyphMap[idx] = *val ? strdup(val) : NULL;
	}
	fclose(f);
	fprintf(stderr, "[glyphmap] loaded %s (%zu slots)\n", path, g_glyphMapSize);
}

/* ------------------------------------------------------------------ PNG -- */
static void put32(unsigned char* p, unsigned v) {
	p[0] = (v >> 24) & 0xFF;
	p[1] = (v >> 16) & 0xFF;
	p[2] = (v >> 8) & 0xFF;
	p[3] = v & 0xFF;
}

static void chunk(FILE* f, const char* type, const unsigned char* data, unsigned len) {
	unsigned char hdr[8];
	unsigned crc;
	put32(hdr, len);
	memcpy(hdr + 4, type, 4);
	fwrite(hdr, 1, 8, f);
	if (len) fwrite(data, 1, len, f);
	crc = crc32(0, (const Bytef*) type, 4);
	if (len) crc = crc32(crc, data, len);
	put32(hdr, crc);
	fwrite(hdr, 1, 4, f);
}

/* Frame skip (mGBA fast-forward): the core draws only one frame in N+1; a
 * screenshot has to force the skipped scanlines out by hand.  Emulation state
 * is untouched - drawScanline()/finishFrame() are pure output. */
static int g_frameskip = 0;

/* Turbo: detach the software renderer entirely and let mGBA's dummy renderer
 * absorb the VRAM/palette/OAM writes (the same trick the frontend's
 * fast-forward uses).  The picture is only brought back up to date when a
 * screenshot is taken. */
static struct GBAVideoSoftwareRenderer* g_sw;
static struct GBAVideoRenderer g_dummy;
static int g_turbo = 0;

static void attachSoftware(void) {
	if (!g_sw) return;
	GBAVideoAssociateRenderer(&g_gba->video, &g_sw->d);
	g_sw->outputBuffer = g_frame;
	g_sw->outputBufferStride = WIDTH;
	/* AssociateRenderer does not replay the register state in this mGBA
	 * build, so feed the renderer the current VRAM/palette/OAM by hand. */
	for (int i = 0; i < 0x100; ++i)
		g_sw->d.writePalette(&g_sw->d, i * 2, g_gba->video.palette[i]);
	for (uint32_t i = 0; i < 0x18000; ++i) g_sw->d.writeVRAM(&g_sw->d, i);
	for (int i = 0; i < 0x400; ++i) g_sw->d.writeOAM(&g_sw->d, i);
}

static void forceRedraw(void) {
	struct GBAVideoRenderer* r = g_gba->video.renderer;
	if (!r || !r->drawScanline) return;
	for (int y = 0; y < HEIGHT; ++y) r->drawScanline(r, y);
	if (r->finishFrame) r->finishFrame(r);
}

static void setFrameskip(int n) {
	g_frameskip = n < 0 ? 0 : n;
	g_gba->video.frameskip = g_frameskip;
	g_gba->video.frameskipCounter = 0;
}

/* turbo <N>: N=0 -> normal rendering.  N>0 -> dummy renderer + optional frame
 * skip; every screenshot re-attaches the software renderer for one frame. */
static void setTurbo(int n) {
	setFrameskip(n);
	int on = n > 0;
	if (on == g_turbo) return;
	if (on) {
		GBAVideoDummyRendererCreate(&g_dummy);
		GBAVideoAssociateRenderer(&g_gba->video, &g_dummy);
	} else {
		attachSoftware();
		forceRedraw();
	}
	g_turbo = on;
}

/* Snapshot to PNG.  With turbo on this is the only thing that brings the
 * software renderer (and therefore the picture) back up to date. */
static void writePNGFile(const char* path);

static void writePNG(const char* path) {
	int restoreTurbo = g_turbo;
	if (g_turbo) attachSoftware();
	forceRedraw();
	writePNGFile(path);
	if (restoreTurbo) {
		GBAVideoAssociateRenderer(&g_gba->video, &g_dummy);
	}
}

static void writePNGFile(const char* path) {
	unsigned char* raw = malloc((size_t) HEIGHT * (1 + WIDTH * 3));
	uLongf clen = compressBound((uLong) HEIGHT * (1 + WIDTH * 3));
	unsigned char* comp = malloc(clen);
	FILE* f;
	if (!raw || !comp) { free(raw); free(comp); return; }
	for (int y = 0; y < HEIGHT; ++y) {
		unsigned char* row = raw + (size_t) y * (1 + WIDTH * 3);
		row[0] = 0;
		for (int x = 0; x < WIDTH; ++x) {
			color_t c = g_frame[y * WIDTH + x];
			row[1 + x * 3 + 0] = c & 0xFF;
			row[1 + x * 3 + 1] = (c >> 8) & 0xFF;
			row[1 + x * 3 + 2] = (c >> 16) & 0xFF;
		}
	}
	if (compress2(comp, &clen, raw, (uLong) HEIGHT * (1 + WIDTH * 3), 6) != Z_OK) {
		free(raw); free(comp); return;
	}
	f = fopen(path, "wb");
	if (!f) { perror(path); free(raw); free(comp); return; }
	fwrite("\x89PNG\r\n\x1a\n", 1, 8, f);
	{
		unsigned char ihdr[13];
		put32(ihdr, WIDTH);
		put32(ihdr + 4, HEIGHT);
		ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
		chunk(f, "IHDR", ihdr, 13);
	}
	chunk(f, "IDAT", comp, (unsigned) clen);
	chunk(f, "IEND", NULL, 0);
	fclose(f);
	free(raw);
	free(comp);
}

/* ------------------------------------------------------------ mem helpers -- */
static void runFrames(long n, uint32_t keys);

static uint32_t r32(uint32_t addr) { return GBAView32(g_gba->cpu, addr); }
static uint16_t r16(uint32_t addr) { return GBAView16(g_gba->cpu, addr); }
/* 未对齐 u32 读：行表可能奇数对齐，GBAView32 会做 ARM 旋转，必须自己拼。 */
static uint32_t r32u(uint32_t addr) {
	return (uint32_t) GBAView8(g_gba->cpu, addr)
	     | ((uint32_t) GBAView8(g_gba->cpu, addr + 1) << 8)
	     | ((uint32_t) GBAView8(g_gba->cpu, addr + 2) << 16)
	     | ((uint32_t) GBAView8(g_gba->cpu, addr + 3) << 24);
}

static void w32(uint32_t addr, uint32_t v) { int cyc = 1; GBAStore32(g_gba->cpu, addr, (int32_t) v, &cyc); }

static int engineLive(void) {
	uint32_t pool = r32(ADDR_DESC);
	return pool >= 0x02020000 && pool < 0x02040000 && r32(ADDR_LINE) < 0x20000;
}

/* gotorow <N>: 把引擎的"行游标"直接挪到本资源第 N 行（0 基）。
 * 用法见 docs/调试器.md「跳剧情」：行地址 = 池基址 + u32[表项]，
 * 0x03007C40 存"表项偏移 + 2"，0x03007C44 存"刚取到的行偏移"。 */
/* skiprows <k>: 相对当前行前进/后退 k 行。
 * 引擎的 0x03007C40 = 表项池内偏移 + 2，所以目标表项 = 池基址 + (cur - 2 + 4k)。 */
/* navfast <idx>: 提示菜单一出现就"立刻"选（菜单大约 5 秒不操作会自己按默认项走），
 * 每次只按 4 帧键、8 帧等，优先 RIGHT（这类 2x2 菜单第一行是 0/1）。 */
static void navFast(long target) {
	uint32_t addr = 0x02012F88;
	/* 这类菜单是 2x2：索引 0/1 在第一行，2/3 在第二行。
	 * 先按行（DOWN/UP），再按列（RIGHT/LEFT），最后按 A。 */
	for (int i = 0; i < 60; ++i) {
		if (r32(0x02015160) != 4) return;
		uint32_t cur = r32(addr);
		if (cur == (uint32_t) target) {
			runFrames(4, 1u << GBA_KEY_A);
			fprintf(stderr, "[navfast] f=%ld -> %ld\n", g_frameNo, target);
			return;
		}
		long trow = target >= 2 ? 1 : 0, crow = cur >= 2 ? 1 : 0;
		int key;
		if (trow != crow) key = (trow > crow) ? GBA_KEY_DOWN : GBA_KEY_UP;
		else key = (cur < (uint32_t) target) ? GBA_KEY_RIGHT : GBA_KEY_LEFT;
		uint32_t before = cur;
		runFrames(4, 1u << key);
		runFrames(8, 0);
		if (r32(addr) == before) {   /* 这个方向不动，换个方向试 */
			runFrames(4, 1u << (key == GBA_KEY_RIGHT ? GBA_KEY_LEFT : GBA_KEY_RIGHT));
			runFrames(8, 0);
		}
	}
	fprintf(stderr, "[navfast] f=%ld 没选到 %ld (cur=%u)\n", g_frameNo, target, r32(addr));
}

static void skipRows(long k) {
	uint32_t pool = r32(ADDR_DESC);
	uint32_t cur = r32(ADDR_CURSOR);
	if (!engineLive() || cur < 2) {
		fprintf(stderr, "[skiprows] f=%ld 不可跳 (live=%d cur=0x%X)\n", g_frameNo, engineLive(), cur);
		return;
	}
	long target = (long) cur - 2 + 4 * k;
	if (target < 0) { fprintf(stderr, "[skiprows] 越界\n"); return; }
	uint32_t off = r32u(pool + (uint32_t) target);
	w32(ADDR_CURSOR, (uint32_t) target + 2);
	w32(ADDR_LINE, off);
	fprintf(stderr, "[skiprows] f=%ld +%ld 行 -> 表项池内 0x%lX, 行偏移 0x%X\n",
	        g_frameNo, k, target, off);
}

static void gotoRow(long n) {
	uint32_t pool = r32(ADDR_DESC);
	uint32_t cnt = r32(ADDR_DESC + 0x14) & 0xFFFF;
	if (!engineLive() || n < 0 || (uint32_t) n >= cnt) {
		fprintf(stderr, "[gotorow] f=%ld 不可跳 (live=%d n=%ld cnt=%u)\n",
		        g_frameNo, engineLive(), n, cnt);
		return;
	}
	uint32_t tbl = pool + (uint32_t) n * 4;
	uint32_t off = r32u(tbl);
	w32(ADDR_CURSOR, (uint32_t) n * 4 + 2);
	w32(ADDR_LINE, off);
	fprintf(stderr, "[gotorow] f=%ld -> row %ld (off=0x%X, cnt=%u)\n", g_frameNo, n, off, cnt);
}


/* Reverse-lookup the current descriptor's (keyCount,keyB) in the ROM dispatch
 * table.  Returns entryId, or -1 when no row matches. */
static int currentEntryId(void) {
	int16_t keyB = (int16_t) (r32(ADDR_DESC + 0x10) & 0xFFFF);
	int16_t keyCount = (int16_t) (r32(ADDR_DESC + 0x14) & 0xFFFF);
	for (int k = 0; k < DISPATCH_COUNT; ++k) {
		uint32_t base = 0x08000000 + DISPATCH_FILE + 8 * k;
		unsigned char b[8];
		for (int i = 0; i < 8; ++i) b[i] = GBAView8(g_gba->cpu, base + i);
		int16_t entryId = (int16_t) (b[0] | (b[1] << 8));
		int16_t kc = (int16_t) (b[2] | (b[3] << 8));
		int16_t kb = (int16_t) (b[6] | (b[7] << 8));
		if (kc == keyCount && kb == keyB) return entryId;
	}
	return -1;
}

/* Decode the u16 codepoint string at pool + lineOff into out (UTF-8).
 * Line offsets can be ODD (packed u16 array), and GBAView16 silently aligns
 * the address down, so assemble each codepoint from two byte reads.
 * Unknown codepoints become "<XXX>" with the hex map index. */
static void decodeLine(uint32_t lineOff, char* out, size_t outsz) {
	uint32_t addr = r32(ADDR_DESC) + lineOff;
	size_t n = 0;
	out[0] = '\0';
	for (int i = 0; i < 512 && n + 8 < outsz; ++i, addr += 2) {
		uint16_t c = GBAView8(g_gba->cpu, addr) | (GBAView8(g_gba->cpu, addr + 1) << 8);
		if (!c) break;
		unsigned idx = (c < 0x20) ? c : c - 1;
		const char* ch = (idx < g_glyphMapSize) ? g_glyphMap[idx] : NULL;
		if (ch && *ch) {
			size_t l = strlen(ch);
			if (n + l >= outsz) break;
			memcpy(out + n, ch, l);
			n += l;
		} else {
			n += snprintf(out + n, outsz - n, "<%03X>", idx);
		}
	}
	out[n] = '\0';
}

/* ----------------------------------------------------------------- input -- */
static int keyByName(const char* name) {
	if (!strcmp(name, "A")) return GBA_KEY_A;
	if (!strcmp(name, "B")) return GBA_KEY_B;
	if (!strcmp(name, "SELECT")) return GBA_KEY_SELECT;
	if (!strcmp(name, "START")) return GBA_KEY_START;
	if (!strcmp(name, "RIGHT")) return GBA_KEY_RIGHT;
	if (!strcmp(name, "LEFT")) return GBA_KEY_LEFT;
	if (!strcmp(name, "UP")) return GBA_KEY_UP;
	if (!strcmp(name, "DOWN")) return GBA_KEY_DOWN;
	if (!strcmp(name, "R")) return GBA_KEY_R;
	if (!strcmp(name, "L")) return GBA_KEY_L;
	return -1;
}

/* ------------------------------------------------- per-frame line logging -- */
static FILE* g_logFile;
static char g_shotLiveDir[1024];
static uint32_t g_lastCur = 0xFFFFFFFF, g_lastLine = 0xFFFFFFFF;
static long g_linesLogged, g_stalls;
/* The engine updates the cursor *while* it is still writing the new line into
 * the pool, so decoding at the moment of change yields garbage.  The pool is
 * append-only within an entry, hence the previous line stays intact: keep one
 * pending (cur,line) and flush it when the cursor moves on, when it has been
 * stable for 240 frames (line fully typed, engine idling), or when logging is
 * turned off.  A pending line is dropped if the descriptor keys changed in
 * the meantime (entry switch = pool reloaded). */
static int g_pendValid;
static uint32_t g_pendCur, g_pendLine, g_pendKeyB, g_pendKeyCount;
static long g_pendFrame, g_lastChange;

static void flushPendingLine(void) {
	if (!g_pendValid) return;
	g_pendValid = 0;
	if (!g_logFile || !engineLive()) return;
	if (r32(ADDR_DESC + 0x10) != g_pendKeyB || r32(ADDR_DESC + 0x14) != g_pendKeyCount)
		return;
	char text[2048];
	decodeLine(g_pendLine, text, sizeof(text));
	fprintf(g_logFile, "%ld\t%d\t0x%08X/0x%08X\t%s\n",
	        g_pendFrame, currentEntryId(), g_pendCur, g_pendLine, text);
	fflush(g_logFile);
	++g_linesLogged;
}

static void afterFrame(void) {
	uint32_t cur, line;
	if (!g_logFile && !g_shotLiveDir[0]) return;
	if (!engineLive()) return;
	cur = r32(ADDR_CURSOR);
	line = r32(ADDR_LINE);
	if (cur == g_lastCur && line == g_lastLine) {
		if (g_pendValid && g_frameNo - g_lastChange >= 240) flushPendingLine();
		return;
	}
	g_lastCur = cur;
	g_lastLine = line;
	g_lastChange = g_frameNo;
	if (g_logFile) {
		flushPendingLine();
		g_pendValid = 1;
		g_pendCur = cur;
		g_pendLine = line;
		g_pendKeyB = r32(ADDR_DESC + 0x10);
		g_pendKeyCount = r32(ADDR_DESC + 0x14);
		g_pendFrame = g_frameNo;
	}
	if (g_shotLiveDir[0]) {
		char path[1024];
		snprintf(path, sizeof(path), "%s/f%ld.png", g_shotLiveDir, g_frameNo);
		writePNG(path);
	}
}

static void runFrames(long n, uint32_t keys) {
	g_core->setKeys(g_core, keys);
	for (long i = 0; i < n; ++i) {
		g_core->runFrame(g_core);
		++g_frameNo;
		afterFrame();
	}
}

static const char* g_outdir = "work/play";

static void shotPath(const char* name, char* buf, size_t n) {
	if (name[0] == '/') snprintf(buf, n, "%s", name);
	else snprintf(buf, n, "%s/%s.png", g_outdir, name);
}

/* ----------------------------------------------------------------- state -- */
static int loadState(const char* path) {
	struct VFile* vf = VFileOpen(path, O_RDONLY);
	int ok = 0;
	if (vf) {
		ok = mCoreLoadStateNamed(g_core, vf, SAVESTATE_ALL);
		vf->close(vf);
	}
	if (ok) {
		struct GBAVideoRenderer* r = g_gba->video.renderer;
		for (int i = 0; i < 512; ++i) r->writePalette(r, i * 2, g_gba->video.palette[i]);
		for (int i = 0; i < 128; ++i) r->writeOAM(r, i);
	}
	return ok;
}

static int saveState(const char* path) {
	/* SAVESTATE_ALL captures a screenshot, which needs a real renderer: with
	 * turbo on the software renderer is detached, so bring it back for the
	 * duration of the save (otherwise mCoreSaveStateNamed fails). */
	int restore = g_turbo;
	if (g_turbo) { attachSoftware(); forceRedraw(); }
	struct VFile* vf = VFileOpen(path, O_WRONLY | O_CREAT | O_TRUNC);
	int ok = 0;
	if (vf) {
		ok = mCoreSaveStateNamed(g_core, vf, SAVESTATE_ALL);
		vf->close(vf);
	}
	if (restore) GBAVideoAssociateRenderer(&g_gba->video, &g_dummy);
	return ok;
}

/* ---------------------------------------------------------------- script -- */

/* Memory dump in the same layout as gbarun_state's, so the existing Python
 * decoders (dump_screen_text.py / sprite_text.py) work on playthrough frames. */
struct mregion { const char* name; uint32_t start; uint32_t size; };
static const struct mregion REGIONS[] = {
	{ "bios",    0x00000000, 0x4000 },
	{ "ewram",   0x02000000, 0x40000 },
	{ "iwram",   0x03000000, 0x8000 },
	{ "io",      0x04000000, 0x400 },
	{ "palette", 0x05000000, 0x400 },
	{ "vram",    0x06000000, 0x18000 },
	{ "oam",     0x07000000, 0x400 },
	{ "rom",     0x08000000, 0 },
};
#define NREGIONS (sizeof(REGIONS) / sizeof(REGIONS[0]))

static void dumpMemory(const char* name) {
	char path[1024], idxPath[1024], infoPath[1024];
	snprintf(path, sizeof(path), "%s/%s_f%ld_mem.bin", g_outdir, name, g_frameNo);
	snprintf(idxPath, sizeof(idxPath), "%s/%s_f%ld_mem.idx", g_outdir, name, g_frameNo);
	snprintf(infoPath, sizeof(infoPath), "%s/%s_f%ld_info.txt", g_outdir, name, g_frameNo);
	FILE* fb = fopen(path, "wb");
	FILE* fi = fopen(idxPath, "wb");
	FILE* ft = fopen(infoPath, "wb");
	if (!fb || !fi || !ft) {
		perror("fopen dump");
		if (fb) fclose(fb);
		if (fi) fclose(fi);
		if (ft) fclose(ft);
		return;
	}
	/* AUTOPLAY_MEM_LITE=1 drops the ROM/BIOS so a dump is ~380 KB instead of
	 * 12 MB, which makes per-press dumps during a playthrough affordable. */
	int lite = getenv("AUTOPLAY_MEM_LITE") != NULL;
	fprintf(fi, "# name start size fileoffset\n");
	unsigned long long offset = 0;
	for (size_t i = 0; i < NREGIONS; ++i) {
		if (lite && (i == 0 || i == NREGIONS - 1)) continue;   /* bios, rom */
		uint32_t size = REGIONS[i].size;
		if (size == 0) size = (uint32_t) g_core->romSize(g_core);
		fprintf(fi, "%s 0x%08X 0x%X 0x%llX\n", REGIONS[i].name, REGIONS[i].start, size, offset);
		for (uint32_t a = 0; a < size; ++a) {
			unsigned char b = GBAView8(g_gba->cpu, REGIONS[i].start + a);
			fwrite(&b, 1, 1, fb);
		}
		offset += size;
	}
	fclose(fb);
	fclose(fi);
	fprintf(ft, "PC=0x%08X\n", g_gba->cpu->gprs[15]);
	fprintf(ft, "KEYINPUT=0x%04X\n", GBAView16(g_gba->cpu, 0x04000130));
	fprintf(ft, "DISPCNT=0x%04X\n", GBAView16(g_gba->cpu, 0x04000000));
	fprintf(ft, "BG0CNT=0x%04X BG1CNT=0x%04X BG2CNT=0x%04X BG3CNT=0x%04X\n",
	        GBAView16(g_gba->cpu, 0x04000008), GBAView16(g_gba->cpu, 0x0400000A),
	        GBAView16(g_gba->cpu, 0x0400000C), GBAView16(g_gba->cpu, 0x0400000E));
	fclose(ft);
	fprintf(stderr, "[mem] f=%ld %s\n", g_frameNo, path);
}

/* Fatal timeout for wait_* / wait_live / expect_entry: screenshot + dump. */
static void timeoutFail(const char* what) {
	char path[1024];
	fprintf(stderr, "[timeout] %s f=%ld\n", what, g_frameNo);
	shotPath("_timeout", path, sizeof(path));
	writePNG(path);
	fprintf(stderr, "[shot] f=%ld %s\n", g_frameNo, path);
	dumpMemory("_timeout");
	exit(3);
}

/* Run frame by frame until u32(addr) satisfies the wait condition.
 * mode: 0 wait_eq, 1 wait_ne, 2 wait_chg (baseline sampled at entry).
 * Returns 1 when satisfied, 0 on timeout. */
static int waitMem(int mode, uint32_t addr, uint32_t val, long timeout) {
	uint32_t base = (mode == 2) ? r32(addr) : 0;
	for (long f = 0; f < timeout; ++f) {
		uint32_t v = r32(addr);
		if ((mode == 0 && v == val) || (mode == 1 && v != val) ||
		    (mode == 2 && v != base)) return 1;
		runFrames(1, 0);
	}
	return 0;
}

/* Non-fatal wait_live; the wait_live *command* turns a 0 into timeoutFail. */
static int waitLiveFrames(long timeout) {
	for (long f = 0; f < timeout; ++f) {
		if (engineLive()) return 1;
		runFrames(1, 0);
	}
	return 0;
}

/* push dialogue: tap KEY, wait up to chgWait frames for the cursor pair to
 * move.  Stall (2 dead taps in a row) or overall timeout are reported, not
 * fatal.  Note: page-full turns of this game can take ~110 frames after the
 * tap before the cursor moves, so chgWait=40 only works mid-page.
 * Stops when the engine goes not-live for 300+ frames (menu / save prompt /
 * scene transition), so the following script steps - not the mash - decide
 * what to press there; brief not-live blips are entry reloads and are
 * waited through without pressing. */
static void mashStall(int key, long timeout, long chgWait) {
	long start = g_frameNo;
	int dead = 0;
	uint32_t kmask = 1u << key;
	if (!waitLiveFrames(1200)) {
		fprintf(stderr, "[mash_stall] f=%ld not live after wait_live(1200), giving up\n",
		        g_frameNo);
		return;
	}
	while (g_frameNo - start < timeout) {
		uint32_t prevCur = r32(ADDR_CURSOR);
		uint32_t prevLine = r32(ADDR_LINE);
		int changed = 0;
		runFrames(4, kmask);
		runFrames(16, 0);
		for (long i = 0; i < chgWait; ++i) {
			runFrames(1, 0);
			/* UI mode flag: 0=dialogue, 2=menu OR auto command cutscene
			 * (tag shown), 4=choice prompt.  4 = bail before tapping into it.
			 * 2 = could be a real menu (cursor pinned) or an auto scene
			 * (cursor still moving): keep waiting; a full wait with no cursor
			 * movement means menu - return without tapping again. */
			uint32_t uif = r32(0x02015160);
			if (uif == 4) {
				fprintf(stderr, "[mash_stall] f=%ld choice prompt opened (uiflag=4)\n",
				        g_frameNo);
				return;
			}
			if (!engineLive()) {
				if (!waitLiveFrames(300)) {
					fprintf(stderr, "[mash_stall] f=%ld engine not-live 300f+ (menu/prompt/transition)\n",
					        g_frameNo);
					return;
				}
			}
			if (r32(ADDR_CURSOR) != prevCur || r32(ADDR_LINE) != prevLine) {
				changed = 1;
				break;
			}
			if (g_frameNo - start >= timeout) break;
		}
		if (changed) {
			dead = 0;
		} else if (r32(0x02015160) == 2) {
			fprintf(stderr, "[mash_stall] f=%ld menu open (uiflag=2, cursor pinned)\n",
			        g_frameNo);
			return;
		} else if (++dead >= 2) {
			++g_stalls;
			fprintf(stderr, "[stall] f=%ld cur=0x%08X line=0x%08X menu=0x%08X inedge=0x%04X\n",
			        g_frameNo, r32(ADDR_CURSOR), r32(ADDR_LINE),
			        r32(ADDR_MENU), r16(ADDR_INEDGE));
			return;
		}
	}
	fprintf(stderr, "[mash_stall] f=%ld timeout after %ld frames (cur=0x%08X line=0x%08X)\n",
	        g_frameNo, timeout, r32(ADDR_CURSOR), r32(ADDR_LINE));
}

/* Ring/choice menu navigation: DOWN-loop until the highlight cursor at
 * cursorAddr == target (menus wrap), then press A. */
static void navMenu(uint32_t cursorAddr, long target, long maxSteps) {
	long s;
	for (s = 0; s < maxSteps && r32(cursorAddr) != (uint32_t) target; ++s) {
		runFrames(4, 1u << GBA_KEY_DOWN);
		runFrames(26, 0);
	}
	if (r32(cursorAddr) != (uint32_t) target) {
		fprintf(stderr, "[nav] FAIL f=%ld addr=0x%08X target=%ld cur=%u\n",
		        g_frameNo, cursorAddr, target, r32(cursorAddr));
		timeoutFail("nav");
	}
	runFrames(45, 0);
	runFrames(4, 1u << GBA_KEY_A);
	runFrames(12, 0);
	fprintf(stderr, "[nav] f=%ld @0x%08X -> %ld (%ld steps)\n",
	        g_frameNo, cursorAddr, target, s);
}

/* navsoft <idx> [maxSteps]: like navc but a no-op when no choice prompt is
 * open, and never fatal.  Used to unwind the search screen's submenus. */
static int navListTry(uint32_t addr, long target, long maxSteps, int needPrompt) {
	if (needPrompt && r32(0x02015160) != 4) return 0;
	/* 提示刚弹出时列表可能还没醒（按方向键纹丝不动），所以整轮重试几次。 */
	for (int attempt = 0; attempt < 4; ++attempt) {
		uint32_t cur = r32(addr);
		if (cur > 63) { runFrames(30, 0); continue; }
		int moved = 0;
		for (long s = 0; s < maxSteps; ++s) {
			cur = r32(addr);
			if (cur == (uint32_t) target) {
				runFrames(4, 1u << GBA_KEY_A);
				runFrames(12, 0);
				fprintf(stderr, "[navsoft] f=%ld @0x%08X -> %ld (try %d)\n",
				        g_frameNo, addr, target, attempt);
				return 1;
			}
			int key = (cur > (uint32_t) target) ? GBA_KEY_UP : GBA_KEY_DOWN;
			uint32_t before = cur;
			runFrames(4, 1u << key);
			runFrames(20, 0);
			if (r32(addr) != before) moved = 1;
		}
		if (!moved) runFrames(40, 0);
	}
	/* 横排提示（公寓「去其他地方／进去里面／不去了」那种）上下走不到，
	 * 再试左右。 */
	int hkeys[2] = { GBA_KEY_RIGHT, GBA_KEY_LEFT };
	for (int hk = 0; hk < 2; ++hk) {
		for (int attempt = 0; attempt < 2; ++attempt) {
			for (long s = 0; s < maxSteps; ++s) {
				if (r32(addr) == (uint32_t) target) {
					runFrames(4, 1u << GBA_KEY_A);
					runFrames(12, 0);
					fprintf(stderr, "[navsoft] f=%ld @0x%08X -> %ld (h%d)\n",
					        g_frameNo, addr, target, hk);
					return 1;
				}
				runFrames(4, 1u << hkeys[hk]);
				runFrames(20, 0);
			}
			runFrames(40, 0);
		}
	}
	fprintf(stderr, "[navsoft] FAIL f=%ld addr=0x%08X target=%ld cur=%u\n",
	        g_frameNo, addr, target, r32(addr));
	return 0;
}

static int navMenuTry(uint32_t addr, long target, long maxSteps) {
	return navListTry(addr, target, maxSteps, 1);
}

/* navisoft <idx> [maxSteps]: non-fatal inference-list navigation
 * (candidate cursor @0x02013340, same UI flag as choice prompts). */
static void navInferTry(long target, long maxSteps) {
	/* 推理候选表：和选择表一样走"逐格验 + 立刻 A"，并带横排回退。 */
	if (navListTry(0x02013340, target, maxSteps > 0 ? maxSteps : 12, 1)) return;
	fprintf(stderr, "[navisoft] f=%ld no hit (cur=%u)\n", g_frameNo, r32(0x02013340));
}

/* navcmd <idx> [maxSteps]: command-menu navigation (ring cursor @0x0200DF94).
 * Non-fatal, and deliberately does NOT press A when the cursor does not move:
 * uiflag=2 is also set by auto cutscenes (tag shown), where this must be a
 * no-op instead of tapping into the scene. */
static void navCmdMenu(long target, long maxSteps) {
	uint32_t addr = 0x0200DF94;
	uint32_t start = r32(addr);
	if (start > 32) {
		fprintf(stderr, "[navcmd] f=%ld not a menu (cur=%u)\n", g_frameNo, start);
		return;
	}
	long s;
	for (s = 0; s < maxSteps && r32(addr) != (uint32_t) target; ++s) {
		runFrames(4, 1u << GBA_KEY_DOWN);
		runFrames(26, 0);
	}
	if (r32(addr) != (uint32_t) target) {
		fprintf(stderr, "[navcmd] f=%ld no move (cur=%u)\n", g_frameNo, r32(addr));
		return;
	}
	runFrames(45, 0);
	runFrames(4, 1u << GBA_KEY_A);
	runFrames(12, 0);
	fprintf(stderr, "[navcmd] f=%ld -> %ld\n", g_frameNo, target);
}

/* drive <frames> <a1,a2,...> [choiceIdx]
 * Unattended pusher for the free-roam phases of this game:
 *   uiflag=4 (choice prompt)          -> pick choiceIdx (default 1) and A
 *   uiflag=2 + command menu responds  -> pick the next action in the rotation
 *                                        and A (rotation advances per use)
 *   otherwise                         -> hold A 40 frames (long dialogue pages
 *                                        need a hold, a 4-frame tap is eaten)
 * The menu test is "does the ring cursor actually move"; during auto cutscenes
 * uiflag is also 2 but the cursor is frozen, so this degrades to just pushing
 * the dialogue. */
static int parseList(const char* in, long* out, int max) {
	int n = 0;
	const char* p = in;
	while (p && *p && n < max) {
		char* end;
		out[n++] = strtol(p, &end, 0);
		if (end == p) break;
		p = (*end == ',') ? end + 1 : end;
		if (!*end) break;
	}
	return n;
}

static void driveLoop(long budget, const char* acts, long choiceIdx, long inferIdx,
                      const char* ansList, const char* infList) {
	long actv[16];
	long ansv[16];
	int nact = parseList(acts, actv, 16);
	int nans = parseList(ansList ? ansList : "", ansv, 16);
	int ansUsed = 0;
	long infq[16];
	int ninf = parseList(infList ? infList : "", infq, 16);
	int infUsed = 0;
	long start = g_frameNo;
	int used = 0;
	int infArmed = 1;      /* 候选游标回到 0 = 可以认下一次推理题 */
	long infTry = 0;       /* 自动模式（inferIdx=-2）已试过的答案个数 */

	while (g_frameNo - start < budget) {
		uint32_t uif = r32(0x02015160);
		long inferTarget = (ninf > 0 && infUsed < ninf) ? infq[infUsed]
	                 : ((inferIdx == -2) ? (infTry % 10) : inferIdx);
		int wantInfer = (inferIdx == -2 || inferIdx >= 0);

		/* uiflag=4 的选择提示（是否保存 / 菜单最后一项之类） */
		if (uif == 4) {
			/* 答案队列：一幕里多个提示各按各的答案走；-1 = 直接按 A 吃默认项。 */
			long pick = (ansUsed < nans) ? ansv[ansUsed] : choiceIdx;
			fprintf(stderr, "[choice] f=%ld n=%d pick=%ld line=0x%X cur388=%u\n",
			        g_frameNo, ansUsed, pick, r32(0x03007C44), r32(0x02012F88));
			++ansUsed;
			int done = 0;
			if (pick < 0) {
				runFrames(45, 0);
				runFrames(4, 1u << GBA_KEY_A);
				runFrames(12, 0);
				done = 1;
			}
			/* 先按"选项/存档提示"的答案处理（0x02012F88）；
			 * 只有它不成立时才当作推理候选表去够目标（0x02013340）。 */
			if (!done) done = navMenuTry(0x02012F88, pick, 12);
			if (!done && wantInfer) done = navMenuTry(0x02013340, inferTarget, 12);
			if (!done) {   /* 兜底：至少把提示推过去 */
				runFrames(45, 0);
				runFrames(4, 1u << GBA_KEY_A);
				runFrames(12, 0);
			}
			runFrames(200, 0);
			continue;
		}

		/* 指令菜单：uiflag=2 且按一下 DOWN 光标真的会动（自动演出里是死的） */
		if (uif == 2 && nact) {
			uint32_t c0 = r32(0x0200DF94);
			runFrames(4, 1u << GBA_KEY_DOWN);
			runFrames(26, 0);
			if (r32(0x0200DF94) != c0) {
				uint32_t target = (uint32_t) actv[used % nact];
				long s;
				for (s = 0; s < 8 && r32(0x0200DF94) != target; ++s) {
					runFrames(4, 1u << GBA_KEY_DOWN);
					runFrames(26, 0);
				}
				if (r32(0x0200DF94) == target) {
					runFrames(45, 0);
					runFrames(4, 1u << GBA_KEY_A);
					runFrames(12, 0);
					++used;
					runFrames(150, 0);
					continue;
				}
				fprintf(stderr, "[drive] f=%ld menu cur=%u, target %u unreachable\n",
				        g_frameNo, r32(0x0200DF94), target);
			}
		}

		/* 推一页对白（长对白只认按住 A），然后按 1 帧粒度盯 500 帧：
		 * 推理题的候选窗口只有 ~150 帧，还出现在"剧本非 live"的装载瞬间。 */
		runFrames(40, 1u << GBA_KEY_A);
		for (int k = 0; k < 500 && g_frameNo - start < budget; ++k) {
			if (r32(0x02015160) == 4) break;   /* 交给外层处理 */
			if (wantInfer) {
				uint32_t ic = r32(0x02013340);
				if (ic == 0) infArmed = 1;
				if (infArmed && ic < 64 && !engineLive()) {
					/* 候选表要按方向键才动，而且"醒着"的窗口只有一两百帧：
					 * 这里每帧看一次，按 5 帧一档往目标推，落到目标立刻 A。 */
					int hit = 0;
					for (int w = 0; w < 900; ++w) {
						if (r32(0x02015160) == 4) break;
						uint32_t cur = r32(0x02013340);
						if (cur == (uint32_t) inferTarget) {
							runFrames(4, 1u << GBA_KEY_A);
							hit = 1;
							break;
						}
						if (cur < 64) {
							int key = (cur > (uint32_t) inferTarget) ? GBA_KEY_UP : GBA_KEY_DOWN;
							runFrames(4, 1u << key);
						}
						runFrames(1, 0);
					}
					fprintf(stderr, "[infer] f=%ld attempt=%ld target=%ld %s (cur=%u)\n",
					        g_frameNo, infTry, inferTarget, hit ? "HIT" : "miss",
					        r32(0x02013340));
					runFrames(120, 0);
					infArmed = 0;
					++infTry;
					if (hit && infUsed < ninf) ++infUsed;
					inferTarget = (infUsed < ninf) ? infq[infUsed] : inferIdx;
					break;
				}
			}
			runFrames(1, 0);
		}
	}
	fprintf(stderr, "[drive] f=%ld actions=%d inferAttempts=%ld\n", g_frameNo, used, infTry);
}

/* expect_entry <N>: engine must be live and on dispatch entry N. */
static void expectEntry(long want) {
	int got;
	if (!waitLiveFrames(600)) timeoutFail("expect_entry: engine never went live");
	got = currentEntryId();
	if (got != want) {
		char path[1024];
		fprintf(stderr, "[expect] FAIL f=%ld want=%ld got=%d\n", g_frameNo, want, got);
		shotPath("_expect_fail", path, sizeof(path));
		writePNG(path);
		fprintf(stderr, "[shot] f=%ld %s\n", g_frameNo, path);
		dumpMemory("_expect_fail");
		exit(4);
	}
	fprintf(stderr, "[ok] entry %ld\n", want);
}

/* --------------------------------------------------------- script loading -- */
struct scriptLine { char* text; };
struct label { char name[64]; long pc; };
static struct scriptLine* g_lines;
static long g_nlines;
static struct label g_labels[256];
static int g_nlabels;

static void loadScript(FILE* f) {
	char buf[512];
	long cap = 64;
	g_lines = malloc(cap * sizeof(*g_lines));
	while (fgets(buf, sizeof(buf), f)) {
		if (g_nlines == cap) {
			cap *= 2;
			g_lines = realloc(g_lines, cap * sizeof(*g_lines));
		}
		g_lines[g_nlines].text = strdup(buf);
		/* first pass: collect labels so forward gotos resolve */
		{
			char tmp[512];
			char cmd[64] = {0}, name[64] = {0};
			char* hash;
			strcpy(tmp, buf);
			hash = strchr(tmp, '#');
			if (hash) *hash = '\0';
			if (sscanf(tmp, "%63s %63s", cmd, name) == 2 && !strcmp(cmd, "label")) {
				if (g_nlabels < 256) {
					snprintf(g_labels[g_nlabels].name, sizeof(g_labels[0].name), "%s", name);
					g_labels[g_nlabels].pc = g_nlines;
					++g_nlabels;
				}
			}
		}
		++g_nlines;
	}
}

static long findLabel(const char* name) {
	for (int i = 0; i < g_nlabels; ++i)
		if (!strcmp(g_labels[i].name, name)) return g_labels[i].pc;
	fprintf(stderr, "[script] unknown label: %s\n", name);
	exit(1);
}

int main(int argc, char** argv) {
	if (argc < 3) {
		fprintf(stderr, "usage: %s <rom> <script> [outdir]\n", argv[0]);
		return 2;
	}
	const char* romPath = argv[1];
	const char* scriptPath = argv[2];
	if (argc > 3) g_outdir = argv[3];

	FILE* script = fopen(scriptPath, "r");
	if (!script) { perror(scriptPath); return 1; }
	loadScript(script);
	fclose(script);
	loadGlyphMap(getenv("AUTOPLAY_GLYPHMAP") ?
	             getenv("AUTOPLAY_GLYPHMAP") : "data/glyph_map.csv");

	g_core = GBACoreCreate();
	if (!g_core) { fprintf(stderr, "failed to create GBA core\n"); return 1; }
	if (!getenv("AUTOPLAY_LOG")) mLogSetDefaultLogger(&g_quiet);
	g_core->init(g_core);
	mCoreInitConfig(g_core, NULL);
	if (!mCoreLoadFile(g_core, romPath)) {
		fprintf(stderr, "failed to load ROM: %s\n", romPath);
		return 1;
	}
	{
		const char* biosPath = getenv("GBARUN_BIOS");
		if (biosPath && *biosPath) {
			struct VFile* bv = VFileOpen(biosPath, O_RDONLY);
			if (bv && g_core->loadBIOS(g_core, bv, 0)) fprintf(stderr, "[bios] loaded\n");
		}
	}
	/* The save type has to be settled *before* the .sav is autoloaded: mGBA
	 * sizes the save buffer from the type, and forcing it afterwards
	 * reallocates the buffer and throws the loaded data away.  A game without
	 * a save-type signature in its header (this one included) therefore needs
	 * GBARUN_FORCE_SAVE=N (4 = EEPROM, 5 = EEPROM512, 1 = SRAM, ...). */
	{
		const char* force = getenv("GBARUN_FORCE_SAVE");
		if (force) {
			g_gba = g_core->board;
			GBASavedataForceType(&g_gba->memory.savedata, (enum SavedataType) atoi(force));
			fprintf(stderr, "[save] type forced to %s\n", force);
		}
	}
	if (!getenv("GBARUN_NO_AUTOSAVE") && mCoreAutoloadSave(g_core)) {
		fprintf(stderr, "[save] autoloaded\n");
	}

	g_gba = g_core->board;
	{
		struct GBAVideoSoftwareRenderer* r = calloc(1, sizeof(*r));
		GBAVideoSoftwareRendererCreate(r);
		r->outputBuffer = g_frame;
		r->outputBufferStride = WIDTH;
		g_sw = r;
		GBAVideoAssociateRenderer(&g_gba->video, &r->d);
	}
	g_core->reset(g_core);
	setTurbo(atoi(getenv("AUTOPLAY_FRAMESKIP") ? getenv("AUTOPLAY_FRAMESKIP") : "0"));

	{
		const char* st = getenv("GBARUN_STATE");
		if (st && *st) {
			if (loadState(st)) fprintf(stderr, "[state] loaded %s\n", st);
			else fprintf(stderr, "[state] FAILED %s\n", st);
			if (!g_turbo) {
				attachSoftware();
				forceRedraw();
			}
			setTurbo(g_frameskip);
		}
	}
	mkdir(g_outdir, 0755);

	int nshot = 0;
	long pc = 0;
	while (pc < g_nlines) {
		char line[512];
		char* hash;
		char cmd[64] = {0};
		char a1[256] = {0}, a2[256] = {0}, a3[256] = {0}, a4[256] = {0}, a5[256] = {0}, a6[256] = {0};
		long n1 = 0, n2 = 0;
		snprintf(line, sizeof(line), "%s", g_lines[pc].text);
		hash = strchr(line, '#');
		if (hash) *hash = '\0';
		int got = sscanf(line, "%63s %255s %255s %255s %255s %255s %255s", cmd, a1, a2, a3, a4, a5, a6);
		if (got < 1) { ++pc; continue; }
		if (got >= 2) n1 = strtol(a1, NULL, 0);
		if (got >= 3) n2 = strtol(a2, NULL, 0);

		if (!strcmp(cmd, "wait")) {
			runFrames(n1, 0);
		} else if (!strcmp(cmd, "hold")) {
			/* hold <KEY> <frames> -- 长按（有些菜单只认长按/连按） */
			uint32_t key = keyByName(a1);
			long n = got >= 3 ? n2 : 30;
			if (!key) { fprintf(stderr, "[hold] bad key\n"); return 1; }
			runFrames(n, key);
			runFrames(10, 0);
		} else if (!strcmp(cmd, "press")) {
			int k = keyByName(a1);
			long hold = got >= 2 && n1 > 0 ? n1 : 4;
			long gap = got >= 3 ? n2 : 12;
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); ++pc; continue; }
			runFrames(hold, 1u << k);
			runFrames(gap, 0);
		} else if (!strcmp(cmd, "hold")) {
			int k = keyByName(a1);
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); ++pc; continue; }
			runFrames(n1 > 0 ? n1 : 4, 1u << k);
		} else if (!strcmp(cmd, "mash")) {
			int k = keyByName(a1);
			long times = n1 > 0 ? n1 : 1;
			long gap = got >= 3 ? n2 : 20;
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); ++pc; continue; }
			for (long i = 0; i < times; ++i) {
				runFrames(4, 1u << k);
				runFrames(gap, 0);
			}
		} else if (!strcmp(cmd, "shot")) {
			char path[1024];
			if (got < 2) { snprintf(a1, sizeof(a1), "shot%03d", nshot); }
			shotPath(a1, path, sizeof(path));
			writePNG(path);
			++nshot;
			fprintf(stderr, "[shot] f=%ld %s\n", g_frameNo, path);
		} else if (!strcmp(cmd, "mem")) {
			dumpMemory(got >= 2 ? a1 : "mem");
		} else if (!strcmp(cmd, "poke")) {
			/* poke <addr> <value16> -- e.g. clear a DISPCNT layer bit to find
			 * out which layer actually draws something on screen. */
			uint32_t addr = (uint32_t) strtoul(a1, NULL, 0);
			GBAView16(g_gba->cpu, addr);
			{
				uint16_t* p = (uint16_t*) (g_gba->memory.io + ((addr - 0x04000000) >> 1));
				*p = (uint16_t) n2;
			}
			fprintf(stderr, "[poke] 0x%08X = 0x%04X\n", addr, (unsigned) n2);
		} else if (!strcmp(cmd, "save")) {
			fprintf(stderr, "[state] %s %s\n", saveState(a1) ? "wrote" : "FAILED", a1);
		} else if (!strcmp(cmd, "load")) {
			fprintf(stderr, "[state] %s %s\n", loadState(a1) ? "loaded" : "FAILED", a1);
		} else if (!strcmp(cmd, "msg")) {
			fprintf(stderr, "[msg] f=%ld %s %s\n", g_frameNo, a1, a2);
		} else if (!strcmp(cmd, "label")) {
			/* marker, collected in the first pass */
		} else if (!strcmp(cmd, "goto")) {
			pc = findLabel(a1);
			continue;
		} else if (!strcmp(cmd, "if_live") || !strcmp(cmd, "if_nlive")) {
			int live = engineLive();
			if ((!strcmp(cmd, "if_live")) == live) { pc = findLabel(a1); continue; }
		} else if (!strcmp(cmd, "if_eq") || !strcmp(cmd, "if_ne")) {
			uint32_t addr = (uint32_t) strtoul(a1, NULL, 0);
			uint32_t val = (uint32_t) strtoul(a2, NULL, 0);
			int eq = r32(addr) == val;
			if ((!strcmp(cmd, "if_eq")) == eq) { pc = findLabel(a3); continue; }
		} else if (!strcmp(cmd, "wait_eq") || !strcmp(cmd, "wait_ne")) {
			uint32_t addr = (uint32_t) strtoul(a1, NULL, 0);
			uint32_t val = (uint32_t) strtoul(a2, NULL, 0);
			long timeout = strtol(a3, NULL, 0);
			int mode = !strcmp(cmd, "wait_eq") ? 0 : 1;
			if (!waitMem(mode, addr, val, timeout > 0 ? timeout : 600))
				timeoutFail(cmd);
		} else if (!strcmp(cmd, "wait_chg")) {
			uint32_t addr = (uint32_t) strtoul(a1, NULL, 0);
			long timeout = strtol(a2, NULL, 0);
			if (!waitMem(2, addr, 0, timeout > 0 ? timeout : 600))
				timeoutFail(cmd);
		} else if (!strcmp(cmd, "wait_live")) {
			long timeout = n1 > 0 ? n1 : 600;
			if (!waitLiveFrames(timeout)) timeoutFail(cmd);
		} else if (!strcmp(cmd, "expect_entry")) {
			expectEntry(n1);
		} else if (!strcmp(cmd, "mash_stall")) {
			int k = keyByName(a1);
			long timeout = got >= 3 ? n2 : 3000;
			long chgWait = got >= 4 ? strtol(a3, NULL, 0) : 40;
			if (chgWait <= 0) chgWait = 40;
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); ++pc; continue; }
			mashStall(k, timeout, chgWait);
		} else if (!strcmp(cmd, "pick")) {
			for (long i = 0; i < 8; ++i) {
				runFrames(4, 1u << GBA_KEY_UP);
				runFrames(12, 0);
				runFrames(8, 0);
			}
			for (long i = 0; i < n1; ++i) {
				runFrames(4, 1u << GBA_KEY_DOWN);
				runFrames(12, 0);
				runFrames(8, 0);
			}
			runFrames(4, 1u << GBA_KEY_A);
			runFrames(12, 0);
		} else if (!strcmp(cmd, "nav")) {
			/* nav <targetIdx> [maxSteps] -- ring command menu (cursor @0x0200DF94) */
			navMenu(0x0200DF94, n1, got >= 3 ? n2 : 16);
		} else if (!strcmp(cmd, "navc")) {
			/* navc <targetIdx> [maxSteps] -- choice lists: save prompt,
			 * 移动 destination list, option menus (cursor @0x02012F88) */
			navMenu(0x02012F88, n1, got >= 3 ? n2 : 16);
		} else if (!strcmp(cmd, "navraw")) {
			/* navraw <targetIdx> [maxSteps] -- 菜单游标 @0x02012F88，
			 * 不要求 uiflag=4（搜查场景这类"无提示"菜单也能用） */
			if (!navListTry(0x02012F88, n1, got >= 3 ? n2 : 10, 0))
				fprintf(stderr, "[navraw] f=%ld no hit (cur=%u n=%u)\n",
				        g_frameNo, r32(0x02012F88), r32(0x02012FA0));
		} else if (!strcmp(cmd, "navsoft")) {
			/* navsoft <targetIdx> [maxSteps] -- non-fatal choice-prompt nav */
			navMenuTry(0x02012F88, n1, got >= 3 ? n2 : 8);
		} else if (!strcmp(cmd, "drive")) {
			/* drive <frames> <a1,a2,...> [choiceIdx] */
			char listbuf[128];
			snprintf(listbuf, sizeof(listbuf), "%s", a2);
			driveLoop(n1 > 0 ? n1 : 100000, listbuf,
			          got >= 4 ? (long) strtol(a3, NULL, 0) : 1,
			          got >= 5 ? (long) strtol(a4, NULL, 0) : -1,
			          got >= 6 ? a5 : NULL,
			          got >= 7 ? a6 : NULL);
		} else if (!strcmp(cmd, "navisoft")) {
			/* navisoft <targetIdx> [maxSteps] -- inference candidate list */
			navInferTry(n1, got >= 3 ? n2 : 24);
		} else if (!strcmp(cmd, "navcmd")) {
			/* navcmd <targetIdx> [maxSteps] -- command menu (@0x0200DF94) */
			navCmdMenu(n1, got >= 3 ? n2 : 8);
		} else if (!strcmp(cmd, "navi")) {
			/* navi <targetIdx> [maxSteps] -- INFERENCE candidate list
			 * (cursor @0x02013340) */
			navMenu(0x02013340, n1, got >= 3 ? n2 : 24);
		} else if (!strcmp(cmd, "log_lines")) {
			flushPendingLine();
			if (g_logFile) { fclose(g_logFile); g_logFile = NULL; }
			if (got >= 2 && strcmp(a1, "off")) {
				g_logFile = fopen(a1, "a");
				if (!g_logFile) perror(a1);
				else fprintf(stderr, "[log] lines -> %s\n", a1);
			}
			g_lastCur = 0xFFFFFFFF;
			g_lastLine = 0xFFFFFFFF;
		} else if (!strcmp(cmd, "okmenu")) {
			/* If a choice prompt (uiflag=4) is open, pick item 0 and press A
			 * (e.g. the search screen's "继续搜索" on the stop-search prompt).
			 * Does nothing when no prompt is open. */
			uint32_t uif = r32(0x02015160);
			if (uif == 4) navMenu(0x02012F88, 0, 16);
			else fprintf(stderr, "[okmenu] f=%ld no prompt (uiflag=%u)\n",
			             g_frameNo, uif);
		} else if (!strcmp(cmd, "if_frame_gt")) {
			/* if_frame_gt <frame> <label> -- jump once the frame budget is used */
			if (g_frameNo > n1) { pc = findLabel(a2); continue; }
		} else if (!strcmp(cmd, "navfast")) {
			navFast(a1[0] ? strtol(a1, NULL, 0) : 0);
		} else if (!strcmp(cmd, "skiprows")) {
			skipRows(a1[0] ? strtol(a1, NULL, 0) : 0);
		} else if (!strcmp(cmd, "gotorow")) {
			gotoRow(a1[0] ? strtol(a1, NULL, 0) : 0);
		} else if (!strcmp(cmd, "w32")) {
			w32((uint32_t) strtoul(a1, NULL, 0), (uint32_t) strtoul(a2, NULL, 0));
			fprintf(stderr, "[w32] f=%ld 0x%08lX = 0x%08lX\n", g_frameNo,
			        strtoul(a1, NULL, 0), strtoul(a2, NULL, 0));
		} else if (!strcmp(cmd, "r32")) {
			/* r32 <addr> [label] -- one-shot read, printed to stderr */
			uint32_t addr = (uint32_t) strtoul(a1, NULL, 0);
			fprintf(stderr, "[r32] f=%ld %s 0x%08X = 0x%08X (%u)\n",
			        g_frameNo, got >= 3 ? a2 : "", addr, r32(addr), r32(addr));
		} else if (!strcmp(cmd, "turbo")) {
			setTurbo((int) n1);
			fprintf(stderr, "[turbo] f=%ld frameskip=%d renderer=%s\n",
			        g_frameNo, g_frameskip, g_turbo ? "dummy" : "software");
		} else if (!strcmp(cmd, "shot_live")) {
			g_shotLiveDir[0] = '\0';
			if (got >= 2 && strcmp(a1, "off")) {
				snprintf(g_shotLiveDir, sizeof(g_shotLiveDir), "%s", a1);
				mkdir(g_shotLiveDir, 0755);
				fprintf(stderr, "[shot] live -> %s/f<frame>.png\n", g_shotLiveDir);
			}
			g_lastCur = 0xFFFFFFFF;
			g_lastLine = 0xFFFFFFFF;
		} else if (!strcmp(cmd, "end")) {
			break;
		} else {
			fprintf(stderr, "[script] unknown command: %s\n", cmd);
		}
		++pc;
	}

	{
		char path[1024];
		shotPath("_final", path, sizeof(path));
		writePNG(path);
		fprintf(stderr, "[done] frames=%ld final=%s\n", g_frameNo, path);
	}
	fprintf(stderr, "[stats] lines_logged=%ld stalls=%ld\n", g_linesLogged, g_stalls);
	{
		const char* sv = getenv("GBARUN_SAVE");
		if (sv && *sv) fprintf(stderr, "[state] %s %s\n", saveState(sv) ? "wrote" : "FAILED", sv);
	}
	{
		/* GBARUN_DUMP_SAVE=<file>: write the cartridge save memory out.  This is
		 * how a save the game itself wrote gets captured - mGBA only flushes a
		 * .sav through a frontend, and the harness has none. */
		const char* dp = getenv("GBARUN_DUMP_SAVE");
		if (dp && *dp) {
			struct GBASavedata* sd = &g_gba->memory.savedata;
			unsigned sz = (sd->type == SAVEDATA_EEPROM) ? 8192 :
			              (sd->type == SAVEDATA_FLASH512) ? 65536 :
			              (sd->type == SAVEDATA_FLASH1M) ? 131072 : 32768;
			FILE* f = fopen(dp, "wb");
			if (f && sd->data) {
				fwrite(sd->data, 1, sz, f);
				fprintf(stderr, "[save] dumped %u bytes (type %d) to %s\n", sz, sd->type, dp);
			} else {
				fprintf(stderr, "[save] dump FAILED (%s)\n", dp);
			}
			if (f) fclose(f);
		}
	}
	if (g_logFile) { flushPendingLine(); fclose(g_logFile); }
	g_core->deinit(g_core);
	return 0;
}
