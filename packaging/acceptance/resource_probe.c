#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void *sleeper(void *unused) { sleep(2); return NULL; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
int main(int argc, char **argv) {
  if (argc != 2) return 2;
  if (!strcmp(argv[1],"cpu")) {
    double end=now()+1.5; volatile unsigned long value=1;
    while(now()<end) for(int i=0;i<10000;i++) value=value*1664525+1013904223;
    return 0;
  }
  if (!strcmp(argv[1],"memory")) {
    for(int i=0;i<128;i++) {
      volatile char *p=malloc(1024*1024); if(!p) return 3;
      for(int j=0;j<1024*1024;j+=4096) p[j]=1;
    } return 4;
  }
  if (!strcmp(argv[1],"processes")) {
    int count=0,error=0;
    for(int i=0;i<32;i++) { pid_t p=fork(); if(p<0) {error=errno;break;} if(!p) {sleep(2);_exit(0);} count++; }
    printf("created=%d errno=%d\n",count,error); while(wait(NULL)>0) {} return 0;
  }
  if (!strcmp(argv[1],"threads")) {
    pthread_t threads[32]; int count=0,error=0;
    for(int i=0;i<32;i++) {int r=pthread_create(&threads[i],NULL,sleeper,NULL); if(r) {error=r;break;} count++;}
    printf("created=%d errno=%d\n",count,error); for(int i=0;i<count;i++) pthread_join(threads[i],NULL); return 0;
  }
  return 2;
}
