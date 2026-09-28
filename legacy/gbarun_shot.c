/* gbarun_shot.c - headless mGBA harness that produces REAL frames.
 *
 * The stock libmgba build has no video renderer attached, so `core->getPixels`
 * (and gbarun's PPM) come back black.  This harness plugs mGBA's own software
 * renderer into the core, which makes the frame buffer usable and lets
 * mCoreSaveStateNamed embed a correct screenshot - i.e. exactly the picture the
 * mGBA GUI would show.
 *
 * Usage:
 *   gbarun_shot <rom> <frames> <out.ppm> [key@frame[:hold] ...]
 *
 * Environment:
 *   GBARUN_STATE=<file.ssN>   load an mGBA savestate before running
 *   GBARUN_SAVE=<file.ssN>    write an mGBA savestate (with screenshot) at the end
 *   GBARUN_SHOT_EVERY=N       also write <out>_<frame>.ppm every N frames
 *   GBARUN_FORCE_SAVE=N       force the save-memory type (see gbarun)
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

#define WIDTH 240
#define HEIGHT 160

static color_t g_frame[WIDTH * HEIGHT];

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

static void writePPM(const char* path) {
	FILE* f = fopen(path, "wb");
	if (!f) {
		perror("fopen");
		return;
	}
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

int main(int argc, char** argv) {
	if (argc < 4) {
		fprintf(stderr, "usage: %s <rom> <frames> <out.ppm> [KEY@frame[:hold] ...]\n",
		        argv[0]);
		return 2;
	}
	const char* romPath = argv[1];
	long frames = strtol(argv[2], NULL, 0);
	const char* outPath = argv[3];

	struct mCore* core = GBACoreCreate();
	if (!core) {
		fprintf(stderr, "failed to create GBA core\n");
		return 1;
	}
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
			if (bv && core->loadBIOS(core, bv, 0)) {
				fprintf(stderr, "[bios] loaded %s\n", biosPath);
			}
		}
	}
	/* Force the save type BEFORE autoloading: mGBA sizes the save buffer from
	 * the type, so forcing it after the load reallocates the buffer and drops
	 * the data.  Needed for games with no save-type signature in the header. */
	{
		const char* force = getenv("GBARUN_FORCE_SAVE");
		if (force) {
			struct GBA* g0 = core->board;
			GBASavedataForceType(&g0->memory.savedata, (enum SavedataType) atoi(force));
			fprintf(stderr, "[save] type forced to %s\n", force);
		}
	}
	/* The frontends call this after loading a ROM; without it the harness runs
	 * with an empty cartridge save and every save/load screen shows no files. */
	if (!getenv("GBARUN_NO_AUTOSAVE") && mCoreAutoloadSave(core)) {
		fprintf(stderr, "[save] autoloaded\n");
	} else {
		fprintf(stderr, "[save] no save file loaded\n");
	}

	/* --- attach mGBA's software renderer ------------------------------- */
	struct GBA* gba = core->board;
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
				fprintf(stderr, "[state] FAILED %s\n", st);
			}
			if (sf) sf->close(sf);
		}
	}

	{
		/* GBARUN_WIPE_SAVE=1: blank the cartridge save RAM after the state
		 * load, so a fresh save can be made and inspected. */
		const char* wipe = getenv("GBARUN_WIPE_SAVE");
		if (wipe && *wipe) {
			struct GBASavedata* sd = &gba->memory.savedata;
			unsigned sz = (sd->type == SAVEDATA_EEPROM) ? 8192 :
			              (sd->type == SAVEDATA_FLASH512) ? 65536 :
			              (sd->type == SAVEDATA_FLASH1M) ? 131072 : 32768;
			if (sd->data) {
				memset(sd->data, 0xFF, sz);
				sd->dirty = 1;
			}
			fprintf(stderr, "[save] wiped %u bytes (type %d)\n", sz, sd->type);
		}
	}

	/* A state load writes memory directly, so the renderer's cached palettes
	 * and sprite list are stale.  Re-announce palette and OAM so the first
	 * frame after the load uses the restored data. */
	{
		struct GBAVideoRenderer* r = gba->video.renderer;
		for (int i = 0; i < 512; ++i) {
			r->writePalette(r, i * 2, gba->video.palette[i]);
		}
		for (int i = 0; i < 128; ++i) {
			r->writeOAM(r, i);
		}
		renderer->outputBuffer = g_frame;
		renderer->outputBufferStride = WIDTH;
	}

	/* --- input script: NAME@FRAME[:HOLD] -------------------------------- */
	struct keyevent { int key; long frame; long count; } events[2048];
	int nEvents = 0;
	for (int i = 4; i < argc && nEvents < 2048; ++i) {
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
	long shotEvery = 0;
	{
		const char* se = getenv("GBARUN_SHOT_EVERY");
		if (se) shotEvery = strtol(se, NULL, 0);
	}

	uint32_t held = 0;
	for (long f = 0; f < frames; ++f) {
		uint32_t keys = 0;
		for (int e = 0; e < nEvents; ++e) {
			if (f >= events[e].frame && f < events[e].frame + events[e].count) {
				keys |= 1u << events[e].key;
			}
		}
		if (keys != held) {
			held = keys;
			core->setKeys(core, held);
		}
		core->runFrame(core);
		if (shotEvery > 0 && f > 0 && (f % shotEvery) == 0) {
			char path[1024];
			snprintf(path, sizeof(path), "%s_%ld.ppm", outPath, f);
			writePPM(path);
		}
	}

	writePPM(outPath);
	unsigned long nz = 0;
	for (int i = 0; i < WIDTH * HEIGHT; ++i) {
		if (g_frame[i] & 0xFFFFFF) ++nz;
	}
	printf("wrote %s (%lu non-black pixels)\n", outPath, nz);

	{
		/* GBARUN_DUMP_SAVE=<file>: write the cartridge save memory out, so a
		 * save the game itself wrote can be inspected (mGBA flushes .sav only
		 * through a frontend). */
		const char* dp = getenv("GBARUN_DUMP_SAVE");
		if (dp && *dp) {
			struct GBASavedata* sd = &gba->memory.savedata;
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
	{
		const char* sv = getenv("GBARUN_SAVE");
		if (sv && *sv) {
			struct VFile* vf = VFileOpen(sv, O_WRONLY | O_CREAT | O_TRUNC);
			if (vf && mCoreSaveStateNamed(core, vf, SAVESTATE_ALL)) {
				fprintf(stderr, "[state] wrote %s\n", sv);
			} else {
				fprintf(stderr, "[state] FAILED to write %s\n", sv);
			}
			if (vf) vf->close(vf);
		}
	}

	free(renderer);
	core->deinit(core);
	return 0;
}
