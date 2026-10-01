# GPU7-only NPS experiment: prepared, not run

**Historical preparation plan. The live experiment has now run. See
[RESULTS.md](RESULTS.md) for the current state, v2 module, known failures, and
corrected rollback procedure. Do not use the v1 module for mixed-NPS changes.**

The patch and modules in this directory are an **unvalidated hardware experiment**.
No test module has been installed or loaded, and no NPS request has been issued.
Loading the module does not itself request an NPS change. A later sysfs/AMD SMI
write queues the request; driver removal sends it to firmware.

## Sources and scope

- User checkout: `/tmp/amdgpu`, commit
  `48f2f4486a8dc6122195fa8755b6a1a6ee2ad3e2` (Linux 7.1 development tree).
- Build source: isolated copies of
  `/usr/src/amdgpu-6.16.13-2341068.24.04`.
- Build target: `6.8.0-138-generic`, the running host kernel.
- Experimental target: physical PCI device `0000:f6:00.0` (host GPU7).
- `upstream.patch`: change in the user checkout.
- `dkms.patch`: the same change against pristine installed DKMS source.
- Built test module: `patched/amd/amdgpu/amdgpu.ko`.
- Baseline build: `baseline/amd/amdgpu/amdgpu.ko`.
- Stock module backup: `stock-modules/`; decompressed stock AMDGPU: `stock-amdgpu.ko`.
- `host-baseline.json`: read-only snapshot. At capture, GPU7 was CPX/NPS1;
  the other seven GPUs were SPX/NPS1. These modes were not set by this preparation.

Only `amdgpu_gmc.c` is patched. The opt-in, read-only module parameter
`nps_test_bdf=0000:f6:00.0` changes the request routing, not the shared XGMI
reset/initialization path. It:

1. Queues NPS requests on the selected physical device instead of the hive.
2. Rejects NPS requests on all other devices while the option is enabled.
3. Checks the target again at the firmware-request boundary and logs the result.
4. Avoids the hive-wide request path during removal.
5. Cancels a pending request when the active mode is written back.

Without this parameter (or with an empty string), the original behavior remains.
An incorrect, nonempty BDF rejects all NPS changes instead of broadcasting.
The option is experimental for this MI300X host; other GPU models are not validated.

## Offline validation

Both complete DKMS module builds succeeded. The patched build has the same
warnings as the unmodified build; no added compile warnings were found.
Kernel patch style and whitespace checks pass.

`test_nps_routing.py` compiles the actual modified C functions with fake GPU and
firmware interfaces. Twelve cases pass for each source version: normal behavior,
empty parameter, target-only request, non-target rejection, cancellation,
replacement, invalid modes, stale hive/other-device requests, bad selector,
VF/APU rejection, firmware error propagation, and target-only rollback request.
This does not exercise kernel locking, hardware, firmware, or XGMI operation.

`module-validation.json` compares stock, baseline, and experimental AMDGPU modules.
All 1,110 imported symbol CRCs and kernel vermagic match. There are no added or
removed imports. This is an ABI prerequisite, not proof that the module will load
or operate correctly. This Ubuntu kernel uses variable-length `__versions`
records; the installed `modprobe --show-modversions` cannot parse them, so
`prepare_artifacts.py` reads the format defined by the installed kernel headers.

Secure Boot is disabled and kernel lockdown is `none`. The test module is unsigned.

Re-run the offline routing checks:

```bash
python3 /tmp/amdgpu-nps-test/test_nps_routing.py
```

To reproduce the DKMS build, copy pristine `/usr/src/...` into a NEW directory,
apply `dkms.patch` with `patch -p1`, and run:

```bash
make KERNELVER=6.8.0-138-generic num_cpu_cores=8
```

Do not rerun this top-level build in an already configured tree: the vendor
pre-build script rewrites source files. Incremental builds of the existing tree
can use the kernel build directly:

```bash
make -j8 -C /lib/modules/6.8.0-138-generic/build \
  M=/tmp/amdgpu-nps-test/patched TTM_NAME=amdttm SCHED_NAME=amd-sched
```

## Live test gate

**Do not execute the following stages until the user authorizes the maintenance
window.** Driver removal/reset affects the entire host GPU stack, even though
the NPS firmware request will target GPU7 only. The experiment cannot promise
that other GPUs' workloads keep running or that their compute modes survive a
reload. Baseline compute modes must be explicitly restored afterward.

Before proceeding:

- Reserve all eight GPUs; stop workloads, GPU containers, and GPU monitoring
  processes through their owners. Do not blindly kill processes.
- Confirm `/dev/kfd` and `/dev/dri/*` are not held open, including by other users.
- Have an independent SSH session AND working out-of-band console/power control.
- Save kernel logs, AMD SMI enumeration, active driver options, and fresh partition
  states. Compare with `host-baseline.json`; stop on unexpected differences.
