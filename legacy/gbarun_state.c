/* gbarun.c - headless mGBA harness for ROM analysis.
 *
 * Usage:
 *   gbarun <rom> <frames> <outprefix> [dumpFrame ...]
 *
 * Runs <frames> frames. For each listed dumpFrame writes:
 *   <prefix>_f<N>.ppm        screen (240x160)
 *   <prefix>_f<N>_mem.bin    concatenated memory regions (see .idx)
 *   <prefix>_f<N>_mem.idx    region index: name start size fileoffset
 *   <prefix>_f<N>_info.txt   video/CPU register state
 * Always writes <prefix>_final.ppm
 */
#include <mgba/core/serialize.h>
#include <mgba/core/core.h>
#include <mgba/core/interface.h>
#include <mgba/core/log.h>
#include <mgba/gba/core.h>
#include <mgba/internal/gba/gba.h>
#include <mgba/internal/gba/memory.h>
#include <mgba/internal/arm/arm.h>
#include <mgba/internal/gba/input.h>
#include <mgba/internal/gba/savedata.h>

#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <mgba-util/vfs.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

static struct mLogger logger;
static FILE* logFile;

static void logHandler(struct mLogger* log, int category, enum mLogLevel level,
                       const char* format, va_list args) {
	(void) log;
	if (!logFile) {
		return;
	}
	fprintf(logFile, "[cat=%d lvl=%d] ", category, level);
	vfprintf(logFile, format, args);
	fputc('\n', logFile);
	fflush(logFile);
}

/* forward decls for the VRAM write watchpoint defined at the bottom */
static struct GBA* g_watchGba;
static FILE* g_swiLog;
static const char* g_snapPrefix;
static unsigned long long g_swiRaiseCount;
static unsigned long long g_swiCount2;
static int g_tileWatch;
static FILE* g_tileLog;
static int g_keyWatch;
static long g_storeTrace0 = -1, g_storeTrace1 = -1;
static long g_frameNo;
static uint32_t g_romWatch0, g_romWatch1;
/* NOTE: reading guest memory from inside GBALoad* would recurse infinitely.
 * Guest memory must be read through the raw pointers instead. */
void dshLoadHook(uint32_t address, uint32_t pc) {
	if (g_keyWatch && (address & 0xFFFFFFFE) == 0x04000130 && g_tileLog) {
		fprintf(g_tileLog, "KEYREAD pc=0x%08X lr?\n", pc);
	}
	if (g_romWatch1 && address >= g_romWatch0 && address < g_romWatch1 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		fprintf(g_tileLog, "ROMRD a=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
		        address, pc, cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], g_frameNo);
	}
}

static void dumpWatch(const char* prefix);

static const char* g_keyScript;
static long g_shotEvery;

static int keyByName(const char* n) {
	if (!strcmp(n, "A")) return 0;
	if (!strcmp(n, "B")) return 1;
	if (!strcmp(n, "SELECT")) return 2;
	if (!strcmp(n, "START")) return 3;
	if (!strcmp(n, "RIGHT")) return 4;
	if (!strcmp(n, "LEFT")) return 5;
	if (!strcmp(n, "UP")) return 6;
	if (!strcmp(n, "DOWN")) return 7;
	if (!strcmp(n, "R")) return 8;
	if (!strcmp(n, "L")) return 9;
	return -1;
}

struct region {
	const char* name;
	uint32_t start;
	uint32_t size;
};

