# GPU7 CPX/NPS4 experiment — live results

## Current state

GPU7 (`0000:f6:00.0`) is **CPX/NPS4**. The other seven physical GPUs remain
**SPX/NPS1**. The v2 experimental module is currently loaded with:

```text
nps_test_bdf=0000:f6:00.0
nps_test_hive_reset=Y
```

This is a temporary in-memory driver substitution. The installed DKMS modules,
modprobe configuration, and initramfs were not replaced. Reboot behavior and
persistence of partition settings have not been validated. The test module has
not been configured for automatic loading at boot.

The user accepted that transfers between GPU7 and the other physical GPUs need
not work. Testing therefore treats GPU7 as an isolated workload device.
No test workloads or test containers remain running.

## Open a GPU7-only workload container

```bash
python3 /users/g893user/amdgpu-nps-test-20260930/gpu7_container.py
```

The launcher verifies CPX/NPS4, derives the eight render nodes from KFD topology,
and passes only those nodes plus `/dev/kfd` to the ROCm 7.2.4 image. Currently
these are `/dev/dri/renderD160` through `renderD167`. It mounts this experiment
directory read-only as `/test`; it grants no writable sysfs or module-loading
capability to workload containers.

To repeat the bounded container check:

```bash
python3 /users/g893user/amdgpu-nps-test-20260930/gpu7_container.py \
  /test/check_gpu stress 15
```

## What passed

| Test | Result |
| --- | --- |
| Original CPX/NPS1 baseline on host | Local checks on 15 logical GPUs and all 210 directed peer paths passed |
| Mixed CPX/NPS4 + SPX/NPS1 enumeration | 15 logical GPUs present; only GPU7 changed NPS mode |
| Mixed-mode local memory and kernels | All 15 logical GPUs passed |
| Isolated host peer checks | 98 directed paths passed: 56 within GPU7 and 42 among the other seven GPUs |
| Independent concurrent host work | All 15 logical GPUs passed a 15-second run |
| GPU7-only Docker enumeration | Exactly eight logical GPUs, all on physical GPU7 |
| GPU7-only Docker internal peers | All 56 directed paths passed |
| GPU7-only Docker concurrent work | All eight partitions passed a 15-second run |
| Request from inside Docker | CPX and NPS4 commands succeeded; kernel logs showed target-only NPS4 firmware request |
| Target-only NPS1 rollback | Requested from Docker, accepted by firmware, and verified before returning to NPS4 |

Local tests cover allocation, host-to-device/device-to-host round trips, a
pattern-generating GPU kernel, and CPU comparison of every copied word.
Each peer path copies 4 MiB and compares every word. Concurrent tests repeatedly
launch kernels and verify the final buffer on each device. These are bounded
smoke tests, not full-memory coverage, long-duration stability, NCCL validation,
or validation of every application synchronization pattern.

## What failed or remains unresolved

**Cross-GPU7 peer transfers are not usable in this experiment.** With mixed NPS
modes, a copy from HIP device 4 (`0000:f6:00.0`, GPU7 partition 0, NPS4) to HIP
device 0 (`0000:76:00.0`, host GPU3, NPS1) reproducibly returned wrong data at
word 1024 (byte offset 4096): actual `4294967295`, expected `387277981`.
The corresponding all-NPS1 baseline passed. The diagnostic aborted on that
first failure; it did not exhaustively characterize every cross-group direction.
The isolated test deliberately excludes all 112 cross-group paths.

The first request-routing-only patch accepted the firmware request but left
GPU7 in minimal initialization, with UNKNOWN compute mode and no KFD device.
Investigation showed a driver assumption: only devices needing reset call the
hive-reset rendezvous, and the call schedules reset only once all members are
present. GPU7 probed before the last member, whose unchanged NPS mode meant it
never called the rendezvous.

The **v2 patch** adds `nps_test_hive_reset=1`, gated by a nonempty test BDF. It
makes the devices participate in the existing minimal-init/shared-reset path,
while preserving firmware reset detection and its side effects. It sends no
NPS request to other GPUs. This completed the mixed-mode transition.

