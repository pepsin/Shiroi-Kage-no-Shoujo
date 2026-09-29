/* gbarun_dbg.c - headless mGBA debugger harness for the Jinguuji Saburou CN patch.
 *
 * Superset of gbarun_state (memory tracing, savestates, memory dumps) and
 * gbarun_shot (real software-rendered screenshots) plus the pieces a *debugger*
 * needs to work on the running game state:
 *
 *   * per-frame poke     GBARUN_POKE="frame:addr:hex; frame:addr:hex; ..."
 *                        (frame -1 = right after the savestate load / reset)
 *   * per-frame read     GBARUN_READ="frame:addr:len; ..."
 *   * byte watch         GBARUN_WATCH="addr:len[;addr:len]"  (print on change)
 *   * RAM search         GBARUN_SEARCH="hex[;hex]" over EWRAM+IWRAM (or a range)
 *   * file -> guest RAM  GBARUN_LOAD="frame:addr:path"
 *   * RAM snapshot       GBARUN_RAMSNAP="frame:prefix"   (EWRAM+IWRAM+VRAM only)
 *
 * Usage:
 *   gbarun_dbg <rom> <frames> <outprefix> [KEY@frame[:hold] ...] [dumpFrame ...]
 *
 * Keys: A B SELECT START RIGHT LEFT UP DOWN R L
 *
 * Environment (all optional):
 *   GBARUN_STATE=<state.ssN>   load an mGBA savestate first (PNG or raw)
 *   GBARUN_SAVE=<state.ssN>    write a savestate at the end
 *   GBARUN_SHOT=<file.ppm>     final screenshot (default <outprefix>_final.ppm)
 *   GBARUN_SHOT_EVERY=N        also write <outprefix>_shotNNNNNN.ppm every N frames
 *   GBARUN_BIOS=<bios.bin>     use a real BIOS
 *   GBARUN_FORCE_SAVE=N        force the cartridge save type
 *   GBARUN_NO_AUTOSAVE=1       do not autoload the .sav next to the ROM
 *   GBARUN_BIOSDUMP=<file>     dump the cartridge save RAM at the end
 *   GBARUN_LOGLOAD="lo:hi"     trace loads   (writes /tmp/gbarun_mem.txt)
 *   GBARUN_LOGSTORE="lo:hi"    trace stores
 *   GBARUN_LOGPC="lo:hi"       only log while PC is inside this range
 *   GBARUN_VERBOSE=1           more stderr chatter
 *
 * Build:
 *   clang -O2 -o work/bin/gbarun_dbg legacy/gbarun_dbg.c \
 *         -I/opt/homebrew/include -L/opt/homebrew/lib -lmgba -lm -lpthread
 */
#include <mgba/core/core.h>
#include <mgba/core/interface.h>
#include <mgba/core/serialize.h>
#include <mgba/gba/core.h>
#include <mgba/internal/gba/gba.h>
#include <mgba/internal/gba/memory.h>
#include <mgba/internal/gba/video.h>
#include <mgba/internal/gba/renderers/video-software.h>
#include <mgba/internal/gba/input.h>
#include <mgba/internal/gba/savedata.h>
#include <mgba/internal/arm/arm.h>
#include <mgba-util/vfs.h>

#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define WIDTH 240
#define HEIGHT 160

static color_t g_frame[WIDTH * HEIGHT];
static struct GBA* g_gba;
static struct mCore* g_core;

static int keyByName(const char* n) {
	if (!strcmp(n, "A")) return GBA_KEY_A;
	if (!strcmp(n, "B")) return GBA_KEY_B;
	if (!strcmp(n, "SELECT")) return GBA_KEY_SELECT;
	if (!strcmp(n, "START")) return GBA_KEY_START;
	if (!strcmp(n, "RIGHT")) return GBA_KEY_RIGHT;
	if (!strcmp(n, "LEFT")) return GBA_KEY_LEFT;
	if (!strcmp(n, "UP")) return GBA_KEY_UP;
	if (!strcmp(n, "DOWN")) return GBA_KEY_DOWN;
	if (!strcmp(n, "R")) return GBA_KEY_R;
	if (!strcmp(n, "L")) return GBA_KEY_L;
	return -1;
}

static void writePPM(const char* path) {
	FILE* f = fopen(path, "wb");
	if (!f) { perror("fopen ppm"); return; }
	fprintf(f, "P6\n%d %d\n255\n", WIDTH, HEIGHT);
	for (int i = 0; i < WIDTH * HEIGHT; ++i) {
		color_t c = g_frame[i];
		unsigned char rgb[3] = {
			(unsigned char) (c & 0xFF),
			(unsigned char) ((c >> 8) & 0xFF),
			(unsigned char) ((c >> 16) & 0xFF),
		};
		fwrite(rgb, 1, 3, f);
	}
	fclose(f);
}

/* ------------------------------------------------------------------ memory */

