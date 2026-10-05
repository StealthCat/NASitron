"""Exercise maintenance commands with fake devices only; never call host ZFS."""
import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from remote import nasitron_root_helper as h

OLD = 'scsi-SATA_WDC_WD60EFAX-68S_WD-WX31D49KSU3H'
NEW = 'scsi-SATA_ST6000VN001-2BB1_ZR13TAY4Y'


@pytest.fixture
def host(monkeypatch, tmp_path):
    calls = []
    members = [dict(guid='200', id='mirror-0', state='ONLINE', group=True, parent_guid='', role='data'),
               dict(guid='300', id=OLD, state='ONLINE', group=False, parent_guid='200', role='data')]
    monkeypatch.setattr(h, 'LOCK_PATH', str(tmp_path / 'lock'))
    monkeypatch.setattr(h, '_aliases', lambda: {})
    monkeypatch.setattr(h, '_pool_names', lambda: {'primary-z2'})
    monkeypatch.setattr(h, '_pool_members', lambda *args: members)
    monkeypatch.setattr(h, '_scan_in_progress', lambda p: '')
    monkeypatch.setattr(h, '_validate_blank_disk', lambda p: ('/dev/sdz', 6000))
    monkeypatch.setattr(h, '_lsblk_disk', lambda p: {'serial': 'ZR13TAY4Y', 'wwn': 'abc'})
    monkeypatch.setattr(h, '_required_vdev_size', lambda *args: 5000)
    original_stat = h.os.stat
    monkeypatch.setattr(h.os, 'stat', lambda p, **kw: SimpleNamespace(st_rdev=123) if str(p) == '/dev/sdz' else original_stat(p, **kw))
    monkeypatch.setattr(h, '_importable_pools', lambda: [{'name': 'backup', 'guid': '999'}])
    monkeypatch.setattr(h, '_run_action', lambda args: calls.append(args) or CompletedProcess(args, 0, 'OK', ''))
    return calls, members


def request(action, **kw):
    return h._action_request(json.dumps(dict(action=action, pool='primary-z2', **kw)))


@pytest.mark.parametrize('action,extra,expected', [
    ('replace', {'target': '300', 'disks': [NEW]}, ['replace', 'primary-z2', OLD, NEW]),
    ('attach', {'target': '200', 'disks': [NEW]}, ['attach', 'primary-z2', 'mirror-0', NEW]),
    ('detach', {'target': '300'}, ['detach', 'primary-z2', OLD]),
    ('remove', {'target': '200'}, ['remove', 'primary-z2', 'mirror-0']),
    ('offline', {'target': '300'}, ['offline', 'primary-z2', OLD]),
    ('offline-temporary', {'target': '300'}, ['offline', '-t', 'primary-z2', OLD]),
    ('online', {'target': '300'}, ['online', 'primary-z2', OLD]),
    ('expand', {'target': '300'}, ['online', '-e', 'primary-z2', OLD]),
    ('clear', {}, ['clear', 'primary-z2']),
    ('clear', {'target': '300'}, ['clear', 'primary-z2', OLD]),
    ('scrub-pause', {}, ['scrub', '-p', 'primary-z2']),
    ('scrub-stop', {}, ['scrub', '-s', 'primary-z2']),
    ('trim-pause', {'target': '300'}, ['trim', '-s', 'primary-z2', OLD]),
    ('trim-stop', {}, ['trim', '-c', 'primary-z2']),
    ('initialize-pause', {}, ['initialize', '-s', 'primary-z2']),
    ('initialize-stop', {}, ['initialize', '-c', 'primary-z2']),
    ('remove-cancel', {}, ['remove', '-s', 'primary-z2']),
    ('checkpoint-discard', {}, ['checkpoint', '-d', 'primary-z2']),
    ('set', {'property': 'autotrim', 'value': 'on'}, ['set', 'autotrim=on', 'primary-z2']),
    ('split', {'disks': [OLD], 'new_pool': 'backup'}, ['split', 'primary-z2', 'backup', OLD]),
    ('add', {'disks': [NEW], 'role': 'cache', 'layout': 'stripe'}, ['add', '-o', 'ashift=12', 'primary-z2', 'cache', NEW]),
])
def test_argv_and_preview_never_mutates(host, action, extra, expected):
    calls, _ = host
    plan = h.action_plan(request(action, **extra))
    assert plan['args'] == expected
    assert '-f' not in plan['args']
    assert '/dev/' not in plan['command']
    assert calls == ([expected[:1] + ['-n'] + expected[1:]] if action in {'add', 'remove', 'split'} else [])


