from datetime import datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.disk_io import parse_disk_io
from app.main import app
from app.metrics import store_metrics
from app.models import Metric, Server
from app.parser import build_snapshot
from app.security import csrf_token


def samples(first=None, second=None, end_time=102, boot='boot'):
    first = first or [100, 0, 200, 300, 400, 0, 500, 600, 2, 700, 800]
    second = second or [104, 0, 216, 320, 406, 0, 524, 630, 0, 1200, 1800]
    return ('boot\n100 0\n8 0 sda ' + ' '.join(map(str, first)) +
            '\nNASITRON_DISK_IO_NEXT\n' + boot + f'\n{end_time} 0\n8 0 sda ' +
            ' '.join(map(str, second)) + '\n')


def test_io_rates_use_elapsed_time_512_byte_sectors_and_completion_latency():
    io = parse_disk_io(samples())['sda']
    assert io == {'read_bps': 4096, 'write_bps': 6144, 'read_iops': 2,
                  'write_iops': 3, 'read_latency_ms': 5, 'write_latency_ms': 5,
                  'busy_pct': 25, 'queue_depth': 0.5, 'sample_seconds': 2}
    idle = parse_disk_io(samples(second=[100, 0, 200, 300, 400, 0, 500, 600, 0, 700, 800]))['sda']
    assert idle['read_bps'] == idle['write_iops'] == 0
    assert idle['read_latency_ms'] is None
    assert parse_disk_io(samples(second=[1] * 11)) == {}


@pytest.mark.parametrize('text', [samples(end_time=100), samples(boot='rebooted'),
                                  samples().split('NASITRON')[0], samples().replace('216', 'bad')])
def test_io_rejects_resets_or_incomplete_samples(text):
    with pytest.raises(ValueError):
        parse_disk_io(text)


def test_snapshot_attaches_only_physical_disk_io_and_failed_command_does_not_reuse_it():
    raw = {'disk_io': {'exit': 0, 'stdout': samples()},
           'lsblk': {'exit': 0, 'stdout': json.dumps({'blockdevices': [
               {'type': 'disk', 'name': 'sda', 'kname': 'sda', 'path': '/dev/sda',
                'children': [{'type': 'part', 'name': 'sda1'}]}]})}}
    snapshot = build_snapshot(raw)
    assert len(snapshot['drives']) == 1
    assert snapshot['drives'][0]['io']['read_bps'] == 4096
    raw['disk_io']['stdout_truncated'] = True
    snapshot = build_snapshot(raw)
    assert snapshot['drives'][0]['io'] == {}
    assert any(e['subsystem'] == 'drives.io' for e in snapshot['collection']['errors'])


@pytest.mark.parametrize('passed', [True, False])
def test_smart_result_stores_without_argument_error_and_io_does_not_require_smart(passed):
    init_db()
    with SessionLocal() as db:
        server = Server(name='io-regression', host='localhost', username='nasitron', enabled=False)
        db.add(server)
        db.flush()
        store_metrics(db, server.id, datetime.utcnow(), {'drives': [
            {'serial': 'smart', 'smart': {'data_available': True, 'smart_passed': passed,
                                        'temperature_c': 32, 'pending_sectors': 3},
             'io': {'read_bps': 123}},
            {'serial': 'no-smart', 'smart': {}, 'io': {'write_iops': 4}},
        ]})
        rows = db.scalars(select(Metric).where(Metric.server_id == server.id)).all()
        metrics = {(r.scope, r.name): r.value for r in rows}
        assert metrics['smart', 'drive.smart_passed'] == int(passed)
        assert metrics['smart', 'drive.pending_sectors'] == 3
        assert metrics['smart', 'drive.io.read_bps'] == 123
        assert metrics['no-smart', 'drive.io.write_iops'] == 4
        store_metrics(db, server.id, datetime.utcnow(), {'collection': {'stale_subsystems': ['drives.inventory']},
            'drives': [{'serial': 'stale', 'io': {'read_bps': 999}}]})
        assert not db.scalars(select(Metric).where(Metric.scope == 'stale')).all()
        db.rollback()


def test_io_history_presets_custom_timezone_bounds_scope_and_poll_cadence():
    init_db()
    with SessionLocal() as db:
        server = Server(name='io-history', host='localhost', username='nasitron', enabled=False,
                        poll_interval_seconds=30, smart_interval_minutes=60)
        db.add(server)
        db.flush()
        sid = server.id
        now = datetime.utcnow().replace(microsecond=0)
        for scope in ['disk-a', 'disk-b']:
            for minutes in [5, 20, 40]:
                db.add(Metric(server_id=sid, name='drive.io.read_bps', scope=scope,
                              value=minutes, captured_at=now - timedelta(minutes=minutes)))
        db.commit()
    with TestClient(app) as client:
        client.post('/login', data={'username': 'ci-admin', 'password': 'ci-password-strong',
                                   'csrf_token': csrf_token(), 'next': '/'})
        url = f'/api/servers/{sid}/metrics'
        params = {'name': 'drive.io.read_bps', 'scope': 'disk-a', 'hours': '0.25'}
        data = client.get(url, params=params).json()
        assert data['sample_count'] == 1
        assert data['points'][0]['v'] == 5
        assert data['expected_interval_seconds'] == 30
        params.update(start=(now - timedelta(minutes=30)).isoformat()+'Z',
                      end=(now - timedelta(minutes=10)).isoformat()+'Z')
        data = client.get(url, params=params).json()
        assert data['sample_count'] == 1 and data['points'][0]['v'] == 20
        # Offset timestamps refer to the same UTC interval.
        params['start'] = (now - timedelta(hours=4, minutes=30)).isoformat()+'-04:00'
        assert client.get(url, params=params).json()['sample_count'] == 1
        params['end'] = params['start']
        assert client.get(url, params=params).status_code == 400
        params.pop('end')
        assert client.get(url, params=params).status_code == 400
        params.update(start=now.isoformat(), end=(now+timedelta(hours=1)).isoformat())
        assert client.get(url, params=params).status_code == 400
        page = client.get('/disk-io', params={'server_id': sid, 'identity': 'disk-a'})
        assert page.status_code == 200 and 'Historical device' in page.text
    with SessionLocal() as db:
        db.delete(db.get(Server, sid))
        db.commit()