static const struct region REGIONS[] = {
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

static void dumpMemory(struct mCore* core, const char* prefix, long frame) {
	char path[1024], idxPath[1024], infoPath[1024];
	snprintf(path, sizeof(path), "%s_f%ld_mem.bin", prefix, frame);
	snprintf(idxPath, sizeof(idxPath), "%s_f%ld_mem.idx", prefix, frame);
	snprintf(infoPath, sizeof(infoPath), "%s_f%ld_info.txt", prefix, frame);
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
	struct GBA* gba = core->board;
	fprintf(fi, "# name start size fileoffset\n");
	unsigned long long offset = 0;
	for (size_t i = 0; i < NREGIONS; ++i) {
		uint32_t size = REGIONS[i].size;
		if (size == 0) {
			size = (uint32_t) core->romSize(core);
		}
		fprintf(fi, "%s 0x%08X 0x%X 0x%llX\n", REGIONS[i].name, REGIONS[i].start, size, offset);
		uint32_t nz = 0;
		for (uint32_t a = 0; a < size; ++a) {
			unsigned char b = GBAView8(gba->cpu, REGIONS[i].start + a);
			if (b) {
				++nz;
			}
			fwrite(&b, 1, 1, fb);
		}
		fprintf(ft, "region %-8s nonzero=%u / %u\n", REGIONS[i].name, nz, size);
		offset += size;
	}
	fclose(fb);
	fclose(fi);

	uint32_t pc = gba->cpu->gprs[15];
	fprintf(ft, "PC=0x%08X\n", pc);
	fprintf(ft, "KEYINPUT=0x%04X (0x3FF=no key)\n", GBAView16(gba->cpu, 0x04000130));
	fprintf(ft, "IE=0x%04X IF=0x%04X IME=%u\n", GBAView16(gba->cpu, 0x04000200), GBAView16(gba->cpu, 0x04000202), GBAView16(gba->cpu, 0x04000208));
	fprintf(ft, "DISPCNT=0x%04X\n", GBAView16(gba->cpu, 0x04000000));
	fprintf(ft, "DISPSTAT=0x%04X VCOUNT=%u\n", GBAView16(gba->cpu, 0x04000004), GBAView8(gba->cpu, 0x04000006));
	fprintf(ft, "BG0CNT=0x%04X BG1CNT=0x%04X BG2CNT=0x%04X BG3CNT=0x%04X\n",
	        GBAView16(gba->cpu, 0x04000008), GBAView16(gba->cpu, 0x0400000A),
	        GBAView16(gba->cpu, 0x0400000C), GBAView16(gba->cpu, 0x0400000E));
	fprintf(ft, "BLDCNT=0x%04X BLDALPHA=0x%04X BLDY=0x%04X\n",
	        GBAView16(gba->cpu, 0x04000050), GBAView16(gba->cpu, 0x04000052),
	        GBAView16(gba->cpu, 0x04000054));
	fclose(ft);
	printf("wrote %s + .idx + _info.txt\n", path);
}

static void writePPM(const char* path, const color_t* buf, unsigned w, unsigned h, size_t stride) {
	FILE* f = fopen(path, "wb");
	if (!f) {
		perror("fopen ppm");
		return;
	}
	fprintf(f, "P6\n%u %u\n255\n", w, h);
	for (unsigned y = 0; y < h; ++y) {
		for (unsigned x = 0; x < w; ++x) {
			color_t c = buf[y * stride + x];
			unsigned char rgb[3] = {
				(unsigned char) (c & 0xFF),
				(unsigned char) ((c >> 8) & 0xFF),
				(unsigned char) ((c >> 16) & 0xFF),
			};
			fwrite(rgb, 1, 3, f);
		}
	}
	fclose(f);
}

#define TRACE(...) do { fprintf(stderr, "[trace] " __VA_ARGS__); fprintf(stderr, "\n"); fflush(stderr); } while (0)

/* ---- memory access tracer ------------------------------------------------
 * ARMMemory exposes the CPU's load/store function pointers, so wrapping them
 * gives a full read/write trace.  Used to find where the text renderer gets
 * its per-glyph advance from.
 *   GBARUN_LOGLOAD="lo:hi"   log loads from that address range
 *   GBARUN_LOGSTORE="lo:hi"  log stores to that address range
 *   GBARUN_LOGPC="lo:hi"     only log while PC is inside that range
 */
static uint32_t g_logLoad0, g_logLoad1, g_logStore0, g_logStore1;
static uint32_t g_logPc0, g_logPc1;
static FILE* g_memLog;
static uint32_t (*orig_load32)(struct ARMCore*, uint32_t, int*);
static uint32_t (*orig_load16)(struct ARMCore*, uint32_t, int*);
static uint32_t (*orig_load8)(struct ARMCore*, uint32_t, int*);
static void (*orig_store32)(struct ARMCore*, uint32_t, int32_t, int*);
static void (*orig_store16)(struct ARMCore*, uint32_t, int16_t, int*);
static void (*orig_store8)(struct ARMCore*, uint32_t, int8_t, int*);
static uint32_t (*orig_loadMultiple)(struct ARMCore*, uint32_t, int, enum LSMDirection, int*);
static uint32_t (*orig_storeMultiple)(struct ARMCore*, uint32_t, int, enum LSMDirection, int*);

static int pcOk(struct ARMCore* cpu);

static uint32_t wrap_loadMultiple(struct ARMCore* cpu, uint32_t base, int mask,
                                  enum LSMDirection dir, int* c) {
	uint32_t v = orig_loadMultiple(cpu, base, mask, dir, c);
	if (g_memLog && pcOk(cpu)) {
		for (int i = 0; i < 16; ++i) {
			uint32_t a = base + 4 * i;
			if ((mask & (1 << i)) && a >= g_logLoad0 && a < g_logLoad1) {
				uint32_t val = orig_load32(cpu, a, c);
				fprintf(g_memLog, "LDM  a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
				        a, val, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1],
				        cpu->gprs[2], cpu->gprs[3], g_frameNo);
			}
		}
	}
	return v;
}
static uint32_t wrap_storeMultiple(struct ARMCore* cpu, uint32_t base, int mask,
                                   enum LSMDirection dir, int* c) {
	if (g_memLog && pcOk(cpu)) {
		for (int i = 0; i < 16; ++i) {
			uint32_t a = base + 4 * i;
			if ((mask & (1 << i)) && a >= g_logStore0 && a < g_logStore1) {
				fprintf(g_memLog, "STM  a=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X f=%ld\n",
				        a, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1],
				        cpu->gprs[2], cpu->gprs[3], g_frameNo);
			}
		}
	}
	return orig_storeMultiple(cpu, base, mask, dir, c);
}

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