@pytest.mark.parametrize('action', ['scrub', 'trim', 'initialize', 'resilver', 'upgrade', 'reguid', 'reopen', 'sync', 'checkpoint', 'export', 'destroy'])
def test_pool_only_actions(host, action):
    assert h.action_plan(request(action))['args'] == [action, 'primary-z2']
    assert host[0] == []


def test_create_and_import(host):
    create = h.action_plan(dict(action='create', pool='newpool', disks=[NEW], layout='stripe'))
    assert create['args'] == ['create', '-o', 'ashift=12', 'newpool', NEW]
    assert h.action_plan(dict(action='import', pool='999'))['args'] == ['import', '-d', '/dev/disk/by-id', '999']
    with pytest.raises(h.HelperError, match='no longer available'):
        h.action_plan(dict(action='import', pool='123'))


@pytest.mark.parametrize('value', ['/dev/sda', '/dev/disk/by-id/'+NEW, '../sda', '-f', 'foo;reboot', '123', 'bad\nname'])
def test_rejects_paths_flags_and_shell(host, value):
    with pytest.raises(h.HelperError):
        h.action_plan(request('replace', target='300', disks=[value]))
    assert not host[0]


@pytest.mark.parametrize('payload', [dict(action='shell', pool='primary-z2'), dict(action='clear', pool='-a'),
    dict(action='clear', pool='primary-z2', flags='-f'), dict(action='clear', pool='primary-z2', disks='bad')])
def test_strict_request(payload):
    with pytest.raises(h.HelperError):
        h._action_request(json.dumps(payload))


def test_size_alias_topology_and_native_rejections(host, monkeypatch):
    with pytest.raises(h.HelperError, match='same disk'):
        h.action_plan(request('add', disks=[NEW, 'wwn-alias'], layout='mirror'))
    monkeypatch.setattr(h, '_required_vdev_size', lambda *a: 9000)
    with pytest.raises(h.HelperError, match='smaller'):
        h.action_plan(request('replace', target='300', disks=[NEW]))
    monkeypatch.setattr(h, '_scan_in_progress', lambda p: 'scrub in progress')
    with pytest.raises(h.HelperError, match='active scrub'):
        h.action_plan(request('offline', target='300'))
    monkeypatch.setattr(h, '_scan_in_progress', lambda p: '')
    monkeypatch.setattr(h, '_run_action', lambda args: CompletedProcess(args, 1, '', 'invalid topology'))
    with pytest.raises(h.HelperError, match='invalid topology'):
        h.action_plan(request('remove', target='200'))


