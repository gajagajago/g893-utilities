#!/usr/bin/env bash
# Run during a maintenance window with all GPUs idle and console recovery ready.
# Builds a fresh v2 module and changes only GPU7 to CPX/NPS4. No workload tests.
set -Eeuo pipefail
shopt -s nullglob

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'HELP'
Usage: sudo bash setup-gpu7.sh

Applies v2-dkms.patch to a fresh source copy, builds and temporarily loads
AMDGPU, then sets physical GPU7 (0000:f6:00.0) to CPX/NPS4.
All eight GPUs must be idle. Have independent SSH and console/power recovery
available. Driver reload affects the entire GPU hive. No workload tests run.
An already configured v2/CPX/NPS4 system is left running without a reload.

Optional environment variables (pass using sudo env NAME=value ...):
  GPU7_WORK_ROOT  Persistent build/log directory (default: <script-dir>/runs)
  GPU7_JOBS       Build parallelism (default: 8)

Validated kernel: 6.8.0-138-generic
Source: /usr/src/amdgpu-6.16.13-2341068.24.04
The installed driver and boot configuration are not replaced.
HELP
    exit 0
fi
[[ $# == 0 ]] || { echo 'Unknown arguments; use --help.' >&2; exit 2; }
[[ $EUID == 0 ]] || { echo 'Run with sudo bash setup-gpu7.sh.' >&2; exit 1; }

base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target=0000:f6:00.0
sysfs=/sys/bus/pci/devices/$target
source_dir=/usr/src/amdgpu-6.16.13-2341068.24.04
kernel=$(uname -r)
jobs=${GPU7_JOBS:-8}
run_dir=
stage=preflight
queued=0

die() { echo "ERROR: $*" >&2; exit 1; }
on_exit() {
    local rc=$?
    if (( rc != 0 )); then
        echo "Stopped during: $stage. Artifacts: ${run_dir:-not created}" >&2
        if (( queued )) && [[ -r $sysfs/current_memory_partition ]] &&
            [[ $(< "$sysfs/current_memory_partition") == NPS1 ]] &&
            [[ -r /sys/module/amdgpu/parameters/nps_test_bdf ]] &&
            [[ $(< /sys/module/amdgpu/parameters/nps_test_bdf) == "$target" ]]; then
            printf 'NPS1\n' > "$sysfs/current_memory_partition" &&
                echo 'Cancelled the still-pending GPU7 request.' >&2
        fi
        echo 'Do not force unload. See RESULTS.md for the v2 recovery sequence.' >&2
    fi
}
trap on_exit EXIT

for tool in flock fuser make patch cp modinfo modprobe insmod dmesg amd-smi grep tee; do
    command -v "$tool" >/dev/null || die "Missing command: $tool"
done
exec 9>/run/lock/gpu7-nps4-setup.lock
flock -n 9 || die 'Another GPU7 setup is running.'
[[ $kernel == 6.8.0-138-generic ]] || die "Unvalidated kernel: $kernel; rebuild/ABI review required."
[[ $jobs =~ ^[1-9][0-9]*$ ]] || die 'GPU7_JOBS must be a positive integer.'

configured_driver() {
    [[ -r /sys/module/amdgpu/parameters/nps_test_bdf &&
       -r /sys/module/amdgpu/parameters/nps_test_hive_reset ]] &&
    [[ $(< /sys/module/amdgpu/parameters/nps_test_bdf) == "$target" &&
       $(< /sys/module/amdgpu/parameters/nps_test_hive_reset) == Y ]]
}
check_modes() {
    local p bdf compute memory count=0
    local paths=(/sys/bus/pci/devices/*/current_memory_partition)
    [[ ${#paths[@]} == 8 ]] || die 'Expected eight physical GPUs with partition attributes.'
    for p in "${paths[@]}"; do
        bdf=${p%/*}; bdf=${bdf##*/}
        memory=$(< "$p")
        compute=$(< "${p%/*}/current_compute_partition")
        printf '%s: %s/%s\n' "$bdf" "$compute" "$memory"
        if [[ $bdf == "$target" ]]; then
            count=$((count + 1))
            [[ $memory == NPS1 || $memory == NPS4 ]] || die 'Unexpected GPU7 memory mode.'
            [[ $compute == SPX || $compute == CPX ]] || die 'Unexpected GPU7 compute mode.'
        else
            [[ $memory == NPS1 && $compute == SPX ]] || die "Unexpected modes on $bdf; refusing changes."
        fi
    done
    [[ $count == 1 ]] || die 'GPU7 is missing.'
}
check_idle() {
    local devices=(/dev/kfd /dev/dri/card* /dev/dri/renderD*) rc
    [[ -e /dev/kfd ]] || die 'KFD is unavailable.'
    if fuser -v "${devices[@]}"; then
        die 'GPU device handles are open. Stop their workloads/containers/monitors first.'
    else
        rc=$?
        [[ $rc == 1 ]] || die "Device-use check failed ($rc)."
    fi
}
check_modes
if configured_driver && [[ $(< "$sysfs/current_compute_partition") == CPX &&
                           $(< "$sysfs/current_memory_partition") == NPS4 ]]; then
    echo 'GPU7 is already CPX/NPS4 under v2; no reload needed.'
    exit 0
fi
check_idle
[[ -f $base/v2-dkms.patch && -d $source_dir ]] || die 'Missing v2 patch or pristine DKMS source.'
[[ -d /lib/modules/$kernel/build ]] || die 'Missing kernel build headers.'
if [[ -r /sys/kernel/security/lockdown ]]; then
    grep -q '\[none\]' /sys/kernel/security/lockdown || die 'Kernel lockdown blocks this unsigned module.'
fi
# insmod does not read modprobe options. Require review rather than dropping them.
deps=$(modprobe --show-depends amdgpu)
if printf '%s\n' "$deps" | grep -E '^insmod .*amdgpu\.ko[^ ]* +[^ ]' >/dev/null; then
    die 'AMDGPU has configured module options; incorporate them into load_v2 before running.'
fi

mkdir -p "${GPU7_WORK_ROOT:-$base/runs}"
run_dir=$(mktemp -d "${GPU7_WORK_ROOT:-$base/runs}/gpu7-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
exec > >(tee -a "$run_dir/setup.log") 2>&1
echo "Build and recovery directory: $run_dir"
dmesg --color=never > "$run_dir/before-kernel.log"
check_modes > "$run_dir/before-modes.txt"
cp -a "$(modinfo -n amdgpu)" "$run_dir/"
printf '%s\n' "$deps" > "$run_dir/stock-dependencies.txt"

stage=build
cp -a "$source_dir" "$run_dir/source"
patch --dry-run -p1 -d "$run_dir/source" -i "$base/v2-dkms.patch"
patch -p1 -d "$run_dir/source" -i "$base/v2-dkms.patch"
echo 'Building AMDGPU; see build.log for progress.'
make -C "$run_dir/source" KERNELVER="$kernel" num_cpu_cores="$jobs" > "$run_dir/build.log" 2>&1
module=$run_dir/source/amd/amdgpu/amdgpu.ko
[[ $(modinfo -F vermagic "$module") == "$(modinfo -F vermagic amdgpu)" ]] || die 'Module vermagic differs from stock.'
modinfo -F parm "$module" | grep '^nps_test_bdf:' >/dev/null || die 'Missing target parameter.'
modinfo -F parm "$module" | grep '^nps_test_hive_reset:' >/dev/null || die 'Missing hive reset parameter.'
load_v2() {
    modprobe -a drm_display_helper amdkcl amdttm cec amd-sched video \
        drm_suballoc_helper amddrm_exec i2c-algo-bit amdxcp amddrm_buddy amddrm_ttm_helper
    insmod "$module" nps_test_bdf="$target" nps_test_hive_reset=1
    configured_driver || die 'Loaded driver parameters do not match.'
}

stage=initial-reload
check_idle
modprobe -r amdgpu
load_v2
check_modes

if [[ $(< "$sysfs/current_memory_partition") != NPS4 ]]; then
    stage=nps4-request
    check_idle
    # Direct sysfs write uses the same driver handler as AMD SMI, without its prompt.
    queued=1
    printf 'NPS4\n' > "$sysfs/current_memory_partition"
    dmesg --color=never > "$run_dir/queued-kernel.log"
    grep -F "$target: amdgpu: NPS-TEST: queued per-device mode=4" "$run_dir/queued-kernel.log" || die 'Missing target queue log.'
    stage=nps4-reload
    modprobe -r amdgpu
    queued=0
    dmesg --color=never > "$run_dir/request-kernel.log"
    # Reload v2 even if firmware rejected the request, so the host can initialize.
    load_v2
    check_modes
    [[ $(< "$sysfs/current_memory_partition") == NPS4 ]] || die 'GPU7 did not enter NPS4.'
    grep -F "$target: amdgpu: NPS-TEST: firmware request NPS4 returned 0" "$run_dir/request-kernel.log" || die 'Missing firmware success log.'
fi

stage=cpx
check_idle
amd-smi set --gpu "$target" --compute-partition CPX
check_modes
[[ $(< "$sysfs/current_compute_partition") == CPX &&
   $(< "$sysfs/current_memory_partition") == NPS4 ]] || die 'GPU7 did not enter CPX/NPS4.'
check_modes > "$run_dir/final-modes.txt"
dmesg --color=never > "$run_dir/final-kernel.log"
stage=complete
echo 'Done: GPU7 CPX/NPS4; other seven GPUs SPX/NPS1. No workload tests run.'
echo "Module: $module"
echo 'This driver is loaded temporarily; automatic boot loading is not configured.'