int main(int argc, char** argv) {
	if (argc < 4) {
		fprintf(stderr, "usage: %s <rom> <frames> <outprefix> [dumpFrame ...]\n", argv[0]);
		return 2;
	}
	const char* romPath = argv[1];
	long frames = strtol(argv[2], NULL, 0);
	const char* prefix = argv[3];

	TRACE("start");
	logFile = fopen("/tmp/gbarun_log.txt", "wb");
	if (getenv("GBARUN_SWI")) {
		g_swiLog = fopen("/tmp/gbarun_swi.txt", "wb");
	}
	if (getenv("GBARUN_TILEWATCH")) {
		g_tileWatch = 1;
		g_tileLog = fopen("/tmp/gbarun_tile.txt", "wb");
	}
	{
		const char* rw = getenv("GBARUN_ROMWATCH");
		if (rw) {
			g_romWatch0 = strtoul(rw, NULL, 0);
			const char* c = strchr(rw, ':');
			g_romWatch1 = c ? strtoul(c + 1, NULL, 0) : g_romWatch0 + 0x1000;
			if (!g_tileLog) g_tileLog = fopen("/tmp/gbarun_tile.txt", "wb");
		}
	}
	{
		const char* st = getenv("GBARUN_STORETRACE");
		if (st) {
			g_storeTrace0 = strtol(st, NULL, 0);
			const char* c = strchr(st, ':');
			g_storeTrace1 = c ? strtol(c + 1, NULL, 0) : g_storeTrace0;
			if (!g_tileLog) g_tileLog = fopen("/tmp/gbarun_tile.txt", "wb");
		}
	}
	{
		const char* ll = getenv("GBARUN_LOGLOAD");
		const char* ls = getenv("GBARUN_LOGSTORE");
		const char* lp = getenv("GBARUN_LOGPC");
		if (ll || ls) {
			g_logLoad0 = g_logStore0 = 0;
			g_logLoad1 = g_logStore1 = 0;
			if (ll) parseRange(ll, &g_logLoad0, &g_logLoad1);
			if (ls) parseRange(ls, &g_logStore0, &g_logStore1);
			if (lp) parseRange(lp, &g_logPc0, &g_logPc1);
			g_memLog = fopen("/tmp/gbarun_mem.txt", "wb");
		}
	}
	if (getenv("GBARUN_KEYWATCH")) {
		g_keyWatch = 1;
		if (!g_tileLog) g_tileLog = fopen("/tmp/gbarun_tile.txt", "wb");
	}
	g_snapPrefix = getenv("GBARUN_SNAP");
	g_keyScript = getenv("GBARUN_KEYS");
	{
		const char* se = getenv("GBARUN_SHOTS");
		g_shotEvery = se ? strtol(se, NULL, 0) : 0;
	}
	logger.log = logHandler;
	mLogSetDefaultLogger(&logger);

	TRACE("creating core");
	struct mCore* core = GBACoreCreate();
	if (!core) {
		fprintf(stderr, "failed to create GBA core\n");
		return 1;
	}
	TRACE("core=%p", (void*)core);
	core->init(core);
	TRACE("inited");
	mCoreInitConfig(core, NULL);
	TRACE("config");
	if (!mCoreLoadFile(core, romPath)) {
		fprintf(stderr, "failed to load ROM: %s\n", romPath);
		return 1;
	}
	{
		const char* biosPath = getenv("GBARUN_BIOS");
		if (biosPath && *biosPath) {
			struct VFile* bv = VFileOpen(biosPath, O_RDONLY);
			if (bv && core->loadBIOS(core, bv, 0)) {
				fprintf(stderr, "[bios] loaded %s\n", biosPath);
			} else {
				fprintf(stderr, "[bios] FAILED to load %s\n", biosPath);
			}
			fflush(stderr);
		}
	}
	{
		struct GBA* g0 = core->board;
		fprintf(stderr, "[save] type=%d (%s)\n", g0->memory.savedata.type,
		        g0->memory.savedata.type == 0 ? "AUTO/none" : "detected");
		fflush(stderr);
		const char* force = getenv("GBARUN_FORCE_SAVE");
		if (force) {
			int t = atoi(force);
			GBASavedataForceType(&g0->memory.savedata, (enum SavedataType) t);
			fprintf(stderr, "[save] forced to %d\n", t);
			fflush(stderr);
		}
	}
	/* The frontends autoload the cartridge save after the ROM; without this
	 * the harness runs with a blank save and every save/load screen is empty. */
	if (getenv("GBARUN_NO_AUTOSAVE") || !mCoreAutoloadSave(core)) {
		fprintf(stderr, "[save] no save file loaded\n");
	}
	core->reset(core);
	TRACE("reset");
	{
		/* GBARUN_STATE=<file>: load an mGBA savestate (the player saves one in
		 * mGBA with Shift+F1..F9) so the analysis runs on the exact scene. */
		const char* st = getenv("GBARUN_STATE");
		if (st && *st) {
			struct VFile* sf = VFileOpen(st, O_RDONLY);
			/* mCoreLoadStateNamed handles the frontend's container formats
			 * (PNG-wrapped gbAs/gbAx chunks); core->loadState only accepts a
			 * bare GBASerializedState. */
			if (sf && mCoreLoadStateNamed(core, sf, SAVESTATE_ALL)) {
				fprintf(stderr, "[state] loaded %s\n", st);
			} else {
				if (sf) {
					sf->seek(sf, 0, SEEK_SET);
					if (core->loadState(core, sf)) {
						fprintf(stderr, "[state] loaded (raw) %s\n", st);
					} else {
						fprintf(stderr, "[state] FAILED to load %s\n", st);
					}
				} else {
					fprintf(stderr, "[state] FAILED to open %s\n", st);
				}
			}
			if (sf) sf->close(sf);
			fflush(stderr);
		}
	}
	{
		/* GBARUN_POKE="addr:hexbytes" - write into guest memory after the state
		 * load (used to re-point RAM-held ROM table pointers). */
		const char* pk = getenv("GBARUN_POKE");
		if (pk) {
			uint32_t addr = strtoul(pk, NULL, 0);
			const char* c = strchr(pk, ':');
			if (c) {
				const char* h = c + 1;
				int n = 0;
				while (h[0] && h[1] && n < 64) {
					char b[3] = { h[0], h[1], 0 };
					uint8_t v = (uint8_t) strtoul(b, NULL, 16);
					struct ARMCore* cpu = ((struct GBA*) core->board)->cpu;
					cpu->memory.store8(cpu, addr + n, v, NULL);
					h += 2; ++n;
				}
				fprintf(stderr, "[poke] wrote %d bytes at 0x%08X\n", n, addr);
			}
		}
	}
	if (g_memLog) {
		installTracer((struct GBA*) core->board);
		fprintf(stderr, "[trace] memory tracer installed\n");
	}

	g_watchGba = core->board;
	unsigned w = 0, h = 0;
	core->desiredVideoDimensions(core, &w, &h);
	size_t stride = w;
	color_t* vbuf = calloc(stride * h, sizeof(color_t));
	core->setVideoBuffer(core, vbuf, stride);
	TRACE("videobuf set stride=%zu", stride);
	printf("ROM loaded: video %ux%u size=%zu\n", w, h, core->romSize(core));
	fflush(stdout);

	if (getenv("GBARUN_SAMPLE")) {
		struct GBA* g = core->board;
		int nsamp = atoi(getenv("GBARUN_SAMPLE"));
		if (nsamp <= 0) nsamp = 500000;
		/* run some frames first */
		for (int f = 0; f < 30; ++f) core->runFrame(core);
		unsigned int hist[0x2000]; memset(hist, 0, sizeof(hist));
		for (int i = 0; i < nsamp; ++i) {
			core->step(core);
			uint32_t pc = g->cpu->gprs[15] & 0xFFFFFF;
			if (pc < 0x08000000) ++hist[(pc >> 8) & 0x1FFF];
		}
		/* also record the most recent 64 distinct PCs */
		{
			uint32_t rec[64]; int nrec = 0;
			for (int i = 0; i < 200000; ++i) {
				core->step(core);
				uint32_t pc = g->cpu->gprs[15];
				if (nrec == 0 || rec[nrec - 1] != pc) {
					if (nrec < 64) rec[nrec++] = pc;
					else { memmove(rec, rec + 1, sizeof(rec) - sizeof(rec[0])); rec[63] = pc; }
				}
			}
			fprintf(stderr, "[sample] last 64 distinct PCs:\n");
			for (int i = 0; i < nrec; ++i) fprintf(stderr, "   0x%08X\n", rec[i]);
		}
		fprintf(stderr, "[sample] PC page histogram (256-byte buckets, top 25):\n");
		for (int k = 0; k < 25; ++k) {
			int bi = -1; unsigned int bv = 0;
			for (int j = 0; j < 0x2000; ++j) if (hist[j] > bv) { bv = hist[j]; bi = j; }
			if (bi < 0 || !bv) break;
			fprintf(stderr, "   0x%06X  %u\n", bi << 8, bv);
			hist[bi] = 0;
		}
		fflush(stderr);
		core->deinit(core); return 0;
	}

	if (getenv("GBARUN_MICRO")) {
		struct GBA* g = core->board;
		uint32_t seen[4096]; int nseen = 0;
		uint32_t histories[64][8];
		long total = 0;
		int logged = 0;
		for (long i = 0; i < 30000000 && logged < 3; ++i) {
			core->step(core);
			++total;
			uint32_t pc = g->cpu->gprs[15];
			/* remember recent distinct PCs */
			if (nseen == 0 || seen[nseen - 1] != pc) {
				if (nseen < 4096) {
					seen[nseen++] = pc;
				} else {
					memmove(seen, seen + 1, sizeof(seen) - sizeof(seen[0]));
					seen[4095] = pc;
				}
			}
			/* detect a tight BIOS halt loop (PC repeatedly in 0x300..0x400) */
			if (pc >= 0x300 && pc < 0x400 && nseen > 600) {
				/* print the last 64 distinct PCs leading in */
				fprintf(stderr, "[micro] entered BIOS loop at step %ld, PC=0x%08X\n", total, pc);
				fprintf(stderr, "[micro] last 64 distinct PCs before:\n");
				int start = nseen - 64; if (start < 0) start = 0;
				for (int k = start; k < nseen; ++k) {
					fprintf(stderr, "   0x%08X\n", seen[k]);
				}
				++logged;
				break;
			}
		}
		fprintf(stderr, "[micro] done after %ld steps\n", total);
		fflush(stderr);
		core->deinit(core); return 0;
	}

	if (getenv("GBARUN_STEP")) {
		struct GBA* g = core->board;
		uint32_t lastPc = 0xFFFFFFFF;
		int log = 0;
		for (long i = 0; i < 20000000; ++i) {
			core->step(core);
			uint32_t pc = g->cpu->gprs[15];
			if (pc >= 0x00000000 && pc < 0x00004000) {
				if (log < 80) {
					fprintf(stderr, "[step %ld] PC=0x%08X R0=0x%08X R1=0x%08X R2=0x%08X R3=0x%08X LR=0x%08X SP=0x%08X CPSR=0x%08X\n",
					        i, pc, g->cpu->gprs[0], g->cpu->gprs[1], g->cpu->gprs[2], g->cpu->gprs[3],
					        g->cpu->gprs[14], g->cpu->gprs[13], g->cpu->cpsr.packed);
					++log;
				}
				if (pc == lastPc && pc >= 0x300 && pc < 0x400 && log >= 80) {
					fprintf(stderr, "[step] stuck at BIOS PC=0x%08X after %ld steps\n", pc, i);
					break;
				}
				lastPc = pc;
			}
		}
		fprintf(stderr, "[step] done\n");
		fflush(stderr);
		core->deinit(core); return 0;
	}

	const void* pxFinal = vbuf;
	size_t strideFinal = stride;
	char path[1024];

	/* Input script: args of the form NAME@FRAME (press for hold frames) or
	 * NAME@FRAME:COUNT. NAME is one of A,B,SELECT,START,RIGHT,LEFT,UP,DOWN,R,L.
	 * Keys are pressed at the START of the given frame and released COUNT frames
	 * later (default 4). */
	struct keyevent { int key; long frame; long count; };
	struct keyevent events[64];
	int nEvents = 0;
	for (int i = 4; i < argc; ++i) {
		const char* at = strchr(argv[i], '@');
		if (!at) {
			continue;
		}
		char name[16];
		size_t nl = (size_t) (at - argv[i]);
		if (nl >= sizeof(name)) {
			continue;
		}
		memcpy(name, argv[i], nl);
		name[nl] = '\0';
		long fr = strtol(at + 1, NULL, 0);
		long cnt = 4;
		const char* colon = strchr(at + 1, ':');
		if (colon) {
			cnt = strtol(colon + 1, NULL, 0);
		}
		int k = -1;
		if (!strcmp(name, "A")) k = GBA_KEY_A;
		else if (!strcmp(name, "B")) k = GBA_KEY_B;
		else if (!strcmp(name, "SELECT")) k = GBA_KEY_SELECT;
		else if (!strcmp(name, "START")) k = GBA_KEY_START;
		else if (!strcmp(name, "RIGHT")) k = GBA_KEY_RIGHT;
		else if (!strcmp(name, "LEFT")) k = GBA_KEY_LEFT;
		else if (!strcmp(name, "UP")) k = GBA_KEY_UP;
		else if (!strcmp(name, "DOWN")) k = GBA_KEY_DOWN;
		else if (!strcmp(name, "R")) k = GBA_KEY_R;
		else if (!strcmp(name, "L")) k = GBA_KEY_L;
		if (k >= 0 && nEvents < 64) {
			events[nEvents].key = k;
			events[nEvents].frame = fr;
			events[nEvents].count = cnt > 0 ? cnt : 1;
			++nEvents;
			TRACE("input: %s (key %d) at frame %ld for %ld", name, k, fr, cnt);
		}
	}
	uint32_t heldKeys = 0;

	for (long f = 0; f < frames; ++f) {
		g_frameNo = f;
		uint32_t frameKeys = 0;
		for (int e = 0; e < nEvents; ++e) {
			if (f >= events[e].frame && f < events[e].frame + events[e].count) {
				frameKeys |= 1u << events[e].key;
			}
		}
		/* Scripted key sequence: GBARUN_KEYS="frame:KEY:hold;frame:KEY:hold;..." */
		if (g_keyScript) {
			struct GBA* gg = core->board;
			for (const char* q = g_keyScript; *q; ) {
				long kf = strtol(q, (char**) &q, 0);
				if (*q != ':') break;
				++q;
				char kn[16]; int ki = 0;
				while (*q && *q != ':' && ki < 15) kn[ki++] = *q++;
				kn[ki] = 0;
				long kh = 4;
				if (*q == ':') { ++q; kh = strtol(q, (char**) &q, 0); }
				int kk = keyByName(kn);
				if (kk >= 0 && f >= kf && f < kf + kh) frameKeys |= 1u << kk;
				if (*q == ';') ++q; else if (*q) break;
				(void) gg;
			}
		}
		/* text detector: count distinct nonzero tilemap entries in the lower
		 * third of BG1/BG2 maps (where dialogue boxes live) */
		if (getenv("GBARUN_TEXTDET") && (f % 60) == 0) {
			struct GBA* gt = core->board;
			uint16_t dcnt = GBAView16(gt->cpu, 0x04000000);
			unsigned long uniq = 0, nz = 0;
			for (int bg = 0; bg < 4; ++bg) {
				if (!((dcnt >> (8 + bg)) & 1)) continue;
				uint16_t c = GBAView16(gt->cpu, 0x04000008 + 2 * bg);
				uint32_t sbase = 0x06000000 + ((c >> 8) & 0x1F) * 0x800;
				unsigned char seen[1024]; memset(seen, 0, sizeof(seen));
				for (uint32_t e = 20 * 32; e < 32 * 32; ++e) {
					uint16_t ent = GBAView16(gt->cpu, sbase + e * 2);
					if (!ent) continue;
					++nz;
					uint16_t t = ent & 0x3FF;
					if (t < 1024 && !seen[t]) { seen[t] = 1; ++uniq; }
				}
			}
			printf("[textdet] f=%ld DISPCNT=0x%04X distinct_tiles_lower=%lu nonzero=%lu\n", f, dcnt, uniq, nz);
			fflush(stdout);
		}
		if (frameKeys != heldKeys) {
			heldKeys = frameKeys;
			core->setKeys(core, heldKeys);
		}
		core->runFrame(core);

		/* watch when the sprite glyph area changes */
		if (getenv("GBARUN_GLYPHPOLL")) {
			struct GBA* gg = core->board;
			static uint32_t lastCrc = 0xFFFFFFFF;
			uint32_t crc = 0;
			for (uint32_t a = 0x06017980; a < 0x06018000; a += 4) {
				crc = crc * 31 + GBAView32(gg->cpu, a);
			}
			if (crc != lastCrc) {
				printf("[glyphpoll] frame %ld: sprite glyph area changed (crc %08X -> %08X)\n", f, lastCrc, crc);
				fflush(stdout);
				lastCrc = crc;
			}
		}

		/* periodic screenshots for visual game navigation.
		 * Use our own registered video buffer (stable pointer). */
		if (g_shotEvery > 0 && (f % g_shotEvery) == 0) {
			snprintf(path, sizeof(path), "%s_shot%06ld.ppm", prefix, f);
			writePPM(path, vbuf, w, h, stride);
		}
		if (getenv("GBARUN_PCTRACE") && (f % 10) == 0) {
			struct GBA* g = core->board;
			fprintf(stderr, "[pc] f=%ld PC=0x%08X LR=0x%08X SP=0x%08X\n", f, g->cpu->gprs[15], g->cpu->gprs[14], g->cpu->gprs[13]);
		}
		char key[32];
		snprintf(key, sizeof(key), "%ld", f);
		for (int i = 4; i < argc; ++i) {
			if (strcmp(argv[i], key) == 0) {
				TRACE("dumping frame %ld", f);
				dumpMemory(core, prefix, f);
				TRACE("dumped frame %ld", f);
			}
		}
	}

	/* capture the final screen from our own stable video buffer */
	core->getPixels(core, &pxFinal, &strideFinal);
	TRACE("pxFinal=%p strideFinal=%zu vbuf=%p", pxFinal, strideFinal, (void*)vbuf);
	{
		const unsigned char* raw = (const unsigned char*) pxFinal;
		printf("raw first 32 bytes:");
		for (int bi = 0; bi < 32; ++bi) printf(" %02X", raw[bi]);
		printf("\n");
		unsigned long nzraw = 0;
		for (size_t bi = 0; bi < 240 * 160 * 4; ++bi) if (raw[bi]) ++nzraw;
		printf("raw nonzero bytes=%lu\n", nzraw);
	}
	snprintf(path, sizeof(path), "%s_final.ppm", prefix);
	writePPM(path, (const color_t*) pxFinal, w, h, strideFinal);
	printf("wrote %s\n", path);

	fprintf(stderr, "[swi] raise=%llu loadhook=%llu\n", g_swiRaiseCount, g_swiCount2);
	if (getenv("GBARUN_WATCH")) {
		dumpWatch(prefix);
	}
	free(vbuf);
	core->deinit(core);
	if (logFile) {
		fclose(logFile);
	}
	return 0;
}