def test_preview_and_fingerprint_recheck(host, capsys, monkeypatch):
    calls, members = host
    raw = json.dumps(request('replace', target='300', disks=[NEW]))
    assert h.cmd_actions(['preview', raw]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not calls
    members[1]['state'] = 'OFFLINE'
    with pytest.raises(h.HelperError, match='changed'):
        h.cmd_actions(['execute', raw, plan['fingerprint']])
    assert not calls
    members[1]['state'] = 'ONLINE'
    monkeypatch.setattr(h, '_lsblk_disk', lambda p: {'serial': 'CHANGED', 'wwn': 'abc'})
    with pytest.raises(h.HelperError, match='changed'):
        h.cmd_actions(['execute', raw, plan['fingerprint']])
    assert not calls
    monkeypatch.setattr(h, '_lsblk_disk', lambda p: {'serial': 'ZR13TAY4Y', 'wwn': 'abc'})
    assert h.cmd_actions(['execute', raw, plan['fingerprint']]) == 0
    assert calls == [['replace', 'primary-z2', OLD, NEW]]


def test_stored_missing_by_id_and_unresolved_member(monkeypatch):
    named = f'''config:
    NAME STATE READ WRITE CKSUM
    primary-z2 DEGRADED 0 0 0
      mirror-0 DEGRADED 0 0 0
        300 UNAVAIL 0 0 0 was /dev/disk/by-id/{OLD}
        /dev/sdb ONLINE 0 0 0
errors: none
'''
    guid = named.replace('mirror-0', '200').replace('/dev/sdb', '400')
    monkeypatch.setattr(h, 'ZPOOL', lambda: '/usr/sbin/zpool')
    monkeypatch.setattr(h, '_run', lambda args, **kw: CompletedProcess(args, 0, guid if '-g' in args else named, ''))
    members = h._pool_members('primary-z2', {})
    assert members[1]['id'] == OLD
    assert members[2]['id'] == ''


def test_short_ids_use_controlled_search_path_without_shell(monkeypatch):
    runner = Mock(return_value=CompletedProcess([], 0, '', ''))
    monkeypatch.setattr(h.subprocess, 'run', runner)
    monkeypatch.setattr(h, 'ZPOOL', lambda: '/usr/sbin/zpool')
    h._run_action(['replace', 'primary-z2', OLD, NEW])
    args, kw = runner.call_args
    assert args[0] == ['/usr/sbin/zpool', 'replace', 'primary-z2', OLD, NEW]
    assert kw['cwd'] == '/dev/disk/by-id'
    assert kw['env']['ZPOOL_IMPORT_PATH'] == '/dev/disk/by-id'
    assert not kw.get('shell')


def test_installer_embeds_current_helper():
    installer = Path('scripts/install-remote.sh').read_text()
    embedded = installer.split("__NASITRON_ROOT_HELPER__'\n", 1)[1].split('\n__NASITRON_ROOT_HELPER__\n', 1)[0]
    assert embedded == Path('remote/nasitron_root_helper.py').read_text().rstrip()


def test_import_discovery_records_topology(monkeypatch):
    output = f'''   pool: backup
     id: 999
  state: ONLINE
 config:
        backup ONLINE
          {OLD} ONLINE
   pool: archive
     id: 888
  state: UNAVAIL
 config:
        archive UNAVAIL
          scsi-missing UNAVAIL
'''
    monkeypatch.setattr(h, 'ZPOOL', lambda: '/usr/sbin/zpool')
    monkeypatch.setattr(h, '_run', lambda *a, **kw: CompletedProcess([], 0, output, ''))
    pools = h._importable_pools()
    assert [p['guid'] for p in pools] == ['999', '888']
    assert pools[0]['members'][1]['name'] == OLD
    assert pools[1]['members'][1]['state'] == 'UNAVAIL'


def test_first_pool_can_use_blank_disk_and_zvols_are_rejected(monkeypatch):
    monkeypatch.setattr(h, '_device', lambda *a, **kw: (a[0], '/dev/nasitron-test-disk'))
    monkeypatch.setattr(h, '_lsblk_disk', lambda p: {'type': 'disk', 'size': 6000, 'kname': 'nasitron-test-disk'})
    monkeypatch.setattr(h, 'WIPEFS', lambda: 'wipefs')
    monkeypatch.setattr(h, 'ZPOOL', lambda: 'zpool')
    monkeypatch.setattr(h, '_run', lambda args, **kw: CompletedProcess(args, 1, '', 'no pools available')
        if args[0] == 'zpool' else CompletedProcess(args, 0, '', ''))
    assert h._validate_blank_disk('/dev/disk/by-id/'+NEW) == ('/dev/nasitron-test-disk', 6000)
    monkeypatch.setattr(h, '_device', lambda *a, **kw: (a[0], '/dev/zd0'))
    with pytest.raises(h.HelperError, match='virtual volumes'):
        h._validate_blank_disk('/dev/disk/by-id/'+NEW)


def test_alias_preference_and_partition_spares(monkeypatch, tmp_path):
    for name in ['wwn-123', 'ata-disk', 'scsi-disk']:
        (tmp_path / name).symlink_to('/dev/nasitron-test-disk')
    monkeypatch.setattr(h, 'BY_ID_DIR', tmp_path)
    assert h._aliases()['/dev/nasitron-test-disk'][0] == 'scsi-disk'
    rows = h._config_rows('config:\n tank ONLINE 0 0 0\n spares\n   scsi-spare AVAIL\nerrors: none')
    assert rows[-1]['name'] == 'scsi-spare' and rows[-1]['role'] == 'spares'


def test_split_never_lets_zfs_choose_unspecified_mirror_disks(host):
    calls, members = host
    members.extend([dict(guid='400', id='mirror-1', state='ONLINE', group=True, parent_guid='', role='special'),
                    dict(guid='500', id='scsi-other', state='ONLINE', group=False, parent_guid='400', role='special')])
    with pytest.raises(h.HelperError, match='every.*mirror'):
        h.action_plan(request('split', new_pool='backup', disks=[OLD]))
    assert not calls
    assert h.action_plan(request('split', new_pool='backup', disks=[OLD, 'scsi-other']))['args'][-2:] == [OLD, 'scsi-other']
    members[-1]['parent_guid'] = 'nested-replacing-vdev'
    with pytest.raises(h.HelperError, match='direct members'):
        h.action_plan(request('split', new_pool='backup', disks=[OLD, 'scsi-other']))


def test_split_rejects_nonmirror_and_duplicate_selection(host):
    calls, members = host
    members.append(dict(guid='301', id='scsi-second', state='ONLINE', group=False, parent_guid='200', role='data'))
    with pytest.raises(h.HelperError, match='exactly one'):
        h.action_plan(request('split', new_pool='backup', disks=[OLD, 'scsi-second']))
    members[0]['id'] = 'raidz2-0'
    with pytest.raises(h.HelperError, match='mirrored'):
        h.action_plan(request('split', new_pool='backup', disks=[OLD]))
    assert not calls


def test_topology_parentage_and_allocation_roles(monkeypatch):
    named = f'''config:
    tank ONLINE 0 0 0
      mirror-0 ONLINE 0 0 0
        /dev/disk/by-id/{OLD} ONLINE 0 0 0
    special
      mirror-1 ONLINE 0 0 0
        /dev/disk/by-id/scsi-special ONLINE 0 0 0
    spares
      /dev/disk/by-id/scsi-spare AVAIL
errors: none
'''
    guids = named.replace('mirror-0', '200').replace('mirror-1', '400').replace('/dev/disk/by-id/'+OLD,'300').replace('/dev/disk/by-id/scsi-special','500').replace('/dev/disk/by-id/scsi-spare','600')
    monkeypatch.setattr(h, 'ZPOOL', lambda: 'zpool')
    monkeypatch.setattr(h, '_run', lambda args, **kw: CompletedProcess(args, 0, guids if '-g' in args else named, ''))
    members = h._pool_members('tank', {})
    assert [m['parent_guid'] for m in members] == ['', '200', '', '400', '']
    assert members[3]['role'] == 'special'
    assert members[-1]['role'] == 'spares'


def test_malformed_action_type_is_validation_error():
    with pytest.raises(h.HelperError):
        h._action_request('{"action":[],"pool":"tank"}')