static unsigned char memRead8(uint32_t a) { return GBAView8(g_gba->cpu, a); }
static void memWrite8(uint32_t a, unsigned char v) {
	g_gba->cpu->memory.store8(g_gba->cpu, a, (int8_t) v, NULL);
}

static void hexDump(uint32_t addr, int len) {
	printf("[read] 0x%08X len=%d:", addr, len);
	for (int i = 0; i < len; ++i) {
		if (i % 16 == 0) printf("\n        0x%08X ", addr + i);
		printf("%02X ", memRead8(addr + i));
	}
	printf("\n");
	fflush(stdout);
}

static int hexVal(int c) {
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

static void hexBytes(const char* h, unsigned char* out, int max, int* n) {
	int k = 0;
	while (h[0] && h[1] && k < max) {
		int hi = hexVal(h[0]), lo = hexVal(h[1]);
		if (hi < 0 || lo < 0) break;
		out[k++] = (unsigned char) ((hi << 4) | lo);
		h += 2;
	}
	*n = k;
}

/* --------------------------------------------------------------- scripted I/O
 * GBARUN_POKE / GBARUN_READ / GBARUN_LOAD are semicolon separated lists of
 * "frame:..." entries; frame -1 means "right now" (before the first frame).
 */
struct action {
	int kind;              /* 0 = poke, 1 = read, 2 = load-file */
	long frame;
	uint32_t addr;
	int len;
	unsigned char* data;   /* poke payload */
	char* path;            /* load-file source */
	struct action* next;
};

static struct action* g_actions;
static struct action* g_lastAction;

static struct action* addAction(int kind, long frame) {
	struct action* a = calloc(1, sizeof(*a));
	a->kind = kind;
	a->frame = frame;
	if (!g_actions) g_actions = a; else g_lastAction->next = a;
	g_lastAction = a;
	return a;
}

static void parsePokeList(const char* spec) {
	char buf[8192];
	snprintf(buf, sizeof(buf), "%s", spec);
	for (char* tok = strtok(buf, ";"); tok; tok = strtok(NULL, ";")) {
		while (*tok == ' ') ++tok;
		char* c1 = strchr(tok, ':');
		if (!c1) continue;
		*c1 = 0;
		char* c2 = strchr(c1 + 1, ':');
		if (!c2) continue;
		*c2 = 0;
		long frame = strtol(tok, NULL, 0);
		uint32_t addr = (uint32_t) strtoul(c1 + 1, NULL, 0);
		unsigned char tmp[2048];
		int n = 0;
		hexBytes(c2 + 1, tmp, (int) sizeof(tmp), &n);
		if (!n) continue;
		/* copy the payload out of the token buffer (buf is reused) */
		unsigned char* payload = malloc(n);
		memcpy(payload, tmp, n);
		struct action* a = addAction(0, frame);
		a->addr = addr;
		a->len = n;
		a->data = payload;
	}
}

static void parseSimpleList(const char* spec, int kind) {
	char buf[8192];
	snprintf(buf, sizeof(buf), "%s", spec);
	for (char* tok = strtok(buf, ";"); tok; tok = strtok(NULL, ";")) {
		while (*tok == ' ') ++tok;
		char* c1 = strchr(tok, ':');
		if (!c1) continue;
		*c1 = 0;
		char* rest = c1 + 1;
		char* c2 = strchr(rest, ':');
		if (kind == 2) {
			if (!c2) continue;
			*c2 = 0;
		}
		long frame = strtol(tok, NULL, 0);
		uint32_t addr = (uint32_t) strtoul(rest, NULL, 0);
		struct action* a = addAction(kind, frame);
		a->addr = addr;
		if (kind == 1) {
			a->len = c2 ? atoi(c2 + 1) : 16;
		} else {
			a->path = strdup(c2 ? c2 + 1 : "");
		}
	}
}

/* ------------------------------------------------------------------- calls
 * GBARUN_CALL="frame:addr:r0,r1,r2,r3[; ...]" - set PC to addr (Thumb bit in the
 * low bit of addr is honoured) with the given arguments and LR pointing at a
 * "trap" (an infinite `b .` in EWRAM).  The full CPU context is restored at the
 * start of the next frame once the PC has landed on the trap, so this is a safe
 * way to invoke an engine routine (e.g. the script loader) from the harness.
 */
#define MAXCALL 16
struct callrec { long frame; uint32_t addr; uint32_t args[4]; int used; };
static struct callrec g_calls[MAXCALL];
static int g_nCalls;
static int g_callActive;
static uint32_t g_callSavedGprs[16];
static uint32_t g_callSavedCpsr;
static uint32_t g_callSavedPrefetch[2];
static uint32_t g_callTrap = 0x0203FF00;
static long g_callFrame = -1;

static void addCall(const char* spec) {
	char buf[2048];
	snprintf(buf, sizeof(buf), "%s", spec);
	for (char* tok = strtok(buf, ";"); tok && g_nCalls < MAXCALL; tok = strtok(NULL, ";")) {
		while (*tok == ' ') ++tok;
		char* c1 = strchr(tok, ':');
		if (!c1) continue;
		*c1 = 0;
		char* c2 = strchr(c1 + 1, ':');
		if (!c2) continue;
		*c2 = 0;
		struct callrec* r = &g_calls[g_nCalls];
		r->frame = strtol(tok, NULL, 0);
		r->addr = (uint32_t) strtoul(c1 + 1, NULL, 0);
		char* a = c2 + 1;
		for (int i = 0; i < 4 && a && *a; ++i) {
			r->args[i] = (uint32_t) strtoul(a, &a, 0);
			if (*a == ',') ++a;
		}
		r->used = 1;
		++g_nCalls;
	}
}

/* Returns 1 if something was started. */
static int maybeStartCall(long frame) {
	for (int i = 0; i < g_nCalls; ++i) {
		if (!g_calls[i].used || g_calls[i].frame != frame) continue;
		struct ARMCore* cpu = g_gba->cpu;
		for (int k = 0; k < 16; ++k) g_callSavedGprs[k] = cpu->gprs[k];
		g_callSavedCpsr = cpu->cpsr.packed;
		g_callSavedPrefetch[0] = cpu->prefetch[0];
		g_callSavedPrefetch[1] = cpu->prefetch[1];
		/* trap: b . (0xE7FE) */
		g_gba->cpu->memory.store16(cpu, g_callTrap, (int16_t) 0xE7FE, NULL);
		uint32_t entry = g_calls[i].addr;
		int thumb = (entry & 1) != 0;
		entry &= 0xFFFFFFFE;
		for (int k = 0; k < 4; ++k) cpu->gprs[k] = g_calls[i].args[k];
		cpu->gprs[14] = g_callTrap;
		cpu->cpsr.t = thumb ? 1 : 0;
		/* mGBA keeps the *next* instruction in prefetch[0]; a raw PC write has
		 * to refill it or the core executes stale code. */
		cpu->gprs[15] = entry;
		if (thumb) {
			cpu->prefetch[0] = cpu->memory.load16(cpu, entry, NULL);
			cpu->prefetch[1] = cpu->memory.load16(cpu, entry + 2, NULL);
		} else {
			cpu->prefetch[0] = cpu->memory.load32(cpu, entry, NULL);
			cpu->prefetch[1] = cpu->memory.load32(cpu, entry + 4, NULL);
		}
		g_calls[i].used = 0;
		g_callActive = 1;
		g_callFrame = frame;
		printf("[call] f=%ld -> 0x%08X(%u,%u,%u,%u)\n", frame, entry,
		       g_calls[i].args[0], g_calls[i].args[1], g_calls[i].args[2], g_calls[i].args[3]);
		fflush(stdout);
		return 1;
	}
	return 0;
}

static void maybeFinishCall(long frame) {
	if (!g_callActive) return;
	struct ARMCore* cpu = g_gba->cpu;
	if ((cpu->gprs[15] & 0xFFFFFFFE) != g_callTrap) {
		printf("[call] f=%ld still running (PC=0x%08X), not restoring\n", frame, cpu->gprs[15]);
		fflush(stdout);
		return;
	}
	for (int k = 0; k < 16; ++k) cpu->gprs[k] = g_callSavedGprs[k];
	cpu->cpsr.packed = g_callSavedCpsr;
	cpu->prefetch[0] = g_callSavedPrefetch[0];
	cpu->prefetch[1] = g_callSavedPrefetch[1];
	cpu->memory.store16(cpu, g_callTrap, (int16_t) 0, NULL);
	g_callActive = 0;
	printf("[call] f=%ld returned, context restored\n", frame);
	fflush(stdout);
}

/* --------------------------------------------------------------- byte watch */

#define MAXWATCH 32
static struct { uint32_t addr; int len; unsigned char* prev; int used; } g_watch[MAXWATCH];
static int g_nWatch;

static void addWatch(const char* spec) {
	char buf[1024];
	snprintf(buf, sizeof(buf), "%s", spec);
	for (char* tok = strtok(buf, ";"); tok && g_nWatch < MAXWATCH; tok = strtok(NULL, ";")) {
		while (*tok == ' ') ++tok;
		char* c = strchr(tok, ':');
		uint32_t addr = (uint32_t) strtoul(tok, NULL, 0);
		int len = c ? atoi(c + 1) : 2;
		if (len <= 0 || len > 256) len = 2;
		g_watch[g_nWatch].addr = addr;
		g_watch[g_nWatch].len = len;
		g_watch[g_nWatch].prev = malloc(len);
		memset(g_watch[g_nWatch].prev, 0xEE, len);
		g_watch[g_nWatch].used = 1;
		++g_nWatch;
	}
}

static void checkWatches(long frame) {
	for (int i = 0; i < g_nWatch; ++i) {
		unsigned char cur[256];
		for (int k = 0; k < g_watch[i].len; ++k) cur[k] = memRead8(g_watch[i].addr + k);
		if (memcmp(cur, g_watch[i].prev, g_watch[i].len) != 0) {
			printf("[watch] f=%ld 0x%08X:", frame, g_watch[i].addr);
			for (int k = 0; k < g_watch[i].len; ++k) printf(" %02X", cur[k]);
			printf("   (was:");
			for (int k = 0; k < g_watch[i].len; ++k) printf(" %02X", g_watch[i].prev[k]);
			printf(")\n");
			fflush(stdout);
			memcpy(g_watch[i].prev, cur, g_watch[i].len);
		}
	}
}

/* -------------------------------------------------------------- RAM search */

#define MAXPAT 16
static int g_nPat;
static unsigned char g_pat[MAXPAT][64];
static int g_patLen[MAXPAT];
static const char* g_patName[MAXPAT];
static uint32_t g_searchLo = 0x02000000, g_searchHi = 0x02040000;
static long g_searchEvery = 1;
static int g_searchStop;
static int g_searchUsed;

static void addSearch(const char* spec) {
	char buf[4096];
	snprintf(buf, sizeof(buf), "%s", spec);
	for (char* tok = strtok(buf, ";"); tok && g_nPat < MAXPAT; tok = strtok(NULL, ";")) {
		while (*tok == ' ') ++tok;
		if (!*tok) continue;
		int n = 0;
		hexBytes(tok, g_pat[g_nPat], 64, &n);
		if (!n) continue;
		g_patLen[g_nPat] = n;
		g_patName[g_nPat] = strdup(tok);
		++g_nPat;
	}
}

static void runSearch(long frame) {
	if (!g_nPat) return;
	if (g_searchEvery > 1 && (frame % g_searchEvery) != 0) return;
	for (int p = 0; p < g_nPat; ++p) {
		for (uint32_t a = g_searchLo; a + (uint32_t) g_patLen[p] <= g_searchHi; ++a) {
			int ok = 1;
			for (int k = 0; k < g_patLen[p]; ++k) {
				if (memRead8(a + k) != g_pat[p][k]) { ok = 0; break; }
			}
			if (ok) {
				printf("[search] f=%ld pat=%s hit=0x%08X\n", frame, g_patName[p], a);
				fflush(stdout);
				g_searchUsed = 1;
				if (g_searchStop) {
					printf("[search] stop requested\n");
					fflush(stdout);
					exit(0);
				}
			}
		}
	}
}

/* ------------------------------------------------------------ RAM snapshots */

static void dumpRAM(const char* prefix) {
	static const struct { const char* n; uint32_t a; uint32_t s; } R[] = {
		{ "ewram", 0x02000000, 0x40000 },
		{ "iwram", 0x03000000, 0x8000 },
		{ "vram",  0x06000000, 0x18000 },
	};
	for (size_t i = 0; i < sizeof(R) / sizeof(R[0]); ++i) {
		char path[1024];
		snprintf(path, sizeof(path), "%s.%s", prefix, R[i].n);
		FILE* f = fopen(path, "wb");
		if (!f) { perror("fopen ramsnap"); continue; }
		unsigned char chunk[4096];
		for (uint32_t a = 0; a < R[i].s; a += sizeof(chunk)) {
			uint32_t n = R[i].s - a < sizeof(chunk) ? R[i].s - a : sizeof(chunk);
			for (uint32_t k = 0; k < n; ++k) chunk[k] = memRead8(R[i].a + a + k);
			fwrite(chunk, 1, n, f);
		}
		fclose(f);
		printf("[ramsnap] wrote %s (%s)\n", path, R[i].n);
	}
	fflush(stdout);
}

/* --------------------------------------------------------- full memory dump */

static const struct { const char* name; uint32_t start; uint32_t size; } REGIONS[] = {
	{ "bios",    0x00000000, 0x4000 },
	{ "ewram",   0x02000000, 0x40000 },
	{ "iwram",   0x03000000, 0x8000 },
	{ "io",      0x04000000, 0x400 },
	{ "palette", 0x05000000, 0x400 },
	{ "vram",    0x06000000, 0x18000 },
	{ "oam",     0x07000000, 0x400 },
	{ "rom",     0x08000000, 0 },
};

static void dumpMemory(const char* prefix, long frame) {
	char path[1024], idxPath[1024], infoPath[1024];
	snprintf(path, sizeof(path), "%s_f%ld_mem.bin", prefix, frame);
	snprintf(idxPath, sizeof(idxPath), "%s_f%ld_mem.idx", prefix, frame);
	snprintf(infoPath, sizeof(infoPath), "%s_f%ld_info.txt", prefix, frame);
	FILE* fb = fopen(path, "wb");
	FILE* fi = fopen(idxPath, "wb");
	FILE* ft = fopen(infoPath, "wb");
	if (!fb || !fi || !ft) { perror("fopen dump"); return; }
	fprintf(fi, "# name start size fileoffset\n");
	unsigned long long offset = 0;
	for (size_t i = 0; i < sizeof(REGIONS) / sizeof(REGIONS[0]); ++i) {
		uint32_t size = REGIONS[i].size;
		if (size == 0) size = (uint32_t) g_core->romSize(g_core);
		fprintf(fi, "%s 0x%08X 0x%X 0x%llX\n", REGIONS[i].name, REGIONS[i].start, size, offset);
		for (uint32_t a = 0; a < size; ++a) {
			unsigned char b = memRead8(REGIONS[i].start + a);
			fwrite(&b, 1, 1, fb);
		}
		fprintf(ft, "region %-8s size=%u\n", REGIONS[i].name, size);
		offset += size;
	}
	fclose(fb); fclose(fi);
	fprintf(ft, "PC=0x%08X\n", g_gba->cpu->gprs[15]);
	fprintf(ft, "KEYINPUT=0x%04X DISPCNT=0x%04X\n",
	        GBAView16(g_gba->cpu, 0x04000130), GBAView16(g_gba->cpu, 0x04000000));
	fclose(ft);
	printf("[dump] wrote %s (+ .idx/.info)\n", path);
	fflush(stdout);
}

/* ------------------------------------------------------------- memory tracer */

static uint32_t g_logLoad0, g_logLoad1, g_logStore0, g_logStore1;
static uint32_t g_logPc0, g_logPc1;
static FILE* g_memLog;
static long g_frameNo;
static uint32_t (*orig_load32)(struct ARMCore*, uint32_t, int*);
static uint32_t (*orig_load16)(struct ARMCore*, uint32_t, int*);
static uint32_t (*orig_load8)(struct ARMCore*, uint32_t, int*);
static void (*orig_store32)(struct ARMCore*, uint32_t, int32_t, int*);
static void (*orig_store16)(struct ARMCore*, uint32_t, int16_t, int*);
static void (*orig_store8)(struct ARMCore*, uint32_t, int8_t, int*);
static uint32_t (*orig_loadMultiple)(struct ARMCore*, uint32_t, int, enum LSMDirection, int*);
static uint32_t (*orig_storeMultiple)(struct ARMCore*, uint32_t, int, enum LSMDirection, int*);

static int pcOk(struct ARMCore* cpu) {
	if (!g_logPc1) return 1;
	uint32_t pc = cpu->gprs[15] & 0xFFFFFFFE;
	return pc >= g_logPc0 && pc < g_logPc1;
}

static uint32_t wrap_load32(struct ARMCore* cpu, uint32_t a, int* c) {
	uint32_t v = orig_load32(cpu, a, c);
	if (g_memLog && a >= g_logLoad0 && a < g_logLoad1 && pcOk(cpu))
		fprintf(g_memLog, "LD32 a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        a, v, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	return v;
}
static uint32_t wrap_load16(struct ARMCore* cpu, uint32_t a, int* c) {
	uint32_t v = orig_load16(cpu, a, c);
	if (g_memLog && a >= g_logLoad0 && a < g_logLoad1 && pcOk(cpu))
		fprintf(g_memLog, "LD16 a=0x%08X v=0x%04X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        a, v, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	return v;
}
static uint32_t wrap_load8(struct ARMCore* cpu, uint32_t a, int* c) {
	uint32_t v = orig_load8(cpu, a, c);
	if (g_memLog && a >= g_logLoad0 && a < g_logLoad1 && pcOk(cpu))
		fprintf(g_memLog, "LD8  a=0x%08X v=0x%02X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        a, v, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	return v;
}
static void wrap_store32(struct ARMCore* cpu, uint32_t a, int32_t v, int* c) {
	if (g_memLog && a >= g_logStore0 && a < g_logStore1 && pcOk(cpu))
		fprintf(g_memLog, "ST32 a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        a, (uint32_t) v, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	orig_store32(cpu, a, v, c);
}
static void wrap_store16(struct ARMCore* cpu, uint32_t a, int16_t v, int* c) {
	if (g_memLog && a >= g_logStore0 && a < g_logStore1 && pcOk(cpu))
		fprintf(g_memLog, "ST16 a=0x%08X v=0x%04X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        a, (uint16_t) v, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	orig_store16(cpu, a, v, c);
}
static void wrap_store8(struct ARMCore* cpu, uint32_t a, int8_t v, int* c) {
	if (g_memLog && a >= g_logStore0 && a < g_logStore1 && pcOk(cpu))
		fprintf(g_memLog, "ST8  a=0x%08X v=0x%02X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        a, (uint8_t) v, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	orig_store8(cpu, a, v, c);
}
static uint32_t wrap_loadMultiple(struct ARMCore* cpu, uint32_t base, int mask,
                                  enum LSMDirection dir, int* c) {
	uint32_t v = orig_loadMultiple(cpu, base, mask, dir, c);
	if (g_memLog && pcOk(cpu)) {
		for (int i = 0; i < 16; ++i) {
			uint32_t a = base + 4 * i;
			if ((mask & (1 << i)) && a >= g_logLoad0 && a < g_logLoad1)
				fprintf(g_memLog, "LDM  a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X f=%ld\n",
				        a, orig_load32(cpu, a, c), cpu->gprs[15], cpu->gprs[14], g_frameNo);
		}
	}
	return v;
}
static uint32_t wrap_storeMultiple(struct ARMCore* cpu, uint32_t base, int mask,
                                   enum LSMDirection dir, int* c) {
	if (g_memLog && pcOk(cpu)) {
		for (int i = 0; i < 16; ++i) {
			uint32_t a = base + 4 * i;
			if ((mask & (1 << i)) && a >= g_logStore0 && a < g_logStore1)
				fprintf(g_memLog, "STM  a=0x%08X pc=0x%08X lr=0x%08X f=%ld\n",
				        a, cpu->gprs[15], cpu->gprs[14], g_frameNo);
		}
	}
	return orig_storeMultiple(cpu, base, mask, dir, c);
}

static void installTracer(struct GBA* g) {
	struct ARMMemory* m = &g->cpu->memory;
	orig_load32 = m->load32; orig_load16 = m->load16; orig_load8 = m->load8;
	orig_store32 = m->store32; orig_store16 = m->store16; orig_store8 = m->store8;
	orig_loadMultiple = m->loadMultiple; orig_storeMultiple = m->storeMultiple;
	m->load32 = wrap_load32; m->load16 = wrap_load16; m->load8 = wrap_load8;
	m->store32 = wrap_store32; m->store16 = wrap_store16; m->store8 = wrap_store8;
	m->loadMultiple = wrap_loadMultiple; m->storeMultiple = wrap_storeMultiple;
}

static void parseRange(const char* s, uint32_t* lo, uint32_t* hi) {
	*lo = strtoul(s, NULL, 0);
	const char* c = strchr(s, ':');
	*hi = c ? strtoul(c + 1, NULL, 0) : *lo + 1;
}

/* --------------------------------------------------------------- logging */

static FILE* g_logFile;
static struct mLogger logger;

static void logHandler(struct mLogger* log, int category, enum mLogLevel level,
                       const char* format, va_list args) {
	(void) log;
	if (!g_logFile) return;
	fprintf(g_logFile, "[cat=%d lvl=%d] ", category, level);
	vfprintf(g_logFile, format, args);
	fputc('\n', g_logFile);
}

/* ------------------------------------------------------------------- main */

int main(int argc, char** argv) {
	if (argc < 4) {
		fprintf(stderr, "usage: %s <rom> <frames> <outprefix> [KEY@frame[:hold] ...] [dumpFrame ...]\n", argv[0]);
		return 2;
	}
	const char* romPath = argv[1];
	long frames = strtol(argv[2], NULL, 0);
	const char* prefix = argv[3];
	const char* verbose = getenv("GBARUN_VERBOSE");

	g_logFile = fopen("/tmp/gbarun_dbg_log.txt", "wb");
	logger.log = logHandler;
	mLogSetDefaultLogger(&logger);

	const char* ll = getenv("GBARUN_LOGLOAD");
	const char* ls = getenv("GBARUN_LOGSTORE");
	const char* lp = getenv("GBARUN_LOGPC");
	if (ll || ls) {
		if (ll) parseRange(ll, &g_logLoad0, &g_logLoad1);
		if (ls) parseRange(ls, &g_logStore0, &g_logStore1);
		if (lp) parseRange(lp, &g_logPc0, &g_logPc1);
		g_memLog = fopen("/tmp/gbarun_mem.txt", "wb");
	}
	if (getenv("GBARUN_POKE")) parsePokeList(getenv("GBARUN_POKE"));
	if (getenv("GBARUN_READ")) parseSimpleList(getenv("GBARUN_READ"), 1);
	if (getenv("GBARUN_LOAD")) parseSimpleList(getenv("GBARUN_LOAD"), 2);
	if (getenv("GBARUN_WATCH")) addWatch(getenv("GBARUN_WATCH"));
	if (getenv("GBARUN_SEARCH")) addSearch(getenv("GBARUN_SEARCH"));
	if (getenv("GBARUN_CALL")) addCall(getenv("GBARUN_CALL"));
	{
		const char* r = getenv("GBARUN_SEARCH_RANGE");
		if (r) {
			const char* c = strchr(r, ':');
			g_searchLo = (uint32_t) strtoul(r, NULL, 0);
			g_searchHi = c ? (uint32_t) strtoul(c + 1, NULL, 0) : 0x03008000;
		}
		const char* e = getenv("GBARUN_SEARCH_EVERY");
		if (e) g_searchEvery = strtol(e, NULL, 0);
		if (getenv("GBARUN_SEARCH_STOP")) g_searchStop = 1;
	}

	struct mCore* core = GBACoreCreate();
	if (!core) { fprintf(stderr, "failed to create GBA core\n"); return 1; }
	g_core = core;
	core->init(core);
	mCoreInitConfig(core, NULL);
	if (!mCoreLoadFile(core, romPath)) {
		fprintf(stderr, "failed to load ROM: %s\n", romPath);
		return 1;
	}
	{
		const char* biosPath = getenv("GBARUN_BIOS");
		if (biosPath && *biosPath) {
			struct VFile* bv = VFileOpen(biosPath, O_RDONLY);
			if (bv && core->loadBIOS(core, bv, 0)) fprintf(stderr, "[bios] loaded %s\n", biosPath);
			else fprintf(stderr, "[bios] FAILED %s\n", biosPath);
		}
	}
	{
		const char* force = getenv("GBARUN_FORCE_SAVE");
		if (force) {
			struct GBA* g0 = core->board;
			GBASavedataForceType(&g0->memory.savedata, (enum SavedataType) atoi(force));
			fprintf(stderr, "[save] type forced to %s\n", force);
		}
	}
	if (!getenv("GBARUN_NO_AUTOSAVE") && mCoreAutoloadSave(core)) {
		if (verbose) fprintf(stderr, "[save] autoloaded\n");
	} else {
		if (verbose) fprintf(stderr, "[save] no save file loaded\n");
	}

	struct GBA* gba = core->board;
	g_gba = gba;
	struct GBAVideoSoftwareRenderer* renderer = calloc(1, sizeof(*renderer));
	GBAVideoSoftwareRendererCreate(renderer);
	renderer->outputBuffer = g_frame;
	renderer->outputBufferStride = WIDTH;
	GBAVideoAssociateRenderer(&gba->video, &renderer->d);

	core->reset(core);

	{
		const char* st = getenv("GBARUN_STATE");
		if (st && *st) {
			struct VFile* sf = VFileOpen(st, O_RDONLY);
			if (sf && mCoreLoadStateNamed(core, sf, SAVESTATE_ALL)) {
				fprintf(stderr, "[state] loaded %s\n", st);
			} else {
				fprintf(stderr, "[state] FAILED to load %s\n", st);
			}
			if (sf) sf->close(sf);
		}
	}
	if (getenv("GBARUN_WIPE_SAVE")) {
		struct GBASavedata* sd = &gba->memory.savedata;
		unsigned sz = (sd->type == SAVEDATA_EEPROM) ? 8192 :
		              (sd->type == SAVEDATA_FLASH512) ? 65536 :
		              (sd->type == SAVEDATA_FLASH1M) ? 131072 : 32768;
		if (sd->data) { memset(sd->data, 0xFF, sz); sd->dirty = 1; }
	}

	/* Re-announce palette/OAM after a state load (the renderer caches them). */
	{
		struct GBAVideoRenderer* r = gba->video.renderer;
		for (int i = 0; i < 512; ++i) r->writePalette(r, i * 2, gba->video.palette[i]);
		for (int i = 0; i < 128; ++i) r->writeOAM(r, i);
		renderer->outputBuffer = g_frame;
		renderer->outputBufferStride = WIDTH;
	}
	if (g_memLog) installTracer(gba);

	/* actions scheduled for "now" (frame -1) run before the first frame */
	for (struct action* a = g_actions; a; a = a->next) {
		if (a->frame != -1) continue;
		if (a->kind == 0) {
			for (int i = 0; i < a->len; ++i) memWrite8(a->addr + i, a->data[i]);
			printf("[poke] init 0x%08X <- %d bytes\n", a->addr, a->len);
		} else if (a->kind == 1) {
			hexDump(a->addr, a->len);
		}
	}
	fflush(stdout);

	/* input script */
	struct keyevent { int key; long frame; long count; } events[4096];
	int nEvents = 0;
	for (int i = 4; i < argc && nEvents < 4096; ++i) {
		const char* at = strchr(argv[i], '@');
		if (!at) continue;
		char name[16];
		size_t nl = (size_t) (at - argv[i]);
		if (nl >= sizeof(name)) continue;
		memcpy(name, argv[i], nl);
		name[nl] = '\0';
		long fr = strtol(at + 1, NULL, 0);
		long cnt = 4;
		const char* colon = strchr(at + 1, ':');
		if (colon) cnt = strtol(colon + 1, NULL, 0);
		int k = keyByName(name);
		if (k >= 0) {
			events[nEvents].key = k;
			events[nEvents].frame = fr;
			events[nEvents].count = cnt > 0 ? cnt : 1;
			++nEvents;
		}
	}
	const char* keyScript = getenv("GBARUN_KEYS");
	long shotEvery = 0;
	{
		const char* se = getenv("GBARUN_SHOT_EVERY");
		if (se) shotEvery = strtol(se, NULL, 0);
	}

	uint32_t held = 0;
	for (long f = 0; f < frames; ++f) {
		g_frameNo = f;
		maybeFinishCall(f);
		maybeStartCall(f);
		/* scheduled actions */
		for (struct action* a = g_actions; a; a = a->next) {
			if (a->frame != f) continue;
			if (a->kind == 0) {
				for (int i = 0; i < a->len; ++i) memWrite8(a->addr + i, a->data[i]);
				printf("[poke] f=%ld 0x%08X <- %d bytes\n", f, a->addr, a->len);
			} else if (a->kind == 1) {
				hexDump(a->addr, a->len);
			} else if (a->kind == 2) {
				FILE* fp = fopen(a->path, "rb");
				if (fp) {
					unsigned char buf[65536];
					size_t n = fread(buf, 1, sizeof(buf), fp);
					for (size_t i = 0; i < n; ++i) memWrite8(a->addr + (uint32_t) i, buf[i]);
					printf("[load] f=%ld 0x%08X <- %zu bytes from %s\n", f, a->addr, n, a->path);
					fclose(fp);
				} else {
					printf("[load] f=%ld FAILED to open %s\n", f, a->path);
				}
			}
			fflush(stdout);
		}

		uint32_t keys = 0;
		for (int e = 0; e < nEvents; ++e) {
			if (f >= events[e].frame && f < events[e].frame + events[e].count) keys |= 1u << events[e].key;
		}
		if (keyScript) {
			for (const char* q = keyScript; *q; ) {
				long kf = strtol(q, (char**) &q, 0);
				if (*q != ':') break;
				++q;
				char kn[16]; int ki = 0;
				while (*q && *q != ':' && ki < 15) kn[ki++] = *q++;
				kn[ki] = 0;
				long kh = 4;
				if (*q == ':') { ++q; kh = strtol(q, (char**) &q, 0); }
				int kk = keyByName(kn);
				if (kk >= 0 && f >= kf && f < kf + kh) keys |= 1u << kk;
				if (*q == ';') ++q; else if (*q) break;
			}
		}
		if (keys != held) { held = keys; core->setKeys(core, held); }

		core->runFrame(core);

		if (shotEvery > 0 && f > 0 && (f % shotEvery) == 0) {
			char p[1024];
			snprintf(p, sizeof(p), "%s_shot%06ld.ppm", prefix, f);
			writePPM(p);
		}
		checkWatches(f);
		runSearch(f);

		char key[32];
		snprintf(key, sizeof(key), "%ld", f);
		for (int i = 4; i < argc; ++i) {
			if (strcmp(argv[i], key) == 0) dumpMemory(prefix, f);
		}
		{
			/* GBARUN_RAMSNAP="frame:prefix" */
			const char* rs = getenv("GBARUN_RAMSNAP");
			if (rs) {
				char buf[1024];
				snprintf(buf, sizeof(buf), "%s", rs);
				char* c = strchr(buf, ':');
				if (c) {
					*c = 0;
					if (strtol(buf, NULL, 0) == f) dumpRAM(c + 1);
				}
			}
		}
	}

	{
		char path[1024];
		const char* so = getenv("GBARUN_SHOT");
		snprintf(path, sizeof(path), "%s", so && *so ? so : "");
		if (!*path) snprintf(path, sizeof(path), "%s_final.ppm", prefix);
		writePPM(path);
		unsigned long nz = 0;
		for (int i = 0; i < WIDTH * HEIGHT; ++i) if (g_frame[i] & 0xFFFFFF) ++nz;
		printf("[shot] wrote %s (%lu non-black px)\n", path, nz);
	}
	{
		const char* dp = getenv("GBARUN_BIOSDUMP");
		if (dp && *dp) {
			struct GBASavedata* sd = &gba->memory.savedata;
			unsigned sz = (sd->type == SAVEDATA_EEPROM) ? 8192 :
			              (sd->type == SAVEDATA_FLASH512) ? 65536 :
			              (sd->type == SAVEDATA_FLASH1M) ? 131072 : 32768;
			FILE* fp = fopen(dp, "wb");
			if (fp && sd->data) { fwrite(sd->data, 1, sz, fp); printf("[save] dumped %u bytes to %s\n", sz, dp); }
			if (fp) fclose(fp);
		}
	}
	{
		const char* sv = getenv("GBARUN_SAVE");
		if (sv && *sv) {
			struct VFile* vf = VFileOpen(sv, O_WRONLY | O_CREAT | O_TRUNC);
			if (vf && mCoreSaveStateNamed(core, vf, SAVESTATE_ALL)) printf("[state] wrote %s\n", sv);
			else printf("[state] FAILED to write %s\n", sv);
			if (vf) vf->close(vf);
		}
	}
	if (g_searchUsed && g_searchStop) { /* already exited */ }
	free(renderer);
	core->deinit(core);
	if (g_memLog) fclose(g_memLog);
	if (g_logFile) fclose(g_logFile);
	return 0;
}