/* ------------------------------------------------------------------ *
 * VRAM write hook (called from patched mGBA memory.c).
 * Records stores into the VRAM tile area together with the guest LR,
 * to identify the glyph/text drawing routine.
 * ------------------------------------------------------------------ */
static unsigned long long g_vramWrites;
static unsigned long long g_lrHits[65536];
static struct GBA* g_watchGba;
static unsigned long long g_pcHits[65536];
static int g_tileWatch;
static FILE* g_tileLog;

/* Hard VRAM hit: called for EVERY store into the 0x06 region, including
 * mirror addresses (0x06020000-0x0603FFFF wrap into VRAM). Logs stores whose
 * effective VRAM offset lands in the sprite-glyph area, so we can catch the
 * text engine even if it writes through a mirror. */
void dshVramHardHit(uint32_t address, uint32_t value) {
	if (!g_tileWatch || !g_tileLog || !g_watchGba) return;
	uint32_t voff = address & 0x1FFFF;
	if (voff >= 0x18000) voff -= 0x18000;
	if (voff < 0x17800 || voff >= 0x18000) return;
	struct ARMCore* cpu = g_watchGba->cpu;
	fprintf(g_tileLog, "VH a=0x%08X voff=0x%05X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X r4=0x%08X r5=0x%08X r6=0x%08X r7=0x%08X f=%ld\n",
	        address, voff, value, cpu->gprs[15], cpu->gprs[14],
	        cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3],
	        cpu->gprs[4], cpu->gprs[5], cpu->gprs[6], cpu->gprs[7], g_frameNo);
	/* When executing inside the BIOS (pc < 0x4000), the game's return address
	 * is on the SVC stack. Dump it so we can find the caller. */
	if (cpu->gprs[15] < 0x4000) {
		uint32_t sp = cpu->gprs[13];
		fprintf(g_tileLog, "VHSK sp=0x%08X:", sp);
		for (int k = 0; k < 24; ++k) {
			uint32_t w = GBAView32(cpu, (sp + 4 * k) & ~3u);
			if (w >= 0x02000000) fprintf(g_tileLog, " [+%d]=0x%08X", k * 4, w);
		}
		fputc('\n', g_tileLog);
		fflush(g_tileLog);
	}
}