A kernel warning about duplicate sysfs `xcp` registration (`-EEXIST`) appeared
when reloading after the incomplete first initialization, and recurred on later
reloads. Its cause has not been repaired or proven harmless; a clean-boot test
of v2 has not been performed. It did not prevent the listed correctness tests
from passing. The driver remains experimental.

## Files and evidence

- `v2-upstream.patch`: final patch against the user's `/tmp/amdgpu` checkout.
- `v2-dkms.patch`: final patch against the pristine installed DKMS source.
- `v2-source/amd/amdgpu/amdgpu_gmc.c`: clean patched source.
- `patched-amdgpu-v2.ko`: currently loaded experimental module.
- `stock-modules/`: copies of the installed modules for recovery.
- `baseline-correctness.log`: initial 210-path baseline success.
- `mixed-peer-diagnostic.log`: reproducible cross-GPU copy failure.
- `isolated-correctness.log`: 98-path host and independent concurrent success.
- `docker-correctness.log`: eight-partition container success.
- `docker-nps4-state.json`, `final-state.json`: physical modes and KFD topology.
- `final-kernel.log`: driver events from the start of the experiment.
- `v2-module-validation.json`: matching vermagic and all 1,110 imported CRCs.
- `v2-routing-tests.log`: 16 offline cases per source version, all passing.

`upstream.patch`, `dkms.patch`, and `patched-amdgpu.ko` without `v2` are retained
only as evidence of the first attempt. Do not use them for another mixed-NPS
transition. `README.md` is the original plan; this file supersedes its live status
and the reload details below supersede its v1 procedure.

## Partition changes still require host maintenance

Docker can issue the request through GPU7's writable sysfs directory, but it
does not provide a private kernel driver. Stop workloads on all eight GPUs and
coordinate the host reload before making another NPS transition.

For a management container only, use the real sysfs path and disable its
AppArmor profile (both were necessary for writes on this host):

```bash
gpu7_sysfs=$(readlink -f /sys/bus/pci/devices/0000:f6:00.0)
sudo docker run --rm -it \
  --device=/dev/kfd --device=/dev/dri/renderD160 \
  --security-opt apparmor=unconfined \
  --mount "type=bind,src=$gpu7_sysfs,dst=$gpu7_sysfs" \
  rocm/dev-ubuntu-24.04:7.2.4-complete
```

Inside that container, the physical GPU7 partition-0 handle is GPU 0. Verify
the BDF with `amd-smi list` before using `amd-smi set --gpu 0 --memory-partition
NPS4` or `NPS1`. This requires the v2 host module and its parameters to be loaded
already. Exit the management container before host driver removal.

## Recovery sequence

The verified NPS4-to-NPS1 rollback requires **v2 to perform the reset as well as
send the request**. Do not load stock immediately after queuing the single-GPU
rollback: it has the same reset-rendezvous assumption found in v1.

During a maintenance window, with every GPU idle:

1. Under v2, request NPS1 on `0000:f6:00.0`; verify target-only queue log.
2. Remove AMDGPU normally; verify GPU7-only NPS1 firmware request returned zero.
3. Load dependencies and **v2 again with both test parameters** to complete the
   shared reset. Confirm all eight GPUs are NPS1 and enumerate correctly.
4. With no pending NPS change, remove v2 normally and load stock with
   `sudo modprobe amdgpu`.
5. Restore the desired compute modes. The pre-test state was GPU7 CPX and all
   other physical GPUs SPX. Verify memory/kernel/peer correctness afterward.

The dependency and v2 load commands are:

```bash
sudo modprobe -a drm_display_helper amdkcl amdttm cec amd-sched video \
  drm_suballoc_helper amddrm_exec i2c-algo-bit amdxcp amddrm_buddy \
  amddrm_ttm_helper
sudo insmod /users/g893user/amdgpu-nps-test-20260930/patched-amdgpu-v2.ko \
  nps_test_bdf=0000:f6:00.0 nps_test_hive_reset=1
```

Do not force module removal or use `amd-smi reset -r` to select this staged
module: automatic loading resolves to the unchanged installed stock module.
Changing NPS modes can require console recovery if initialization fails.
