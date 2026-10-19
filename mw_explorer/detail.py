"""Sparse, approximately 1.85 km coverage cells around TIS transmitters."""
from array import array
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import json
import math
import pickle
import struct
import sys
import time

from . import config
from .geo import gc_km
from .build import (CELL_BYTES, CELL_FORMAT, HOUR_FMT, assign_colors, build_dir, carry_over, compute_frames,
                    current_build, prune, write_atomic)

STEP = 1 / 60  # One minute of latitude; longitude spacing follows the latitude of each row.
RADIUS_KM = 20
BATCH_CELLS = 2048


def lon_step(i):
    return STEP / math.cos(math.radians((i + .5) * STEP))


def detail_rows(stations):
    """Union of cells covering the neighborhoods of all TIS sites, encoded as row runs."""
    selected = defaultdict(set)
    pad = RADIUS_KM + 111.195 * STEP / math.sqrt(2)
    for st in stations:
        if st.get('service') != 'TIS':
            continue
        lat, lon = st['lat'], st['lon']
        for i in range(math.floor((lat - pad / 111.195) / STEP),
                       math.floor((lat + pad / 111.195) / STEP) + 1):
            center = (i + .5) * STEP
            dx = lon_step(i)
            reach = pad / (111.195 * math.cos(math.radians(center)))
            for j in range(math.floor((lon + 180 - reach) / dx),
                           math.floor((lon + 180 + reach) / dx) + 1):
                if gc_km(lat, lon, center, -180 + (j + .5) * dx) <= pad:
                    selected[i].add(j)
    rows, offset = [], 0
    for i, columns in sorted(selected.items()):
        run = []
        for j in sorted(columns):
            if run and j != run[-1] + 1:
                rows.append([i, run[0], len(run), offset])
                offset += len(run)
                run = []
            run.append(j)
        if run:
            rows.append([i, run[0], len(run), offset])
            offset += len(run)
    return rows


def detail_cells(rows):
    return [((i + .5) * STEP, -180 + (j + .5) * lon_step(i))
            for i, first, n, _ in rows for j in range(first, first + n)]


def detail_edges(rows):
    """Horizontal and vertical neighbors, including rows with different longitude spacing."""
    lookup = defaultdict(dict)
    for i, first, n, offset in rows:
        lookup[i].update((j, offset + j - first) for j in range(first, first + n))
    edges = []
    for i, columns in lookup.items():
        previous = lookup.get(i - 1, {})
        for j, index in columns.items():
            if j - 1 in columns:
                edges.append((columns[j - 1], index))
            left, right = j * lon_step(i), (j + 1) * lon_step(i)
            for other in range(math.floor(left / lon_step(i - 1) + 1e-9),
                               math.ceil(right / lon_step(i - 1) - 1e-9)):
                if other in previous:
                    edges.append((previous[other], index))
    return edges


def compute_detail_batch(task):
    freq, stations, ctx, start, cells = task
    ctx = dict(ctx, freq=freq)
    out = build_dir(ctx['build']) / 'ch' / str(freq)
    data = b''.join(buf for _, buf, _, _ in compute_frames(stations, ctx, cells))
    write_atomic(out / f'{start}.detailpart', data)
    return freq, start, len(cells)