void dshVramHook(uint32_t address, uint32_t value) {
	if (g_frameNo >= g_storeTrace0 && g_frameNo <= g_storeTrace1 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		if (address >= 0x06000000 && address < 0x06020000) {
			fprintf(g_tileLog, "ST a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X\n",
			        address, value, cpu->gprs[15], cpu->gprs[14], cpu->gprs[0], cpu->gprs[1], cpu->gprs[2]);
		}
	}
	/* Tile-data watch: log stores into the BG charbase region (0x06004000+)
	 * with full CPU context, to find the routine that composites text glyphs
	 * into VRAM tiles. */
	if (g_tileWatch && address >= 0x06004000 && address < 0x06020000 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		fprintf(g_tileLog, "TW a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X r4=0x%08X r5=0x%08X sp=0x%08X mode=%d\n",
		        address, value, cpu->gprs[15], cpu->gprs[14],
		        cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3],
		        cpu->gprs[4], cpu->gprs[5], cpu->gprs[13], cpu->executionMode);
	}
	/* Copy-queue watch: stores into the glyph-copy request queue at
	 * 0x03007Cxx {src, dst, size, ...}. Catches the text engine enqueueing
	 * a glyph copy with the computed ROM source address. */
	if (g_tileWatch && address >= 0x03007C00 && address < 0x03007E00 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		uint32_t pc = cpu->gprs[15];
		if (pc >= 0x08000000) {
			fprintf(g_tileLog, "CQ a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X r4=0x%08X r5=0x%08X r6=0x%08X r7=0x%08X r8=0x%08X r9=0x%08X f=%ld\n",
			        address, value, pc, cpu->gprs[14],
			        cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3],
			        cpu->gprs[4], cpu->gprs[5], cpu->gprs[6], cpu->gprs[7],
			        cpu->gprs[8], cpu->gprs[9], g_frameNo);
		}
	}
	/* Text-buffer watch: stores into the suspected script-code buffers
	 * (u16 glyph indices + control codes) at 0x0200DFxx. */
	if (g_tileWatch && ((address >= 0x0200DF00 && address < 0x0200E200) ||
	    (address >= 0x02003EE0 && address < 0x02003F20)) && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		uint32_t pc = cpu->gprs[15];
		if (pc >= 0x08000000) {
			fprintf(g_tileLog, "TB a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X r4=0x%08X r5=0x%08X r6=0x%08X r7=0x%08X f=%ld\n",
			        address, value, pc, cpu->gprs[14],
			        cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3],
			        cpu->gprs[4], cpu->gprs[5], cpu->gprs[6], cpu->gprs[7], g_frameNo);
		}
	}
	/* EWRAM image-buffer watch: catch the text compositor drawing glyph
	 * pixels into the staging buffer that later gets DMA'd to VRAM. */
	if (g_tileWatch && address >= 0x02020000 && address < 0x02031000 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		uint32_t pc = cpu->gprs[15];
		if (pc >= 0x08000000) {
			fprintf(g_tileLog, "EW a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X r4=0x%08X r5=0x%08X mode=%d\n",
			        address, value, pc, cpu->gprs[14],
			        cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3],
			        cpu->gprs[4], cpu->gprs[5], cpu->executionMode);
		}
	}
	/* IWRAM tilemap-staging watch: the per-frame DMA copies 0x03000000 ->
	 * 0x06000000 (8KB of BG maps). Text engines write tile indices into this
	 * staging area with plain CPU stores — catching them reveals the text
	 * routine and the script pointer in its registers. */
	if (g_tileWatch && address >= 0x03000000 && address < 0x03002000 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		uint32_t pc = cpu->gprs[15];
		if (pc >= 0x08000000) {
			fprintf(g_tileLog, "IW a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X r4=0x%08X r5=0x%08X mode=%d\n",
			        address, value, pc, cpu->gprs[14],
			        cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3],
			        cpu->gprs[4], cpu->gprs[5], cpu->executionMode);
		}
	}
	/* DMA register watch: log every DMA setup, and when a channel is enabled
	 * dump SAD/DAD so we know the true source of VRAM-bound data. */
	if (g_tileWatch && address >= 0x040000B0 && address < 0x040000E0 && g_tileLog) {
		struct ARMCore* cpu = g_watchGba->cpu;
		fprintf(g_tileLog, "DMAW a=0x%08X v=0x%08X pc=0x%08X lr=0x%08X\n",
		        address, value, cpu->gprs[15], cpu->gprs[14]);
		if (((address & 0xF) == 0xC && (value & 0x80000000)) || ((address & 0xF) == 0xE && (value & 0x8000))) {
			fflush(g_tileLog);
		}
	}
	/* The real BIOS' SWI dispatcher executes at 0x140..0x144. When the CPU is
	 * there with LR pointing just past a "svc" instruction, we can recover the
	 * SWI number from the instruction stream and log the handler arguments. */
	if (g_watchGba && g_swiLog) {
		uint32_t pc = g_watchGba->cpu->gprs[15];
		if (pc == 0x00000144 || pc == 0x00000148) {
			uint32_t lr = g_watchGba->cpu->gprs[14];
			if (lr >= 2) {
				uint32_t sv = GBAView16(g_watchGba->cpu, (lr - 2) & 0xFFFFFFFE);
				if ((sv & 0xFF00) == 0xDF00) {
					fprintf(g_swiLog, "SWI=0x%02X r0=0x%08X r1=0x%08X r2=0x%08X caller=0x%08X\n",
					        sv & 0xFF, g_watchGba->cpu->gprs[0], g_watchGba->cpu->gprs[1],
					        g_watchGba->cpu->gprs[2], lr);
					fflush(g_swiLog);
				}
			}
		}
	}
	/* log every memory store issued by the BIOS while it runs in the SWI
	 * handler region, so we can see the true SWI call sites */
	if (g_watchGba && g_swiLog && getenv("GBARUN_BIOSTRACE")) {
		uint32_t pc = g_watchGba->cpu->gprs[15];
		if (pc >= 0x00000140 && pc < 0x00000170) {
			fprintf(g_swiLog, "BIOSPC=0x%04X r0=0x%08X r1=0x%08X r2=0x%08X lr=0x%08X\n",
			        pc, g_watchGba->cpu->gprs[0], g_watchGba->cpu->gprs[1],
			        g_watchGba->cpu->gprs[2], g_watchGba->cpu->gprs[14]);
			fflush(g_swiLog);
		}
	}
	if (address >= 0x06000000 && address < 0x06018000 && g_watchGba) {
		++g_vramWrites;
		uint32_t lr = g_watchGba->cpu->gprs[14] & 0xFFFFFF;
		++g_lrHits[(lr >> 1) & 0xFFFF];
		uint32_t pc = g_watchGba->cpu->gprs[15] & 0xFFFFFF;
		++g_pcHits[(pc >> 1) & 0xFFFF];
	}
}

