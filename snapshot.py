#!/usr/bin/env python3
import json
import pathlib
import re
import subprocess
import sys
import time

root = pathlib.Path(__file__).resolve().parent
label = sys.argv[1]
assert re.fullmatch(r'[a-z0-9-]+', label)
data = {'time_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'gpus': []}
for p in sorted(pathlib.Path('/sys/bus/pci/devices').glob('*')):
    if not (p / 'current_memory_partition').exists():
        continue
    item = {'bdf': p.name}
    for key, attr in [('compute', 'current_compute_partition'), ('memory', 'current_memory_partition')]:
        item[key] = (p / attr).read_text().strip()
    data['gpus'].append(item)
    print(item)
data['kfd_nodes'] = []
for p in sorted(pathlib.Path('/sys/class/kfd/kfd/topology/nodes').glob('*')):
    gpu_id = (p / 'gpu_id').read_text().strip()
    if gpu_id == '0':
        continue
    props = dict(line.split(maxsplit=1) for line in (p / 'properties').read_text().splitlines())
    data['kfd_nodes'].append({'node': p.name, 'gpu_id': gpu_id, **props})
print('KFD GPU nodes:', len(data['kfd_nodes']))
(root / f'{label}-state.json').write_text(json.dumps(data, indent=2) + '\n')
logs = subprocess.check_output(['sudo', '-n', 'dmesg', '--color=never'], text=True)
start = float((root / 'stage-a-start-uptime.txt').read_text().split()[0])
lines = []
for line in logs.splitlines():
    stamp = re.match(r'\[\s*([\d.]+)\]', line)
    if stamp and float(stamp[1]) >= start:
        lines.append(line)
(root / f'{label}-kernel.log').write_text('\n'.join(lines) + '\n')
interesting = [l for l in lines if re.search(r'NPS-TEST|memory partition change|\*ERROR\*|GPU fault|failed|BUG:|Oops:', l, re.I)]
print('\n'.join(interesting[-40:]))