- Check for pending NPS changes from earlier commands. Public sysfs reports the
  active mode, not necessarily a queued hive request. If the existing driver has
  an unknown pending request, do not treat its removal as harmless.
- Inspect `modprobe --show-depends amdgpu` and applicable module options. Carry
  required host-specific options into `insmod`; it does not read modprobe config.
- Verify recovery artifacts survive reboot: `/tmp` may be cleared at boot.
  Copy this experiment bundle to persistent storage before live testing. The
  installed stock modules themselves are left untouched by this procedure.

## Stage A: load the experimental module, without requesting NPS4

Use the host, not Docker, for the first experiment. No module installation or
initramfs update is required. First remove the stock module normally:

```bash
sudo modprobe -r amdgpu
```

If removal fails, stop and investigate; do not force-unload modules.
Preload the stock dependencies, whose imported CRCs match the test module:

```bash
sudo modprobe -a drm_display_helper amdkcl amdttm cec amd-sched video \
  drm_suballoc_helper amddrm_exec i2c-algo-bit amdxcp amddrm_buddy \
  amddrm_ttm_helper
sudo insmod /tmp/amdgpu-nps-test/patched/amd/amdgpu/amdgpu.ko \
  nps_test_bdf=0000:f6:00.0
```

Verify `/sys/module/amdgpu/parameters/nps_test_bdf` and enumeration. All eight
physical GPUs should still report NPS1. Inspect kernel logs before continuing.
Re-query available accelerator/memory profiles; restore GPU7's CPX compute mode
if needed, while the hive is idle. Keep all other workloads stopped throughout.

## Stage B: queue and apply GPU7's request

Use the exact PCI address, since numerical indices and render nodes may change:

```bash
sudo amd-smi set --gpu 0000:f6:00.0 --memory-partition NPS4
```

Confirm the loaded driver still exposes the test parameter. Confirm a
`NPS-TEST: queued per-device mode=4` message on `0000:f6:00.0`. Do not use
`amd-smi reset -r` here, because its automatic reload would select the installed
stock module rather than the staged experimental module.

If aborting BEFORE removal, cancel the queued request by writing the still-active
mode directly (AMD SMI may skip an unchanged-mode write):

```bash
printf 'NPS1\n' | sudo tee \
  /sys/bus/pci/devices/0000:f6:00.0/current_memory_partition
```

Confirm `NPS-TEST: queued per-device mode=0`. Otherwise do not assume cancellation.

To apply, remove the experimental module normally, then repeat the dependency
and `insmod` commands from Stage A. During removal, capture the firmware request
and return value. Expected: exactly one NPS4 firmware request, for `0000:f6:00.0`,
returning zero. Any request on a different PCI device is an abort condition.

The following load still performs the original shared initialization/reset
sequence. Firmware success alone is not proof of a working mixed configuration.

## Stage C: evaluate

Before any workloads, require all of the following:

- GPU7 reports NPS4 and a compatible compute partition mode (intended: CPX).
- The seven other physical GPUs remain NPS1.
- All physical GPUs and expected logical partitions enumerate correctly.
- No initialization, memory-map, RAS, GPU fault, or repeated reset errors appear.
- Inspect compute modes on every GPU; reloads may reset them independently of NPS.

Then, in increasing scope: allocation/host-copy/kernel correctness on each
logical GPU; peer-copy data correctness between GPU7 partitions; peer-copy
correctness across the physical XGMI links; finally sustained concurrent work.
Record checksums and kernel/RAS logs, not just bandwidth. Recreate containers
with the newly verified device mapping only after host validation succeeds.

## Rollback

If the experimental driver is operational in mixed mode, keep the test parameter
enabled, queue NPS1 on GPU7, and verify the target-only log. Remove it normally
to send that request, then load the installed stock driver with
`sudo modprobe amdgpu`. Check every GPU's memory mode and restore the saved
compute modes by PCI address (GPU7 CPX, the other seven SPX at original capture).

If the driver cannot initialize, collect console logs and recover using the
installed stock driver/known-good boot configuration, with reboot or power cycle
as required. A reboot or restoring the stock module is NOT a guarantee that the
NPS setting reverted. If stock initialization succeeds but mixed settings remain,
a stock NPS1 request/reload uses the hive-wide path; the whole hive must remain
reserved until all modes are verified. If firmware rejects the configuration or
stock cannot initialize, vendor/platform recovery may be necessary.

Do not use PCI unbind, forced module removal, or an unreviewed reset shortcut as
part of this first test. No automated live-test script is provided intentionally;
each stage requires inspecting its results before advancing.