/* --- SWI hook: log decompression source/destination --- */
static FILE* g_swiLog;
static int g_swiCount;
static const char* g_snapPrefix;
static int g_snapBudget = 40;

/* dump all GBA memory regions to <prefix>_swi<N>_{ewram,iwram,vram,pal,oam,io}.bin */
static void snapshotNow(int idx, int immediate, uint32_t src, uint32_t dst) {
	if (!g_snapPrefix || g_snapBudget <= 0) return;
	--g_snapBudget;
	struct GBA* g = g_watchGba;
	if (!g) return;
	char path[1024];
	static const struct { const char* n; uint32_t a; uint32_t s; } R[] = {
		{ "ewram", 0x02000000, 0x40000 }, { "iwram", 0x03000000, 0x8000 },
		{ "io", 0x04000000, 0x400 }, { "pal", 0x05000000, 0x400 },
		{ "vram", 0x06000000, 0x18000 }, { "oam", 0x07000000, 0x400 },
	};
	for (size_t i = 0; i < sizeof(R)/sizeof(R[0]); ++i) {
		snprintf(path, sizeof(path), "%s_swi%03d_%s.bin", g_snapPrefix, idx, R[i].n);
		FILE* f = fopen(path, "wb");
		if (!f) continue;
		for (uint32_t a = 0; a < R[i].s; ++a) {
			unsigned char b = GBAView8(g->cpu, R[i].a + a);
			fwrite(&b, 1, 1, f);
		}
		fclose(f);
	}
	snprintf(path, sizeof(path), "%s_swi%03d.txt", g_snapPrefix, idx);
	FILE* f = fopen(path, "wb");
	if (f) {
		fprintf(f, "swi=0x%02X src=0x%08X dst=0x%08X\n", immediate, src, dst);
		fclose(f);
	}
	printf("snapshot %d: swi=0x%02X src=0x%08X dst=0x%08X\n", idx, immediate, src, dst);
}

