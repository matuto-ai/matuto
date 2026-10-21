/* A stand-in speech server: Breeze-TTS-2.cpp and whisper.cpp are not installed
   on every machine that runs this suite, so the qualification driver talks to
   this instead. It answers the same HTTP surface the two runtimes do, records
   the exact request it was given, and reports the device memory a real
   synthesis process would hold.
   Build arguments: -DFIXTURE=<directory this process reads and writes>. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>
#include <sys/socket.h>
#include <netinet/in.h>

static void quote(FILE *out,const char *s) {
    fputc('"',out);
    for(;*s;s++) {
        if(*s=='"'||*s=='\\') fputc('\\',out);
        if((unsigned char)*s<32) fprintf(out,"\\u%04x",(unsigned char)*s);
        else fputc(*s,out);
    }
    fputc('"',out);
}
static const char *option(int argc,char **argv,const char *name) {
    for(int i=1;i+1<argc;i++) if(!strcmp(argv[i],name)) return argv[i+1];
    return "";
}
static int marker(const char *path) { return access(path,F_OK)==0; }
/* A client sends the headers and the body as two separate writes, so one read()
   here returns the headers alone and leaves the body sitting in the receive
   queue. Closing a socket that still holds unread data makes the kernel answer
   RST instead of FIN and discard the response already in flight, so a synthesis
   slow enough for the body to land reset the run that was reading its audio.
   The whole declared request is taken before anything is answered. */
static ssize_t read_request(int client,char *request,size_t room) {
    ssize_t size=0; long length=0;
    for(;;) {
        ssize_t got;
        char *end, *declared;
        if((size_t)size+1>=room) break;
        got=read(client,request+size,room-1-(size_t)size);
        if(got<=0) break;
        size+=got; request[size]=0;
        end=strstr(request,"\r\n\r\n");
        if(!end) continue;
        declared=strstr(request,"Content-Length:");
        if(declared&&declared<end) length=atol(declared+15);
        if((size_t)size>=(size_t)(end-request)+4+(size_t)length) break;
    }
    return size;
}
static long amount(const char *path) {
    FILE *file=fopen(path,"r");
    long value=0;
    if(file) { if(fscanf(file,"%ld",&value)!=1) value=0; fclose(file); }
    return value;
}
/* What this process was asked to run, in the same shape the model fixture
   records, so a test can read the placement and the model operand back. */
