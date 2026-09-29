"""Exact zpool list -v measurements, separate from status and dataset accounting."""
import math
import re

PROPERTIES = ('name,size,allocated,free,checkpoint,expandsize,fragmentation,'
              'capacity,dedupratio,health,altroot')
COLUMNS = ('name', 'size', 'allocated', 'free', 'checkpoint', 'expandsize',
           'fragmentation', 'capacity', 'dedupratio', 'health', 'altroot')
ROLES = {'logs': 'log', 'special': 'special', 'dedup': 'dedup',
         'cache': 'cache', 'spare': 'spare', 'spares': 'spare'}


def parse_capacity(text, pool_name):
    rows = []
    role = 'data'
    for line in text.splitlines():
        if not line.strip():
            continue
        # OpenZFS 2.2 emits allocation-class headings using a fixed-width
        # printf even under -H. Recognize only heading + dash placeholders.
        marker = re.fullmatch(r"\s*(logs|special|dedup|cache|spares?)\s+(?:-\s*)+", line)
        if rows and marker:
            role = ROLES[marker[1]]
            continue
        # Script mode prefixes vdev rows with a tab, but does not retain depth.
        parts = line.lstrip('\t').split('\t')
        name = parts[0].strip()
        if name in ROLES and rows:
            role = ROLES[name]
            continue
        if len(parts) < 7 or len(parts) > len(COLUMNS):
            raise ValueError('Malformed verbose pool capacity row')
        if not rows and (name != pool_name or len(parts) != len(COLUMNS)):
            raise ValueError('Missing pool summary in verbose capacity output')
        parts += ['-'] * (len(COLUMNS) - len(parts))
        row = dict(zip(COLUMNS, parts))
        row.update(name=name, role=role)
        for key in COLUMNS[1:9]:
            value = row[key].strip()
            if value == '-':
                row[key] = None
                continue
            number = int(value) if key in COLUMNS[1:6] else float(value.rstrip('%x'))
            if not math.isfinite(number) or number < 0:
                raise ValueError('Invalid verbose capacity value')
            row[key] = number
        for key in ('health', 'altroot'):
            row[key] = None if row[key] == '-' else row[key]
        rows.append(row)
    if not rows:
        raise ValueError('Empty verbose capacity output')
    return rows


def capacity_rows(pool, topology):
    """Recover hierarchy from status; retain every list row even if unmatched."""
    hierarchy = {}

    def walk(nodes, role, depth):
        for node in nodes:
            hierarchy[role, node['name']] = (depth, bool(node['children']))
            walk(node['children'], role, depth + 1)

    for section in topology['sections']:
        if section['nodes']:
            walk(section['nodes'], section['nodes'][0].get('role', 'data'), 1)
    result = []
    for entry in (pool.get('capacity_detail') or {}).get('rows', []):
        row = dict(entry)
        root = row['name'] == pool['name']
        depth, group = hierarchy.get((row['role'], row['name']), (0 if root else 1, False))
        row.update(depth=depth, group=group, root=root)
        result.append(row)
    return result


def attach_capacity(topology, rows):
    """Attach only the same element's measurements, never pool-wide substitutes."""
    measurements = {(r['role'], r['name']): r for r in rows}

    def walk(nodes, role):
        for node in nodes:
            node['capacity'] = measurements.get((role, node['name']))
            walk(node['children'], role)

    for section in topology['sections']:
        if section['nodes']:
            walk(section['nodes'], section['nodes'][0].get('role', 'data'))