/* Called from ARMRaiseSWI for every SWI, before the BIOS handler runs.
 * The SWI number is in the instruction at LR; args are in r0..r3. */
static unsigned long long g_swiRaiseCount;
static unsigned long long g_swiCount2;
void dshSwiRaise(struct ARMCore* cpu) {
	++g_swiRaiseCount;
	if (!g_swiLog) return;
	uint32_t lr = cpu->gprs[14];
	uint32_t instr = 0;
	if (cpu->executionMode == 1 /* THUMB */) {
		instr = GBAView16(cpu, lr & 0xFFFFFFFE);
	} else {
		instr = GBAView32(cpu, lr & 0xFFFFFFFC);
	}
	int swi = -1;
	if ((instr & 0xFF00) == 0xDF00) swi = instr & 0xFF;
	else if ((instr & 0x0F000000) == 0x0F000000) swi = instr & 0xFF;
	if (swi < 0) return;
	if (swi != 0x05 && swi != 0x02 && swi != 0x01 && swi != 0x00) {
		fprintf(g_swiLog, "SWI=0x%02X r0=0x%08X r1=0x%08X r2=0x%08X r3=0x%08X lr=0x%08X instr=0x%08X mode=%d\n",
		        swi, cpu->gprs[0], cpu->gprs[1], cpu->gprs[2], cpu->gprs[3], lr, instr, cpu->executionMode);
		fflush(g_swiLog);
	}
}

