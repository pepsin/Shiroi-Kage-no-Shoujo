/* Read-only libretro runner. Input: a frame-indexed libretro joypad mask file. */
#include <libretro.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdarg.h>
#include <sys/stat.h>
static const char *outdir;
static unsigned frame, mask, format;
static uint8_t rgb[240*160*3];
static void *ewram, *iwram;
static bool have_video;
static void logmsg(enum retro_log_level level, const char *fmt, ...) {
 va_list ap; va_start(ap,fmt); vfprintf(stderr,fmt,ap); va_end(ap);
}
static bool env(unsigned cmd, void *data) {
 switch(cmd) {
 case RETRO_ENVIRONMENT_GET_LOG_INTERFACE: ((struct retro_log_callback*)data)->log=logmsg; return true;
 case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
 case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY: *(const char**)data=outdir; return true;
 case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: format=*(unsigned*)data; return format <= 2;
 case RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE: *(bool*)data=false; return true;
 case RETRO_ENVIRONMENT_GET_VARIABLE: {
  struct retro_variable *v=data;
  if(!strcmp(v->key,"gpsp_bios")) v->value="builtin";
  else if(!strcmp(v->key,"gpsp_boot_mode")) v->value="game";
  else if(!strcmp(v->key,"gpsp_frameskip")) v->value="disabled";
  else if(!strcmp(v->key,"gpsp_drc")) v->value="disabled";
  else {v->value=NULL; return false;} return true;
 }
 case RETRO_ENVIRONMENT_SET_MEMORY_MAPS: {
  const struct retro_memory_map *m=data;
  for(unsigned i=0;i<m->num_descriptors;i++) {
   const struct retro_memory_descriptor *d=&m->descriptors[i];
   if(d->start==0x02000000) ewram=(uint8_t*)d->ptr+d->offset;
   if(d->start==0x03000000) iwram=(uint8_t*)d->ptr+d->offset;
  } return true;
 }
 case RETRO_ENVIRONMENT_GET_CAN_DUPE: *(bool*)data=true; return true;
 case RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS:
 case RETRO_ENVIRONMENT_SET_VARIABLES: return true;
 default: return false;
 }
}
static void video(const void *data,unsigned w,unsigned h,size_t pitch) {
 if(!data) return;
 if(w!=240 || h!=160) {fprintf(stderr,"Unexpected video %ux%u\n",w,h);exit(2);}
 for(unsigned y=0;y<h;y++) for(unsigned x=0;x<w;x++) {
  uint32_t p; unsigned r,g,b;
  if(format==RETRO_PIXEL_FORMAT_XRGB8888) {
   memcpy(&p,(const char*)data+y*pitch+x*4,4); r=p>>16&255;g=p>>8&255;b=p&255;
  } else {
   uint16_t v; memcpy(&v,(const char*)data+y*pitch+x*2,2); p=v;
   r=(p>>(format==2?11:10)&31)*255/31;
   g=(p>>5&(format==2?63:31))*255/(format==2?63:31); b=(p&31)*255/31;
  }
  unsigned k=(y*w+x)*3;rgb[k]=r;rgb[k+1]=g;rgb[k+2]=b;
 } have_video=true;
}
static void audio(int16_t l,int16_t r){}
static size_t batch(const int16_t *d,size_t n){return n;}
static void poll(void){}
static int16_t input(unsigned port,unsigned device,unsigned index,unsigned id) {
 return port==0 && device==RETRO_DEVICE_JOYPAD && id<16 && (mask&(1u<<id)) ? 1:0;
}
static void dump(const char *ext,const void *data,size_t n) {
 char name[4096]; snprintf(name,sizeof(name),"%s/frame-%06u.%s",outdir,frame+1,ext);
 FILE *f=fopen(name,"wb");if(!f){perror(name);exit(2);} 
 if(!strcmp(ext,"ppm"))fprintf(f,"P6\n240 160\n255\n");
 if(fwrite(data,1,n,f)!=n){perror("write");exit(2);}fclose(f);
}
#define FN(name) __typeof__(&name) p_##name=dlsym(core,#name); if(!p_##name){fprintf(stderr,"Missing %s\n",#name);return 2;}
int main(int argc,char **argv) {
 if(argc!=5 && argc!=6){fprintf(stderr,"usage: %s core rom output-dir frame-masks.txt [save.sav]\n",argv[0]);return 2;}
 outdir=argv[3]; void *core=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);if(!core){fprintf(stderr,"%s\n",dlerror());return 2;}
 FN(retro_set_environment);FN(retro_set_video_refresh);FN(retro_set_audio_sample);
 FN(retro_set_audio_sample_batch);FN(retro_set_input_poll);FN(retro_set_input_state);
 FN(retro_init);FN(retro_deinit);FN(retro_load_game);FN(retro_run);FN(retro_unload_game);
 FN(retro_get_memory_data);FN(retro_get_memory_size);FN(retro_get_system_info);
 p_retro_set_environment(env);p_retro_set_video_refresh(video);p_retro_set_audio_sample(audio);
 p_retro_set_audio_sample_batch(batch);p_retro_set_input_poll(poll);p_retro_set_input_state(input);
 p_retro_init();struct retro_system_info si={0};p_retro_get_system_info(&si);
 fprintf(stderr,"core=%s version=%s\n",si.library_name,si.library_version);
 struct retro_game_info gi={.path=argv[2]};
 if(!p_retro_load_game(&gi)){fprintf(stderr,"ROM LOAD FAILED\n");return 1;}
 fprintf(stderr,"ROM LOAD OK\n");
 void **maps=dlsym(core,"memory_map_read");
 if(maps) fprintf(stderr,"EEPROM read 0x0D000000 has_direct_ROM_mapping=%s\n", maps[0xD000000/0x8000]?"YES":"no");
 if(argc==6) {
  FILE *s=fopen(argv[5],"rb"); if(!s){perror(argv[5]);return 2;}
  size_t n=p_retro_get_memory_size(RETRO_MEMORY_SAVE_RAM);
  void *mem=p_retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
  if(!mem||n<8192){fprintf(stderr,"Missing save RAM\n");return 2;}
  size_t got=fread(mem,1,n,s);fclose(s);
  if(got!=8192){fprintf(stderr,"Expected 8KiB EEPROM, got %zu\n",got);return 2;}
  fprintf(stderr,"Loaded test cartridge EEPROM %zu bytes\n",got);
 }
 FILE *keys=fopen(argv[4],"r");if(!keys){perror(argv[4]);return 2;}
 while(fscanf(keys,"%u",&mask)==1) {
  p_retro_run();
  if((frame+1)%500==0){
   if(have_video)dump("ppm",rgb,sizeof(rgb));
   if(ewram)dump("ewram",ewram,0x40000);if(iwram)dump("iwram",iwram,0x8000);
  } frame++;
 }
 if(ferror(keys)||!feof(keys)||frame==0||!have_video){fprintf(stderr,"INCOMPLETE\n");return 1;}fclose(keys);
 frame--;
 dump("ppm",rgb,sizeof(rgb));if(ewram)dump("ewram",ewram,0x40000);if(iwram)dump("iwram",iwram,0x8000);
 size_t sz=p_retro_get_memory_size(RETRO_MEMORY_SAVE_RAM);void *save=p_retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
 if(sz&&save)dump("sav",save,sz);
 fprintf(stderr,"DONE frames=%u ewram=%s iwram=%s\n",frame+1,ewram?"yes":"no",iwram?"yes":"no");
 p_retro_unload_game();p_retro_deinit();dlclose(core);return 0;
}
