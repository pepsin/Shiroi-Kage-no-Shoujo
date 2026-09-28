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
 * Usage:
 *   autoplay <rom> <script> [outdir]
 *
 * Environment:
 *   GBARUN_STATE=<file>   load a savestate before the script
 *   GBARUN_BIOS=<file>    use a real BIOS (usually leave unset)
 *   GBARUN_FORCE_SAVE=N   force the cartridge save type
 *   GBARUN_NO_AUTOSAVE=1  do not autoload the .sav next to the ROM
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

static void writePNG(const char* path) {
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

static void runFrames(long n, uint32_t keys) {
	g_core->setKeys(g_core, keys);
	for (long i = 0; i < n; ++i) {
		g_core->runFrame(g_core);
		++g_frameNo;
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
	struct VFile* vf = VFileOpen(path, O_WRONLY | O_CREAT | O_TRUNC);
	int ok = 0;
	if (vf) {
		ok = mCoreSaveStateNamed(g_core, vf, SAVESTATE_ALL);
		vf->close(vf);
	}
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
		GBAVideoAssociateRenderer(&g_gba->video, &r->d);
	}
	g_core->reset(g_core);

	{
		const char* st = getenv("GBARUN_STATE");
		if (st && *st) {
			if (loadState(st)) fprintf(stderr, "[state] loaded %s\n", st);
			else fprintf(stderr, "[state] FAILED %s\n", st);
			((struct GBAVideoSoftwareRenderer*) g_gba->video.renderer)->outputBuffer = g_frame;
			((struct GBAVideoSoftwareRenderer*) g_gba->video.renderer)->outputBufferStride = WIDTH;
		}
	}
	mkdir(g_outdir, 0755);

	char line[512];
	int nshot = 0;
	while (fgets(line, sizeof(line), script)) {
		char* hash = strchr(line, '#');
		char cmd[64] = {0};
		char a1[256] = {0}, a2[256] = {0};
		long n1 = 0, n2 = 0;
		if (hash) *hash = '\0';
		int got = sscanf(line, "%63s %255s %255s", cmd, a1, a2);
		if (got < 1) continue;
		if (got >= 2) n1 = strtol(a1, NULL, 0);
		if (got >= 3) n2 = strtol(a2, NULL, 0);

		if (!strcmp(cmd, "wait")) {
			runFrames(n1, 0);
		} else if (!strcmp(cmd, "press")) {
			int k = keyByName(a1);
			long hold = got >= 2 && n1 > 0 ? n1 : 4;
			long gap = got >= 3 ? n2 : 12;
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); continue; }
			runFrames(hold, 1u << k);
			runFrames(gap, 0);
		} else if (!strcmp(cmd, "hold")) {
			int k = keyByName(a1);
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); continue; }
			runFrames(n1 > 0 ? n1 : 4, 1u << k);
		} else if (!strcmp(cmd, "mash")) {
			int k = keyByName(a1);
			long times = n1 > 0 ? n1 : 1;
			long gap = got >= 3 ? n2 : 20;
			if (k < 0) { fprintf(stderr, "[script] unknown key %s\n", a1); continue; }
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
		} else if (!strcmp(cmd, "end")) {
			break;
		} else {
			fprintf(stderr, "[script] unknown command: %s\n", cmd);
		}
	}
	fclose(script);

	{
		char path[1024];
		shotPath("_final", path, sizeof(path));
		writePNG(path);
		fprintf(stderr, "[done] frames=%ld final=%s\n", g_frameNo, path);
	}
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
	g_core->deinit(g_core);
	return 0;
}