void dshSwiHook(int immediate, uint32_t r0, uint32_t r1, uint32_t r2, uint32_t lr) {
	if (g_swiLog && getenv("GBARUN_ALLSWI")) {
		fprintf(g_swiLog, "SWI=0x%02X r0=0x%08X r1=0x%08X r2=0x%08X lr=0x%08X\n", immediate, r0, r1, r2, lr);
		fflush(g_swiLog);
	}
	switch (immediate) {
	case 0x11: case 0x12: case 0x13:
	case 0x14: case 0x15:
	case 0x16:
		if (g_swiLog) {
			fprintf(g_swiLog, "SWI=0x%02X src=0x%08X dst=0x%08X r2=0x%08X lr=0x%08X\n",
			        immediate, r0, r1, r2, lr);
			fflush(g_swiLog);
		}
		snapshotNow(++g_swiCount, immediate, r0, r1);
		break;
	default:
		break;
	}
}

static void dumpWatch(const char* prefix) {
	char path[1024];
	snprintf(path, sizeof(path), "%s_vramwatch.txt", prefix);
	FILE* f = fopen(path, "wb");
	if (!f) return;
	fprintf(f, "total VRAM writes: %llu\n\n", g_vramWrites);
	fprintf(f, "guest PC histogram (top 50):\n");
	for (int k = 0; k < 50; ++k) {
		int bi = -1; unsigned long long bv = 0;
		for (int j = 0; j < 65536; ++j) if (g_pcHits[j] > bv) { bv = g_pcHits[j]; bi = j; }
		if (bi < 0 || !bv) break;
		fprintf(f, "   PC=0x%06X  count=%llu\n", bi << 1, bv);
		g_pcHits[bi] = 0;
	}
	fprintf(f, "\nguest LR histogram (top 50):\n");
	for (int k = 0; k < 50; ++k) {
		int bi = -1; unsigned long long bv = 0;
		for (int j = 0; j < 65536; ++j) if (g_lrHits[j] > bv) { bv = g_lrHits[j]; bi = j; }
		if (bi < 0 || !bv) break;
		fprintf(f, "   LR=0x%06X  count=%llu\n", bi << 1, bv);
		g_lrHits[bi] = 0;
	}
	fclose(f);
	printf("wrote %s\n", path);
}
