"""Deterministic OS/backend observations around the real runtime and caller code.

The compiled ELF exercises descriptor execution and Landlock, unlike a script
interpreter. Hardware observations and fixture-only failure deadlines are
substituted; trust, state, constraints, coordination, HTTP handlers and process
launch remain production code.
"""
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REVISION = "de699957b92f490efebad149665b0dccf127eaff"
# Profiles pin the commit; the build prints the abbreviation git gave it. A
# fixture reporting the full commit models a build llama.cpp never produces.
REPORTED_REVISION = REVISION[:9]
HIP_UUID = "GPU-1234567890abcdef"
VK_UUID = "1234567890abcdef1234567890abcdef"
PCI = "0000:03:00.0"
HOST = {"pciAddress": PCI, "vendor": "0x1002", "device": "0x747e", "class": "0x030000", "subsystem_vendor": "0x1002", "subsystem_device": "0x0e1b"}


def create(root):
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    agents = root / "agents"
    agents.mkdir(exist_ok=True)
    build = root / "build/bin"
    build.mkdir(parents=True, exist_ok=True)
    inventory = {"host": [HOST], "HIP/ROCm": [{"pciAddress": PCI, "uuid": HIP_UUID}], "Vulkan": [{"pciAddress": PCI, "uuid": VK_UUID, "index": 0}]}
    (root / "inventory.json").write_text(json.dumps(inventory))
    shutil.copy2(ROOT / "agents/matuto-lab", agents / "matuto-lab")
    for name in ("matuto_runtime.py", "matuto_bench.py", "matuto_voice.py"):
        shutil.copy2(ROOT / "agents" / name, agents / name)
    package = agents / "matuto"
    package.mkdir()
    for name in ("matuto_runtime.py", "matuto_bench.py", "matuto_voice.py", "progress.py",
                 "store_lab.py", "store.py", "arguments.py", "installation.py", "recipes.py",
                 "__init__.py"):
        shutil.copy2(ROOT / "agents/matuto" / name, package / name)
    # Observe entry to the real protected shutdown wait, without shortening it.
    bench = agents / "matuto" / "matuto_bench.py"
    source = bench.read_text()
    # Separate fixture homes model separate cards, not competing host workloads.
    server_probe = '                if (entry / "cmdline").read_bytes().split(b"\\0", 1)[0].rpartition(b"/")[2] == b"llama-server":'
    assert source.count(server_probe) == 1
    source = source.replace(server_probe, '''                if os.fsencode("HOME=" + os.environ["HOME"]) not in (entry / "environ").read_bytes().split(b"\\0"):
                    continue
''' + server_probe)
    wait = "            process.wait(timeout=STOP_SECONDS)"
    assert source.count(wait) == 1
    bench.write_text(source.replace(wait, f'''            if number == signal.SIGTERM and "-d" in process.args and Path({str(root / 'ignore-term')!r}).exists():
                Path({str(root / 'shutdown-wait')!r}).write_text(str(process.pid))
{wait}'''))
    # Byte-valued VRAM is a device counter the test machine does not have; the
    # value is substituted, the ceiling comparison that reads it is not.
    (root / "vram-used").write_text("20000000000")
    # The card states its own capacity, so a bundle compares measured occupancy
    # with this reading rather than with a ceiling a profile declares.
    (root / "vram-total").write_text("32000000000")
    # Reuse compiled production code across fixture processes without writing caches into the checkout.
    runtime_source = ROOT / "agents/matuto/matuto_runtime.py"
    runtime_cache = root / "runtime-impl.pyc"
    py_compile.compile(str(runtime_source), cfile=str(runtime_cache), doraise=True)
    # Copied public entry points import this fixture-only observation/deadline seam.
    (package / "matuto_runtime.py").write_text(f'''import importlib.machinery, importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("fixture_runtime_impl", {str(runtime_source)!r},
    loader=importlib.machinery.SourcelessFileLoader("fixture_runtime_impl", {str(runtime_cache)!r}))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
r.__file__ = {str(package / 'matuto_runtime.py')!r}
# Immediate fixture probes still get three seconds on a loaded runner.
assert r.PROBE_SECONDS == 5
r.PROBE_SECONDS = 3
def inventory():
    return json.loads(Path({str(root / 'inventory.json')!r}).read_text())
def identities(backend, environment):
    records = inventory()[backend]
    if backend == "HIP/ROCm" and "ROCR_VISIBLE_DEVICES" in environment:
        records = [x for x in records if x["uuid"] == environment["ROCR_VISIBLE_DEVICES"]]
    return records
_real_pci_devices = r.pci_devices
r.pci_devices = lambda root=None: _real_pci_devices(root) if root is not None else {{"state":"completed", "devices":inventory()["host"]}}
r.bounded_identity_probe = identities
r.vram_telemetry = lambda pci, root=None: Path({str(root / 'vram-used')!r})
r.vram_capacity = lambda pci, root=None: Path({str(root / 'vram-total')!r})
globals().update({{k:v for k,v in vars(r).items() if not k.startswith("__")}})
if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--identity-probe":
        try:
            print(json.dumps(r.identity_probe(sys.argv[2])))
        except r.InspectionError:
            sys.exit(1)
    else:
        sys.exit(r.runtime_cli(sys.argv[1:]) if len(sys.argv)>1 and sys.argv[1] in ("show","select","launch","check","builds","model-root","model-path","installation","speech","speech-check") else r.inspection_cli(sys.argv[1] if len(sys.argv)==2 else None))
''')
    (package / "store.py").write_text('''import os
from pathlib import Path

def store_directory(config_root=None):
    root = config_root or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / "ai-agents-sandbox" / "matuto-manager"
''')
    (agents / "matuto_runtime.py").write_text('''import sys
from matuto import matuto_runtime as runtime
if __name__ == "__main__":
    sys.exit(runtime.runtime_cli(sys.argv[1:]) if len(sys.argv)>1 and sys.argv[1] in ("show","select","launch","check","builds","model-root","model-path","installation","speech","speech-check") else runtime.inspection_cli(sys.argv[1] if len(sys.argv)==2 else None))
else:
    sys.modules[__name__] = runtime
''')
    # Expected flags are the public llama.cpp spellings used by existing profiles.
    help_text = "--version --list-devices --device -dev --model -m --gpu-layers --n-gpu-layers -ngl --ctx-size -c --n-predict -n --seed --temp -p -r -o -d --n-depth --alias --host --port --api-key-file --models-dir --models-preset --models-max --n-cpu-moe --parallel --cache-prompt --cache-type-k --cache-type-v --flash-attn --jinja --metrics --reasoning-format --reasoning-preserve --no-context-shift --timeout --main-gpu --tensor-split --split-mode --spec-type --mmproj --spec-draft-model --device-draft --mmproj-device --override-tensor --api-key --simple-io --single-turn --custom VALUE"
    source = r'''#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <sys/wait.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <errno.h>
static void quote(FILE *out, const char *s) {
    fputc('"',out);
    for (;*s;s++) { if (*s=='"'||*s=='\\') fputc('\\',out); if ((unsigned char)*s<32) fprintf(out,"\\u%04x",(unsigned char)*s); else fputc(*s,out); }
    fputc('"',out);
}
static const char *option(int argc,char **argv,const char *name,const char *fallback) {
    for(int i=1;i+1<argc;i++) if(!strcmp(argv[i],name)) return argv[i+1];
    return fallback;
}
static int present(int argc,char **argv,const char *name) {
    for(int i=1;i<argc;i++) if(!strcmp(argv[i],name)) return 1;
    return 0;
}
/* One llama-bench row: the dimensions this process was asked for, with
   synthetic samples that fall as the measured depth rises. */
static void row(int *first,long prompt,long gen,long depth,long reps,const char *k,const char *v) {
    double base=100.0-(double)depth/10000.0, total=0, squares=0;
    if(base<1.0) base=1.0;
    if(!*first) printf(",\n");
    *first=0;
    printf("  {\"n_prompt\": %ld, \"n_gen\": %ld, \"n_depth\": %ld, \"type_k\": \"%s\", \"type_v\": \"%s\", \"samples_ts\": [",prompt,gen,depth,k,v);
    for(long i=0;i<reps;i++) {
        double value=base+(double)i*0.5;
        total+=value; squares+=value*value;
        printf("%s%.4f",i?", ":"",value);
    }
    double mean=total/(double)reps, variance=reps>1?(squares-total*mean)/(double)(reps-1):0.0;
    printf("], \"avg_ts\": %.4f, \"stddev_ts\": %.4f}",mean,variance>0?sqrt(variance):0.0);
}
static void rows(int argc,char **argv) {
    long prompt=atol(option(argc,argv,"-p","0")), gen=atol(option(argc,argv,"-n","0")), reps=atol(option(argc,argv,"-r","1"));
    const char *depths=option(argc,argv,"-d","0"), *k=option(argc,argv,"--cache-type-k","f16"), *v=option(argc,argv,"--cache-type-v","f16");
    int first=1;
    if(reps<1) reps=1;
    printf("[\n");
    /* A zero-token prompt runs no prompt-processing test, as llama-bench does. */
    if(prompt>0) row(&first,prompt,0,atol(depths),reps,k,v);
    for(const char *cursor=depths;;) {
        row(&first,0,gen,atol(cursor),reps,k,v);
        const char *comma=strchr(cursor,',');
        if(!comma) break;
        cursor=comma+1;
    }
    printf("\n]\n");
}
/* The identity endpoints an owned server answers while a readiness trial times
   it, and the generation endpoint a bundle measures synthesis against. Model
   path and context are echoed from this process's own arguments. */
static long amount(const char *path) {
    FILE *file=fopen(path,"r"); long value=0;
    if(file) { if(fscanf(file,"%ld",&value)!=1) value=0; fclose(file); }
    return value;
}
static long json_int(const char *body,const char *key) {
    const char *found=strstr(body,key), *colon;
    if(!found) return 0;
    colon=strchr(found,':');
    return colon?atol(colon+1):0;
}
/* A request is read until the client's declared body has arrived, so a POST that
   arrives in more than one segment is still seen whole. */
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
/* llama.cpp documents /health as 503 while it loads the model and 200 once it is
   resident, so this window is real time a run must wait through, not a status it
   may skip. The marker states how many more answers are 503, so a suite can hold
   that window open and prove the run waited rather than accepting the first
   answer and measuring a server whose weights had not arrived. */
static int loading(void) {
    FILE *file=fopen(FIXTURE "/loading","r");
    long remaining;
    if(!file) return 0;
    if(fscanf(file,"%ld",&remaining)!=1) remaining=0;
    fclose(file);
    if(remaining<=0) { unlink(FIXTURE "/loading"); return 0; }
    file=fopen(FIXTURE "/loading","w");
    if(file) { fprintf(file,"%ld",remaining-1); fclose(file); }
    return 1;
}
static void answer(int client,int argc,char **argv) {
    const char *model=option(argc,argv,"-m",""), *ctx=option(argc,argv,"-c","0");
    char request[4096];
    ssize_t size=read_request(client,request,sizeof(request));
    FILE *stream;
    if(size<=0) { close(client); return; }
    stream=fdopen(client,"w");
    if(!stream) { close(client); return; }
    if(!strncmp(request,"POST /completion ",17)) {
        /* Generation in flight is the server's own report; the marker is how a
           concurrent reader observes it. A load takes time in proportion to the
           tokens it states, so a long load is genuinely long: a factor is only a
           factor while that load is in flight, and the two are only the same
           thing if the fixture's generation can outlast brief audio. */
        long tokens=json_int(request,"\"n_predict\"");
        int ignore_eos=strstr(request,"\"ignore_eos\": true")||strstr(request,"\"ignore_eos\":true");
        FILE *flag=fopen(FIXTURE "/generating","w");
        if(flag) fclose(flag);
        if(tokens<1) tokens=1;
        if(tokens>60000) tokens=60000;
        /* A real model stops at its own end-of-text long before a large ceiling:
           the host run asked for 512 tokens and the model produced 104. The
           stated load is only the load that runs when the request ignores EOS. */
        if(!ignore_eos&&tokens>104) tokens=104;
        /* The two speech timing probes use a quarter-speed generation load so
           their synthesis margins stay visible without multi-second waits. */
        usleep((useconds_t)tokens*(access(FIXTURE "/generation-fast",F_OK)==0?250:1000));
        unlink(FIXTURE "/generating");
        fputs("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n",stream);
        fprintf(stream,"{\"content\": \"fixture\", \"tokens_predicted\": %ld",tokens>0?tokens:1);
        if(!strcmp(option(argc,argv,"--spec-type",""),"draft-mtp")&&access(FIXTURE "/no-mtp",F_OK)!=0)
            fputs(", \"timings\": {\"draft_n\": 23, \"draft_n_accepted\": 7}",stream);
        fputs("}",stream);
        fclose(stream);
        return;
    }
    if((!strncmp(request,"GET /health ",12)||!strncmp(request,"GET /props ",11))&&loading()) {
        fputs("HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"error\": \"loading model\"}",stream);
        fclose(stream);
        return;
    }
    if(strncmp(request,"GET /health ",12)&&strncmp(request,"GET /props ",11)&&strncmp(request,"GET /slots ",11)) {
        fputs("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n",stream);
        fclose(stream);
        return;
    }
    fputs("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n",stream);
    if(!strncmp(request,"GET /health ",12)) fputs("{\"status\": \"ok\"}",stream);
    else if(!strncmp(request,"GET /slots ",11)) fprintf(stream,"[{\"id\": 0, \"n_ctx\": %s, \"is_processing\": %s}]",ctx,access(FIXTURE "/generating",F_OK)==0?"true":"false");
    else {
        fputs("{\"model_path\": ",stream);
        quote(stream,model);
        fputs(", \"build_info\": \"version: 0.4.0-dev (build 48, commit " REVISION ")\", \"chat_template\": \"fixture\"",stream);
        fprintf(stream,", \"modalities\": {\"vision\": %s}}",present(argc,argv,"--mmproj")&&access(FIXTURE "/no-vision",F_OK)!=0?"true":"false");
    }
    fclose(stream);
}
static int serve(int argc,char **argv) {
    struct sockaddr_in address;
    int listener=socket(AF_INET,SOCK_STREAM,0), reuse=1;
    if(listener<0) return 6;
    setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&reuse,sizeof(reuse));
    memset(&address,0,sizeof(address));
    address.sin_family=AF_INET;
    address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    address.sin_port=htons((unsigned short)atoi(option(argc,argv,"--port","0")));
    if(bind(listener,(struct sockaddr *)&address,sizeof(address))||listen(listener,8)) return 7;
    /* One child per connection, so a request that takes a second to answer does
       not hide the server's own state from a reader waiting on it. */
    for(;;) {
        int client=accept(listener,NULL,NULL);
        pid_t child;
        if(client<0) continue;
        child=fork();
        if(child==0) { answer(client,argc,argv); _exit(0); }
        close(client);
        while(waitpid(-1,NULL,WNOHANG)>0);
    }
}
int main(int argc,char **argv) {
    for(int i=1;i<argc;i++) {
        if(!strcmp(argv[i],"--help")) { if(access(FIXTURE "/unreadable-help",F_OK)==0) return 9; puts(HELP); return 0; }
        if(!strcmp(argv[i],"--version")) { puts("version: 0.4.0-dev (build 48, commit " REVISION ")"); return 0; }
        if(!strcmp(argv[i],"--list-devices")) {
            FILE *out=access(FIXTURE "/record-device-args",F_OK)==0
                ? fopen(FIXTURE "/received.json","w") : NULL;
            if(out) {
                fputs("{\"args\":[",out);
                for(int j=1;j<argc;j++) { if(j>1) fputc(',',out); quote(out,argv[j]); }
                fputs("]}",out); fclose(out);
            }
            puts("Available devices:");
            if(getenv("GGML_VK_VISIBLE_DEVICES")) puts("  Vulkan0: discrete fixture (8192 MiB, 4096 MiB free)");
            else if(getenv("ROCR_VISIBLE_DEVICES")) puts("  ROCm0: discrete fixture (8192 MiB, 4096 MiB free)");
            else {
                puts("  ROCm0: discrete fixture (8192 MiB, 4096 MiB free)");
                if(access(FIXTURE "/multiple-hip",F_OK)==0) puts("  ROCm1: second fixture (8192 MiB, 4096 MiB free)");
                puts("  Vulkan0: discrete fixture (8192 MiB, 4096 MiB free)");
            }
            return 0;
        }
    }
    /* The smoke modes exercise the CLI's structured output file and console without a GPU. */
    if(!strcmp(argv[0],"llama-cli") && present(argc,argv,"-p")) {
        const char *reply = access(FIXTURE "/smoke-empty-reply",F_OK)==0 ? "" :
            access(FIXTURE "/smoke-wrong",F_OK)==0 ? "not the requested text" : "ROCm smoke test OK";
        FILE *transcript;
        if(access(FIXTURE "/smoke-fails",F_OK)==0) return 1;
        if(access(FIXTURE "/smoke-no-reply",F_OK)==0) return 0;
        if(access(FIXTURE "/smoke-extra",F_OK)==0) reply = "ROCm smoke test OK\nunexpected extra reply";
        puts("fixture CLI banner");
        puts(reply);
        transcript=fopen(option(argc,argv,"-o",""),"w");
        if(transcript) {
            fprintf(transcript,"User:\n%s\n\nAssistant:\n",option(argc,argv,"-p",""));
            if(access(FIXTURE "/smoke-overflow",F_OK)==0) {
                static char padding[1100000];
                memset(padding, 'x', sizeof(padding));
                fputs("ROCm smoke test OK",transcript);
                fwrite(padding,1,sizeof(padding),transcript);
                fputs("\n\n",transcript);
            } else fprintf(transcript,"%s\n\n",reply);
            fclose(transcript);
        }
        if(access(FIXTURE "/smoke-overflow",F_OK)==0) {
            static char padding[1100000];
            memset(padding, '\n', sizeof(padding));
            fwrite(padding, 1, sizeof(padding), stdout);
        }
    }
    /* Publishing the PID also proves the depth process already ignores TERM. */
    if(access(FIXTURE "/ignore-term",F_OK)==0 && present(argc,argv,"-d")) signal(SIGTERM,SIG_IGN);
    FILE *out=fopen(FIXTURE "/received.json","w");
    if(!out) return 3;
    fprintf(out,"{\"pid\":%d,\"args\":[",getpid());
    for(int i=1;i<argc;i++) { if(i>1) fputc(',',out); quote(out,argv[i]); }
    fputs("],\"hip\":",out); quote(out,getenv("ROCR_VISIBLE_DEVICES")?:"");
    fputs(",\"vulkan\":",out); quote(out,getenv("GGML_VK_VISIBLE_DEVICES")?:"");
    fputs(",\"identity\":",out); quote(out,getenv("MATUTO_RUNTIME_IDENTITY")?:"");
    char cwd[PATH_MAX]; if(!getcwd(cwd,sizeof(cwd))) return 5;
    fputs(",\"cwd\":",out); quote(out,cwd);
    fputs("}",out); fclose(out);
    /* A served model holds device memory for as long as it runs; the amount is
       the fixture's stand-in for an allocation this machine cannot make. */
    if(present(argc,argv,"--port")&&access(FIXTURE "/vram-model",F_OK)==0) {
        long held=amount(FIXTURE "/vram-used")+amount(FIXTURE "/vram-model");
        FILE *used=fopen(FIXTURE "/vram-used","w");
        if(used) { fprintf(used,"%ld",held); fclose(used); }
    }
    /* Stand in for a measurement that allocates device memory and does not release
       it the instant it exits, so a baseline read afterwards would be its memory. */
    if(access(FIXTURE "/vram-grow",F_OK)==0 && present(argc,argv,"-o")) {
        FILE *used=fopen(FIXTURE "/vram-used","w");
        if(used) { fprintf(used,"25000000000"); fclose(used); }
    }
    /* Only the depth invocation hangs, so the pinned row still completes first. */
    while(access(FIXTURE "/hold-depth",F_OK)==0 && present(argc,argv,"-d")) usleep(10000);
    while(!strcmp(argv[0],"llama-cli") && access(FIXTURE "/hold-smoke",F_OK)==0) usleep(10000);
    if(access(FIXTURE "/bench-json",F_OK)==0 && present(argc,argv,"-o")) { rows(argc,argv); return 0; }
    if(access(FIXTURE "/http",F_OK)==0 && present(argc,argv,"--port")) return serve(argc,argv);
    while(access(FIXTURE "/hold",F_OK)==0) {
        if(access(FIXTURE "/spawn",F_OK)==0) {
            char path[PATH_MAX]; ssize_t size=readlink("/proc/self/exe",path,sizeof(path)-1); if(size<0) return 4; path[size]=0;
            unlink(FIXTURE "/spawn");
            pid_t child=fork();
            if(child==0) { char *args[]={path,"--version",NULL}; execv(path,args); _exit(errno==EACCES?42:43); }
            int status; waitpid(child,&status,0); out=fopen(FIXTURE "/child-status","w"); fprintf(out,"%d",WEXITSTATUS(status)); fclose(out);
        }
        usleep(10000);
    }
    return 0;
}
'''
    (root / "fixture.c").write_text(source)
    subprocess.run(["cc", "-Wall", "-Werror", f"-DFIXTURE={json.dumps(str(root))}", f"-DHELP={json.dumps(help_text)}", f"-DREVISION={json.dumps(REPORTED_REVISION)}", str(root / "fixture.c"), "-o", str(build / "llama-server"), "-lm"], check=True)
    for name in ("llama-cli", "llama-bench", "llama-perplexity"):
        shutil.copy2(build / "llama-server", build / name)
    # The speech runtimes take their model operand positionally, as
    # Breeze-TTS-2.cpp does, and answer synthesis as raw mono PCM.
    subprocess.run(["cc", "-Wall", "-Werror", f"-DFIXTURE={json.dumps(str(root))}", str(ROOT / "agents/tests/speech_fixture.c"), "-o", str(build / "breeze-server")], check=True)
    shutil.copy2(build / "breeze-server", build / "whisper-server")
    return agents, build


def configure(agents, build, environment, backend="HIP/ROCm"):
    subprocess.run([sys.executable, "-B", str(agents / "matuto_runtime.py"), "select", str(build), backend, PCI], env=environment, check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    create(sys.argv[1])