def assemble_detail(task, rows):
    freq, stations, ctx = task
    out = build_dir(ctx['build']) / 'ch' / str(freq)
    ch = json.loads((out / 'stations.json').read_text())
    count = sum(r[2] for r in rows)
    bests = [array('i') for _ in ctx['hours']]
    wins = defaultdict(float, ((k, st.get('coarse_wins', st['wins']))
                               for k, st in enumerate(ch['stations']) if st.get('coarse_wins', st['wins']) > 0))
    # Keep legend counts in continental-cell units so detail doesn't outweigh broad coverage.
    weights = [STEP * lon_step(i) / ctx['grid']['step'] ** 2
               for i, _, n, _ in rows for _ in range(n)]
    temps = [out / (hour + '.detail.bin.gz.tmp') for hour in ctx['hours']]
    try:
        with ExitStack() as stack:
            streams = [stack.enter_context(gzip.open(path, 'wb', compresslevel=6)) for path in temps]
            for start in range(0, count, BATCH_CELLS):
                part = out / f'{start}.detailpart'
                n = min(BATCH_CELLS, count - start)
                with part.open('rb') as source:
                    for h, stream in enumerate(streams):
                        buf = source.read(n * CELL_BYTES)
                        if len(buf) != n * CELL_BYTES:
                            raise ValueError(f'Incomplete detail batch: {part}')
                        stream.write(buf)
                        best = [r[0] if r[0] != 65535 else -1 for r in struct.iter_unpack(CELL_FORMAT, buf)]
                        bests[h].extend(best)
                        for c, k in enumerate(best):
                            if k >= 0:
                                wins[k] += weights[start + c]
                    if source.read(1):
                        raise ValueError(f'Unexpected detail batch size: {part}')
                part.unlink()
        for path, hour in zip(temps, ctx['hours']):
            path.replace(out / (hour + '.detail.bin.gz'))
    finally:
        for path in temps:
            path.unlink(missing_ok=True)
    coarse = []
    for hour in ctx['hours']:
        data = gzip.decompress((out / (hour + '.bin.gz')).read_bytes())
        coarse.append(array('i', (r[0] if r[0] != 65535 else -1 for r in struct.iter_unpack(CELL_FORMAT, data))))
    colors = assign_colors(wins, coarse, ctx['grid']['nlat'], ctx['grid']['nlon'], bests, detail_edges(rows))
    for k, st in enumerate(ch['stations']):
        st['coarse_wins'] = st.get('coarse_wins', st['wins'])
        st['wins'] = round(wins.get(k, 0), 6)
        st['color'] = colors.get(k)
    ch['detail'] = {'version': 1, 'step': STEP, 'radius_km': RADIUS_KM, 'rows': rows, 'cells': count}
    write_atomic(out / 'stations.json', json.dumps(ch, separators=(',', ':')))
    return freq, count


def batch_tasks(task, rows):
    cells = detail_cells(rows)
    return [(*task, start, cells[start:start + BATCH_CELLS])
            for start in range(0, len(cells), BATCH_CELLS)]


def refine_channel(task):
    if task[2]['grid']['step'] <= STEP:
        return task[0], 0
    rows = detail_rows(task[1])
    if not rows:
        return task[0], 0
    for batch in batch_tasks(task, rows):
        compute_detail_batch(batch)
    return assemble_detail(task, rows)


def refine_channels(tasks, workers=None):
    prepared, jobs, remaining = {}, [], {}
    for task in tasks:
        if task[2]['grid']['step'] <= STEP:
            continue
        rows = detail_rows(task[1])
        if not rows:
            continue
        prepared[task[0]] = (task, rows)
        batches = batch_tasks(task, rows)
        remaining[task[0]] = len(batches)
        jobs.extend(batches)
    if not jobs:
        return
    print(f'Refining {len(prepared)} channels in {len(jobs)} batches...', file=sys.stderr)
    complete = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for n, (freq, _, _) in enumerate(pool.map(compute_detail_batch, jobs), 1):
            remaining[freq] -= 1
            if remaining[freq] == 0:
                _, count = assemble_detail(*prepared[freq])
                complete += 1
                print(f'  [{complete}/{len(prepared)}] {freq} kHz: {count} local cells', file=sys.stderr)
            elif n % 10 == 0:
                print(f'  {n}/{len(jobs)} batches computed', file=sys.stderr)


def refine(workers=None):
    """Publish a new build with local detail while reusing the current continental frames."""
    previous = current_build()
    if not previous:
        sys.exit('No build yet. Run: python -m mw_explorer build')
    index = json.loads((config.out_dir() / 'index.json').read_text())
    db = pickle.loads((build_dir(previous) / 'db.pickle').read_bytes())
    now = datetime.now(timezone.utc)
    build = now.strftime(HOUR_FMT) + '-' + str(time.time_ns())
    prune({previous})
    carry_over(previous, build, set())
    tasks = []
    for freq, stations in db['stations'].items():
        path = build_dir(build) / 'ch' / str(freq) / 'stations.json'
        ch = json.loads(path.read_text())
        ch['build'] = build
        for st in ch['stations']:
            st['wins'] = st.get('coarse_wins', st['wins'])
        write_atomic(path, json.dumps(ch, separators=(',', ':')))
        if any(s.get('service') == 'TIS' for s in stations):
            tasks.append((freq, stations, {'build': build, 'grid': index['grid'],
                                           'ssn': ch['ssn'], 'hours': ch['hours']}))
    tasks.sort(key=lambda t: -len(t[1]))
    refine_channels(tasks, workers)
    db['build'] = build
    write_atomic(build_dir(build) / 'db.pickle', pickle.dumps(db))
    index.update(build=build, generated=now.isoformat())
    write_atomic(config.out_dir() / 'index.json', json.dumps(index, separators=(',', ':')))
    prune({previous, build})
    print(f'Published {build}', file=sys.stderr)
