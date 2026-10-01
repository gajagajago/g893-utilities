#!/usr/bin/env python3
"""Run a workload container exposing only GPU7's current CPX render nodes."""
import os
import pathlib
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parent
physical = pathlib.Path('/sys/bus/pci/devices/0000:f6:00.0')
assert (physical / 'current_compute_partition').read_text().strip() == 'CPX'
assert (physical / 'current_memory_partition').read_text().strip() == 'NPS4'
minors = []
for node in pathlib.Path('/sys/class/kfd/kfd/topology/nodes').glob('*'):
    if (node / 'gpu_id').read_text().strip() == '0':
        continue
    props = dict(line.split(maxsplit=1) for line in (node / 'properties').read_text().splitlines())
    if int(props['domain']) == 0 and int(props['location_id']) & ~7 == 0xf600:
        minors.append(int(props['drm_render_minor']))
assert len(set(minors)) == 8, f'Expected eight GPU7 partitions, found {minors}'
command = ['sudo', 'docker', 'run', '--rm', '-i']
if os.isatty(0) and os.isatty(1):
    command.append('-t')
command.append('--device=/dev/kfd')
command.extend(f'--device=/dev/dri/renderD{minor}' for minor in sorted(minors))
command += ['--mount', f'type=bind,src={root},dst=/test,readonly',
            'rocm/dev-ubuntu-24.04:7.2.4-complete']
command += sys.argv[1:] or ['bash']
print('GPU7 render nodes:', sorted(minors), flush=True)
raise SystemExit(subprocess.call(command))