static void record(int argc,char **argv,const char *self) {
    char path[PATH_MAX], cwd[PATH_MAX];
    FILE *out;
    snprintf(path,sizeof(path),"%s/received-%s.json",FIXTURE,self);
    out=fopen(path,"w");
    if(!out) exit(3);
    fprintf(out,"{\"program\":"); quote(out,self);
    fprintf(out,",\"pid\":%d,\"args\":[",getpid());
    for(int i=1;i<argc;i++) { if(i>1) fputc(',',out); quote(out,argv[i]); }
    fputs("],\"hip\":",out); quote(out,getenv("ROCR_VISIBLE_DEVICES")?:"");
    fputs(",\"vulkan\":",out); quote(out,getenv("GGML_VK_VISIBLE_DEVICES")?:"");
    fputs(",\"cuda\":",out); quote(out,getenv("CUDA_VISIBLE_DEVICES")?:"");
    fputs(",\"identity\":",out); quote(out,getenv("MATUTO_RUNTIME_IDENTITY")?:"");
    if(!getcwd(cwd,sizeof(cwd))) exit(5);
    fputs(",\"cwd\":",out); quote(out,cwd);
    fputs("}",out); fclose(out);
}
int main(int argc,char **argv) {
    const char *self=strrchr(argv[0],'/'), *port=option(argc,argv,"--port");
    struct sockaddr_in address;
    char path[PATH_MAX];
    int listener=socket(AF_INET,SOCK_STREAM,0), reuse=1;
    self=self?self+1:argv[0];
    if(!*port) return 2;
    record(argc,argv,self);
    if(!strcmp(self,"breeze-server")&&marker(FIXTURE "/speech-fail-start")) return 8;
    if(!strcmp(self,"whisper-server")&&marker(FIXTURE "/vram-missing")) unlink(FIXTURE "/vram-used");
    snprintf(path,sizeof(path),"%s/vram-speech",FIXTURE);
    /* Synthesis holds device memory for as long as it runs; recognition is a CPU
       workload and holds none, which is why only one program name grows it. */
    if(!strcmp(self,"breeze-server")&&marker(path)) {
        long held=amount(FIXTURE "/vram-used")+amount(path);
        FILE *used=fopen(FIXTURE "/vram-used","w");
        if(used) { fprintf(used,"%ld",held); fclose(used); }
    }
    if(listener<0) return 6;
    setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&reuse,sizeof(reuse));
    memset(&address,0,sizeof(address));
    address.sin_family=AF_INET;
    address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    address.sin_port=htons((unsigned short)atoi(port));
    if(bind(listener,(struct sockaddr *)&address,sizeof(address))||listen(listener,8)) return 7;
    for(;;) {
        char request[4096];
        int client=accept(listener,NULL,NULL);
        ssize_t size;
        FILE *stream;
        /* 24000 samples at 24 kHz: one second of audio, scaled with the
           synthesis delays that establish the bundle's timing relationships. */
        long samples=24000;
        if(client<0) continue;
        size=read_request(client,request,sizeof(request));
        if(size<=0) { close(client); continue; }
        stream=fdopen(client,"w");
        if(!stream) { close(client); continue; }
        if(!strncmp(request,"GET / ",6)||!strncmp(request,"GET /index.html ",16)) {
            /* A runtime that answers only the endpoints it serves is up; the lab
               reads readiness from any answer, including this 404. The status it
               answered is recorded so a case can prove which path it took. */
            int status=marker(FIXTURE "/speech-no-root")?404:200;
            char root[PATH_MAX];
            snprintf(root,sizeof(root),"%s/root-%s.json",FIXTURE,self);
            FILE *answer=fopen(root,"w");
            if(answer) { fprintf(answer,"{\"status\": %d}\n",status); fclose(answer); }
            if(status==404)
                fputs("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",stream);
            else
                fputs("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n<html><body>fixture</body></html>",stream);
        } else if(!strncmp(request,"POST /v1/audio/speech ",22)) {
            if(marker(FIXTURE "/speech-http-error")) {
                fputs("HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",stream);
            } else {
                if(marker(FIXTURE "/speech-silent")) samples=0;
                if(marker(FIXTURE "/speech-slow")) { samples=2400; usleep(750000); }
                /* One second of audio that takes longer to generate than the model
                   keeps generating: the load lapses mid-synthesis while the factor
                   is still above 1.0. */
                if(marker(FIXTURE "/speech-lag")) usleep(375000);
                fputs("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nConnection: close\r\n",stream);
                fprintf(stream,"Content-Length: %ld\r\n\r\n",samples*2);
                for(long i=0;i<samples*2;i++) fputc(0,stream);
            }
        } else if(!strcmp(self,"whisper-server")&&!strncmp(request,"POST /inference ",16)) {
            if(marker(FIXTURE "/recognition-http-error")) {
                fputs("HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",stream);
            } else {
                const char *text=marker(FIXTURE "/recognition-empty")?"":"Matuto qualifies this voice bundle under the stated generation load.";
                char response[512];
                int length;
                if(marker(FIXTURE "/recognition-slow")) usleep(1100000);
                length=snprintf(response,sizeof(response),"{\"text\":\"%s\"}",text);
                fputs("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n",stream);
                fprintf(stream,"Content-Length: %d\r\n\r\n%s",length,response);
            }
        } else {
            fputs("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n",stream);
        }
        fclose(stream);
    }
}
