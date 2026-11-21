/*
 * PGO counts from a running instrumented build (-DMANHUNT_PGO=generate).
 *
 * Every object is compiled with -fprofile-generate -fprofile-info-section=gcov_info,
 * so it registers its counters in the section "gcov_info" and libgcov writes
 * nothing at exit (its exit path assumes a host file system, and the title is
 * usually ended by the auto exit or HOME). This file streams all of them into one
 * file with GCC's __gcov_info_to_gcda() (gcov.h):
 *     sdmc:/switch/manhunt1_nx/pgo/manhunt_<run id>.stream
 * replaced by each snapshot (counts only grow, so the latest holds the whole
 * launch). Snapshots: after 60 s, then every 60 s, and at the auto exit.
 * tools/pgo_merge.py turns the streams into .gcda files for -DMANHUNT_PGO=use.
 *
 * The streaming approach follows sa2-nx's and renut-nx's notes.
 */
#ifdef MANHUNT_PGO_GENERATE
#include <gcov.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <switch.h>

#include "sd_layout.h"

extern const struct gcov_info *const __start_gcov_info[] __attribute__((weak));
extern const struct gcov_info *const __stop_gcov_info[] __attribute__((weak));

static Mutex s_mutex;
static unsigned long long s_run;
static unsigned s_snapshots;

static void dump_fn(const void *data, unsigned length, void *arg) { fwrite(data, 1, length, (FILE *)arg); }
static void filename_fn(const char *name, void *arg) { __gcov_filename_to_gcfn(name, dump_fn, arg); }
static void *allocate_fn(unsigned length, void *arg) { (void)arg; return malloc(length); }

void manhunt_pgo_snapshot(const char *why)
{
    if (!__start_gcov_info || !__stop_gcov_info)
        return;
    mutexLock(&s_mutex);
    char path[160], tmp[168];
    snprintf(path, sizeof path, MANHUNT_SD_BASE "/pgo/manhunt_%016llX.stream", s_run);
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    const u64 t0 = armGetSystemTick();
    FILE *f = fopen(tmp, "wb");
    if (!f) {
        printf("[PGO] cannot open %s\n", tmp);
        mutexUnlock(&s_mutex);
        return;
    }
    static char buffer[1 << 20];
    setvbuf(f, buffer, _IOFBF, sizeof buffer);
    unsigned objects = 0;
    for (const struct gcov_info *const *p = __start_gcov_info; p < __stop_gcov_info; ++p, ++objects)
        __gcov_info_to_gcda(*p, filename_fn, dump_fn, allocate_fn, f);
    const long bytes = ftell(f);
    const int ok = fclose(f) == 0;
    remove(path);
    const int renamed = ok && rename(tmp, path) == 0;
    s_snapshots++;
    printf("[PGO] snapshot %u (%s): %u objects, %.1f MB, %llu ms, %s\n", s_snapshots, why, objects,
           bytes / 1048576.0, (unsigned long long)(armTicksToNs(armGetSystemTick() - t0) / 1000000ull),
           renamed ? path : "WRITE FAILED");
    mutexUnlock(&s_mutex);
}

static void pgo_thread(void *arg)
{
    (void)arg;
    svcSleepThread(60ll * 1000000000ll);
    for (;;) {
        manhunt_pgo_snapshot("periodic");
        svcSleepThread(60ll * 1000000000ll);
    }
}

void manhunt_pgo_start(void)
{
    s_run = armGetSystemTick();
    mutexInit(&s_mutex);
    mkdir(MANHUNT_SD_BASE "/pgo", 0777);
    const long n = (__start_gcov_info && __stop_gcov_info) ? (long)(__stop_gcov_info - __start_gcov_info) : -1;
    printf("[PGO] instrumented build: %ld objects registered, run %016llX\n", n, s_run);
    static Thread t;
    static const int prios[] = {0x2C, 0x3B};
    Result rc = 1;
    for (unsigned i = 0; i < sizeof prios / sizeof prios[0] && R_FAILED(rc); i++) {
        rc = threadCreate(&t, pgo_thread, NULL, NULL, 0x10000, prios[i], -2);
        if (R_SUCCEEDED(rc))
            rc = threadStart(&t);
    }
    printf("[PGO] snapshot thread rc=0x%X (every 60 s and at the auto exit)\n", rc);
}
#endif
