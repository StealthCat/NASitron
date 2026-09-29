from datetime import datetime
import pytest

from app.pool_capacity import parse_capacity, capacity_rows
from app.parser import build_snapshot
from app.service import _merge_previous_subsystems

TEXT = '''tank\t1000000\t400000\t600000\t-\t0\t12\t40\t1.00x\tONLINE\t/mnt/alternate root
\traidz2-0\t1000000\t400000\t600000\t0\t0\t12\t40\t-\tONLINE\t-
\t/dev/disk with space\t-\t-\t-\t-\t1000\t-\t-\t-\tONLINE\t-
logs\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-
\t/dev/sdz\t2000\t0\t2000\t0\t0\t0\t0\t-\tONLINE\t-
spare\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-
\t/dev/sdy\t-\t-\t-\t-\t0\t-\t-\t-\tAVAIL\t-
'''


def test_capacity_keeps_every_column_missing_values_and_allocation_classes():
    rows = parse_capacity(TEXT, 'tank')
    assert len(rows) == 5
    assert rows[0]['altroot'] == '/mnt/alternate root'
    assert rows[0]['checkpoint'] is None and rows[1]['checkpoint'] == 0
    assert rows[0]['capacity'] == 40 and rows[0]['dedupratio'] == 1
    assert rows[2]['name'] == '/dev/disk with space' and rows[2]['size'] is None
    assert rows[2]['expandsize'] == 1000
    assert rows[3]['role'] == 'log' and rows[4]['role'] == 'spare'
    assert parse_capacity(TEXT.replace('tank', 'cache', 1), 'cache')[0]['name'] == 'cache'


@pytest.mark.parametrize('text', ['', TEXT.replace('1000000', 'bad', 1),
                                  TEXT.replace('tank', 'other', 1), 'tank\t1\t2',
                                  TEXT.replace('12\t40', 'nan\t40', 1)])
def test_capacity_rejects_malformed_or_wrong_pool_results(text):
    with pytest.raises(ValueError):
        parse_capacity(text, 'tank')


def test_hierarchy_comes_from_status_without_inventing_child_capacity():
    topology = {'sections': [{'nodes': [{'name': 'raidz2-0', 'role': 'data', 'children': [
        {'name': '/dev/disk with space', 'children': []}]}]}]}
    rows = capacity_rows({'name': 'tank', 'capacity_detail': {'rows': parse_capacity(TEXT, 'tank')}}, topology)
    assert [r['depth'] for r in rows[:3]] == [0, 1, 2]
    assert rows[1]['group'] and rows[0]['root']
    assert rows[2]['size'] is None


def test_failed_capacity_collection_retains_previous_rows_marked_stale():
    raw = {'zpool_list': {'exit': 0, 'stdout': 'tank\t1000000\t400000\t600000\t12\t40\t1.00x\tONLINE'},
           'zpool_list_verbose': {'tank': {'exit': 0, 'stdout': TEXT}}}
    previous = build_snapshot(raw, datetime(2026, 9, 27, 12))
    assert previous['pools'][0]['capacity_detail']['fresh'] is True
    raw['zpool_list_verbose']['tank']['stdout_truncated'] = True
    current = build_snapshot(raw, datetime(2026, 9, 27, 13))
    _merge_previous_subsystems(current, previous)
    detail = current['pools'][0]['capacity_detail']
    assert detail['fresh'] is False and len(detail['rows']) == 5
    assert detail['captured_at'] == '2026-09-27T12:00:00Z'
    assert 'pool.capacity:tank' in current['collection']['stale_subsystems']


@pytest.mark.parametrize('heading,role', [('logs','log'), ('cache','cache'),
                                        ('special','special'), ('dedup','dedup'), ('spare','spare')])
def test_openzfs_22_mixed_whitespace_class_headers_and_ten_column_vdevs(heading, role):
    root = TEXT.splitlines()[0]
    vdev = '\traidz2-0\t1000000\t400000\t600000\t-\t-\t12\t40\t-\tONLINE'
    marker = heading.ljust(45) + '      -      -      -        -         -      -      -      -         -'
    rows = parse_capacity('\n'.join([root, vdev, marker,
        '\t/dev/sdz\t2000\t500\t1500\t-\t-\t5\t25\t-\tONLINE']), 'tank')
    assert len(rows) == 3
    assert rows[1]['size'] == 1000000 and rows[1]['capacity'] == 40
    assert rows[2]['role'] == role and rows[2]['allocated'] == 500
    assert rows[2]['health'] == 'ONLINE' and rows[2]['altroot'] is None


def test_hierarchy_capacity_is_per_element_not_inherited_from_pool():
    from app.pool_capacity import attach_capacity
    topology = {'sections': [{'nodes': [{'name': 'raidz2-0', 'role': 'data', 'children': [
        {'name': '/dev/disk with space', 'children': []}, {'name': 'missing', 'children': []}]}]}]}
    attach_capacity(topology, parse_capacity(TEXT, 'tank'))
    group = topology['sections'][0]['nodes'][0]
    assert group['capacity']['allocated'] == 400000
    assert group['children'][0]['capacity']['size'] is None
    assert group['children'][1]['capacity'] is None
