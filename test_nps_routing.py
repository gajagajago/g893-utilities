#!/usr/bin/env python3
"""Compile real NPS functions with fake devices/firmware; never touch GPUs."""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
SOURCES = [
    pathlib.Path('/tmp/amdgpu/drivers/gpu/drm/amd/amdgpu/amdgpu_gmc.c'),
    ROOT / 'v2-source/amd/amdgpu/amdgpu_gmc.c',
]
SOURCES = [path for path in SOURCES if path.exists()]
assert SOURCES, 'No saved source found'


def extract(source, signature):
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


PREAMBLE = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#define BIT(x) (1UL << (x))
#define AMD_IS_APU 1
#define WRITE_ONCE(x, v) ((x) = (v))
#define xchg(p, v) __extension__ ({ __typeof__(*(p)) old = *(p); *(p) = (v); old; })
#define for_each_inst(i, mask) for ((i) = 0; (i) < 16; ++(i)) if ((mask) & BIT(i))
static unsigned log_count;
#define dev_warn(dev, ...) ((void)(dev), ++log_count)
#define dev_err(dev, ...) ((void)(dev), ++log_count)
#define dev_info(dev, ...) ((void)(dev), ++log_count)
enum amdgpu_memory_partition { UNKNOWN_MEMORY_PARTITION_MODE = 0, NPS1 = 1, NPS2 = 2, NPS4 = 4 };
struct amdgpu_device;
struct pci_dev { const char *name; };
struct device { void *data; };
struct device_attribute { int unused; };
struct drm_device { struct amdgpu_device *adev; };
struct psp_context { void *funcs; struct amdgpu_device *owner; };
struct amdgpu_gmc_funcs {
    enum amdgpu_memory_partition (*query_mem_partition_mode)(struct amdgpu_device *);
    int (*request_mem_partition_mode)(struct amdgpu_device *, int);
    bool (*need_reset_on_init)(struct amdgpu_device *);
};
struct amdgpu_hive_info { int requested_nps_mode; };
struct amdgpu_device {
    struct device *dev;
    struct pci_dev *pdev;
    struct psp_context psp;
    unsigned flags;
    bool vf, in_hive, pending_reset;
    int reset_checks;
    enum amdgpu_memory_partition active_mode;
    struct {
        enum amdgpu_memory_partition requested_nps_mode;
        unsigned supported_nps_modes;
        const struct amdgpu_gmc_funcs *gmc_funcs;
        struct { unsigned num_physical_nodes; } xgmi;
    } gmc;
};
static char *amdgpu_nps_test_bdf;
static bool amdgpu_nps_test_hive_reset;
static const char *nps_desc[16] = { [1] = "NPS1", [2] = "NPS2", [4] = "NPS4", [8] = "NPS8" };
static struct amdgpu_device gpus[8];
static struct device devices[8];
static struct drm_device drms[8];
static struct pci_dev pcis[8];
static const char *bdfs[8] = { "0000:06:00.0", "0000:16:00.0", "0000:66:00.0", "0000:76:00.0", "0000:86:00.0", "0000:96:00.0", "0000:e6:00.0", "0000:f6:00.0" };
static struct amdgpu_hive_info hive;
static int fw_calls, hive_calls, fw_rc, last_mode;
static struct amdgpu_device *last_gpu;
static const char *pci_name(struct pci_dev *p) { return p->name; }
static struct drm_device *dev_get_drvdata(struct device *d) { return d->data; }
static struct amdgpu_device *drm_to_adev(struct drm_device *d) { return d->adev; }
static bool amdgpu_sriov_vf(struct amdgpu_device *a) { return a->vf; }
static int atomic_read(int *p) { return *p; }
static void atomic_set(int *p, int v) { *p = v; }
static struct amdgpu_hive_info *amdgpu_get_xgmi_hive(struct amdgpu_device *a) { return a->in_hive ? &hive : NULL; }
static void amdgpu_put_xgmi_hive(struct amdgpu_hive_info *h) { (void)h; }
static int psp_memory_partition(struct psp_context *p, int mode) {
    ++fw_calls; last_gpu = p->owner; last_mode = mode; return fw_rc;
}
int amdgpu_gmc_request_memory_partition(struct amdgpu_device *, int);
static int amdgpu_xgmi_request_nps_change(struct amdgpu_device *a, struct amdgpu_hive_info *h, int mode) {
    (void)a; ++hive_calls;
    for (int i = 0; i < 8; ++i) {
        int r = amdgpu_gmc_request_memory_partition(&gpus[i], mode);
        if (r) return r;
    }
    h->requested_nps_mode = 0; return 0;
}
'''

TESTS = r'''
static enum amdgpu_memory_partition query_mode(struct amdgpu_device *a) { return a->active_mode; }
static bool query_reset(struct amdgpu_device *a) { ++a->reset_checks; return a->pending_reset; }
static const struct amdgpu_gmc_funcs funcs = { query_mode, amdgpu_gmc_request_memory_partition, query_reset };
static void reset(char *selector) {
    memset(gpus, 0, sizeof(gpus));
    memset(&hive, 0, sizeof(hive));
    amdgpu_nps_test_bdf = selector;
    amdgpu_nps_test_hive_reset = false;
    fw_calls = hive_calls = fw_rc = last_mode = 0; last_gpu = NULL;
    for (int i = 0; i < 8; ++i) {
        pcis[i].name = bdfs[i]; devices[i].data = &drms[i]; drms[i].adev = &gpus[i];
        gpus[i].dev = &devices[i]; gpus[i].pdev = &pcis[i]; gpus[i].in_hive = true;
        gpus[i].psp.owner = &gpus[i]; gpus[i].psp.funcs = (void *)&funcs;
        gpus[i].gmc.gmc_funcs = &funcs;
        gpus[i].gmc.xgmi.num_physical_nodes = 8;
        gpus[i].gmc.supported_nps_modes = BIT(1) | BIT(2) | BIT(4);
        gpus[i].active_mode = NPS1;
    }
}
static ssize_t request(int gpu, const char *mode) {
    return current_memory_partition_store(&devices[gpu], NULL, mode, strlen(mode));
}
static void teardown_all(void) {
    for (int i = 0; i < 8; ++i) amdgpu_gmc_prepare_nps_mode_change(&gpus[i]);
}
int main(void) {
    reset(NULL);
    assert(request(7, "NPS4") == 4);
    assert(hive.requested_nps_mode == NPS4 && fw_calls == 0);
    teardown_all(); assert(hive_calls == 1 && fw_calls == 8);
    puts("PASS disabled: original hive-wide request path");

    reset(""); request(7, "NPS4"); teardown_all();
    assert(hive_calls == 1 && fw_calls == 8);
    puts("PASS empty selector: original behavior");

    reset("0000:f6:00.0");
    assert(request(7, "NPS4") == 4);
    assert(hive.requested_nps_mode == 0 && fw_calls == 0);
    teardown_all();
    assert(fw_calls == 1 && hive_calls == 0 && last_gpu == &gpus[7] && last_mode == NPS4);
    teardown_all(); assert(fw_calls == 1);
    puts("PASS target-only: exactly one firmware request, only during teardown");

    reset("0000:f6:00.0");
    for (int i = 0; i < 7; ++i) {
        assert(request(i, "NPS4") == -EPERM);
        assert(amdgpu_gmc_request_memory_partition(&gpus[i], NPS4) == -EPERM);
    }
    teardown_all(); assert(fw_calls == 0 && hive_calls == 0);
    puts("PASS other GPUs: sysfs and firmware-boundary requests rejected");

    reset("0000:f6:00.0"); request(7, "NPS4"); request(7, "NPS1");
    teardown_all(); assert(fw_calls == 0 && hive_calls == 0);
    puts("PASS cancellation: writing active mode clears pending change");

    reset("0000:f6:00.0"); request(7, "NPS4"); request(7, "NPS2");
    teardown_all(); assert(fw_calls == 1 && last_mode == NPS2);
    puts("PASS replacement: latest valid request wins");

    reset("0000:f6:00.0");
    assert(request(7, "NPS8") == -EINVAL);
    assert(request(7, "garbage") == -EINVAL);
    teardown_all(); assert(fw_calls == 0);
    puts("PASS unsupported modes: no firmware requests");

    reset("0000:f6:00.0"); hive.requested_nps_mode = NPS4;
    for (int i = 0; i < 7; ++i) gpus[i].gmc.requested_nps_mode = NPS4;
    teardown_all(); assert(fw_calls == 0 && hive_calls == 0);
    puts("PASS stale non-target/hive requests: never broadcast in test mode");

    reset("bad-selector");
    for (int i = 0; i < 8; ++i) assert(request(i, "NPS4") == -EPERM);
    teardown_all(); assert(fw_calls == 0 && hive_calls == 0);
    puts("PASS invalid selector: fails closed");

    reset("0000:f6:00.0"); gpus[7].vf = true;
    assert(request(7, "NPS4") == -EOPNOTSUPP); teardown_all(); assert(fw_calls == 0);
    reset("0000:f6:00.0"); gpus[7].flags = AMD_IS_APU;
    assert(request(7, "NPS4") == -EOPNOTSUPP);
    puts("PASS VF/APU: experimental requests rejected");

    reset("0000:f6:00.0"); fw_rc = -EIO;
    assert(amdgpu_gmc_request_memory_partition(&gpus[7], NPS4) == -EIO);
    assert(fw_calls == 1 && hive_calls == 0);
    puts("PASS firmware failure: error propagated, no broadcast fallback");

    reset("0000:f6:00.0"); gpus[7].active_mode = NPS4;
    request(7, "NPS1"); teardown_all();
    assert(fw_calls == 1 && last_gpu == &gpus[7] && last_mode == NPS1);
    puts("PASS rollback request: target-only NPS4 to NPS1");

    reset(NULL); amdgpu_nps_test_hive_reset = true;
    assert(!amdgpu_gmc_need_reset_on_init(&gpus[0]));
    gpus[0].pending_reset = true;
    assert(amdgpu_gmc_need_reset_on_init(&gpus[0]));
    assert(gpus[0].reset_checks == 2);
    puts("PASS reset option alone: original firmware detection preserved");

    reset("0000:f6:00.0");
    assert(!amdgpu_gmc_need_reset_on_init(&gpus[0]));
    puts("PASS target selector alone: no forced reset");

    reset("0000:f6:00.0"); amdgpu_nps_test_hive_reset = true;
    gpus[7].pending_reset = true;
    for (int i = 0; i < 8; ++i) {
        assert(amdgpu_gmc_need_reset_on_init(&gpus[i]));
        assert(gpus[i].reset_checks == 1);
    }
    assert(fw_calls == 0 && hive_calls == 0);
    puts("PASS mixed reset: all members rendezvous, firmware detection retained, no NPS writes");

    reset("0000:f6:00.0"); amdgpu_nps_test_hive_reset = true;
    gpus[0].vf = true;
    gpus[1].flags = AMD_IS_APU;
    gpus[2].gmc.xgmi.num_physical_nodes = 1;
    gpus[3].gmc.xgmi.num_physical_nodes = 0;
    for (int i = 0; i < 4; ++i) assert(!amdgpu_gmc_need_reset_on_init(&gpus[i]));
    puts("PASS forced reset excludes VFs, APUs, and non-hive devices");
}
'''

SIGNATURES = [
    'static bool amdgpu_gmc_nps_test_enabled(',
    'static bool amdgpu_gmc_nps_test_target(',
    'static ssize_t current_memory_partition_store(',
    'int amdgpu_gmc_request_memory_partition(',
    'static inline bool amdgpu_gmc_need_nps_switch_req(',
    'void amdgpu_gmc_prepare_nps_mode_change(',
    'bool amdgpu_gmc_need_reset_on_init(',
]

for source_path in SOURCES:
    source = source_path.read_text()
    actual_functions = '\n'.join(extract(source, s) for s in SIGNATURES)
    with tempfile.TemporaryDirectory(prefix='nps-routing-', dir=ROOT) as tmp:
        c_path = pathlib.Path(tmp) / 'routing.c'
        executable = pathlib.Path(tmp) / 'routing'
        c_path.write_text(PREAMBLE + actual_functions + TESTS)
        subprocess.run(['gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-fsanitize=undefined',
                        '-o', str(executable), str(c_path)], check=True)
        print(f'\nSource: {source_path}', flush=True)
        subprocess.run([str(executable)], check=True)
