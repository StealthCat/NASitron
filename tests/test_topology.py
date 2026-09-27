import json

from app.parser import parse_pool_status, parse_pool_status_json
from app.topology import pool_topology


def test_three_raidz_groups_keep_six_members_and_root_counters():
    lines = ['config:', '\tNAME STATE READ WRITE CKSUM', '\tprimary-z2 ONLINE 0 0 0']
    for group in range(3):
        lines.append(f'\t  raidz2-{group} ONLINE 0 0 {group}')
        lines.extend(f'\t    /dev/disk/by-id/disk-{group}-{i} ONLINE 0 0 0' for i in range(6))
    status = parse_pool_status('\n'.join(lines))
    topology = pool_topology({'name': 'primary-z2', 'status': status}, [])
    groups = topology['sections'][0]['nodes']
    assert topology['root']['name'] == 'primary-z2'
    assert [n['name'] for n in groups] == ['raidz2-0', 'raidz2-1', 'raidz2-2']
    assert [n['disk_count'] for n in groups] == [6, 6, 6]
    assert groups[2]['checksum_errors'] == 2
    assert groups[0]['children'][0]['name'] == '/dev/disk/by-id/disk-0-0'


def test_nested_replacement_and_allocation_classes_with_counterless_spare():
    status = parse_pool_status('''config:
    tank DEGRADED 0 0 0
      mirror-0 DEGRADED 0 0 0
        replacing-0 DEGRADED 0 0 0
          /dev/sda1 OFFLINE 1 2 3
          /dev/sdb ONLINE 0 0 0
        /dev/sdc ONLINE 0 0 0
    logs
      mirror-1 ONLINE 0 0 0
        /dev/sdd ONLINE 0 0 0
        /dev/sde ONLINE 0 0 0
    cache
      /dev/sdf ONLINE 0 0 0
    special
      /dev/sdg ONLINE 0 0 0
    spares
      /dev/sdh AVAIL
errors: No known data errors
''')
    disks = [{'path': '/dev/sda', 'serial': 'A'}, {'path': '/dev/sdab', 'serial': 'B'}]
    topology = pool_topology({'name': 'tank', 'status': status}, disks)
    assert [s['disk_count'] for s in topology['sections']] == [3, 2, 1, 1, 1]
    replacement = topology['sections'][0]['nodes'][0]['children'][0]
    assert replacement['kind'] == 'REPLACING'
    assert replacement['children'][0]['disk']['serial'] == 'A'
    assert topology['sections'][-1]['nodes'][0]['read_errors'] is None
    assert status['vdevs'][-2]['leaf'] is True


def test_json_and_text_same_hierarchy_and_no_guessed_disk_identity():
    tree = {'tank': {'state': 'ONLINE', 'vdevs': {'mirror-0': {'state': 'ONLINE', 'vdevs': {
        'a': {'path': '/dev/sda', 'state': 'ONLINE'},
        'b': {'path': '/dev/disk/by-id/serial-A', 'state': 'ONLINE'},
    }}}}}
    status = parse_pool_status_json(json.dumps({'pools': {'tank': {'vdevs': tree}}}), 'tank')
    topology = pool_topology({'name': 'tank', 'status': status}, [{'path': '/dev/sda', 'serial': 'A'}])
    group = topology['sections'][0]['nodes'][0]
    assert group['disk_count'] == 2
    assert group['children'][0]['disk']['serial'] == 'A'
    assert group['children'][1]['disk'] is None
    assert pool_topology({'name': 'empty'}, [])['sections'] == []


def test_verbose_status_preserves_advice_scan_and_error_paths_without_fake_vdevs():
    from app.parser import parse_status_sections
    text = '''  pool: tank
 state: DEGRADED
status: A device is unavailable.
        The pool remains accessible.
action: Check connections.
        Replace the faulted device.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-9P
  scan: resilver in progress
        100G scanned, 50.00% done, 00:10:00 to go
config:
        NAME STATE READ WRITE CKSUM
        tank DEGRADED 0 0 3
          mirror-0 DEGRADED 0 0 3
            /dev/sda ONLINE 0 0 0 (resilvering)
            /dev/sdb FAULTED 0 0 3 too many errors
errors: Permanent errors have been detected in the following files:
        /tank/photos/family photo.jpg
        tank/data:<0xdeadbeef>
        /tank/path ONLINE 0 0 0
'''
    parsed = parse_pool_status(text)
    sections = {s['key']: s['text'] for s in parsed['sections']}
    assert len(parsed['vdevs']) == 4
    assert parsed['vdevs'][-1]['detail'] == 'too many errors'
    assert 'Replace the faulted device.' in sections['action']
    assert sections['see'] == 'https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-9P'
    assert '50.00% done' in sections['scan']
    assert 'family photo.jpg' in sections['errors']
    assert 'tank/data:<0xdeadbeef>' in sections['errors']
    assert parse_status_sections(text) == parsed['sections']
    json_status = parse_pool_status_json(json.dumps({'pools': {'tank': {'vdevs': {
        'tank': {'state': 'DEGRADED', 'vdevs': {'a': {'path': '/dev/sda', 'state': 'ONLINE'}}}
    }}}}), 'tank', text)
    assert json_status['sections'] == parsed['sections']
    assert json_status['vdevs'][-1]['detail'] == '(resilvering)'
