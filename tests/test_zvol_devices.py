import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.collector import SSHCollector
from app.devices import is_zvol, device_label, drive_health
from app.db import SessionLocal
from app.main import app
from app.models import Server, CurrentState, Enclosure
from app.parser import build_snapshot
from app.security import csrf_token


@pytest.mark.parametrize('name,expected', [('zd0', True), ('/dev/zd16', True),
    ('zd0p1', True), ('/dev/zvol/tank/vm', True), ('zdb0', False),
    ('sda', False), ('nvme0n1', False), ('zd', False)])
def test_zvol_identification(name, expected):
    assert is_zvol({'path': name}) == expected


def test_zvol_skips_smart_collection_and_parsing():
    nodes = [{'path': '/dev/zd0', 'type': 'disk'}, {'path': '/dev/sda', 'type': 'disk'}]
    assert SSHCollector._disk_paths(nodes) == ['/dev/sda']
    snapshot = build_snapshot({'lsblk': {'exit': 0, 'stdout': json.dumps({'blockdevices': nodes})},
                               'smart_sampled': True, 'smart_inventory_ok': True})
    volume = snapshot['drives'][0]
    assert volume['smart']['not_applicable']
    assert drive_health(volume) == ('unknown', 'SMART not applicable')
    assert device_label(volume) == 'ZFS virtual volume (zvol)'


def test_retained_zvol_inventory_and_bay_assignment():
    with TestClient(app) as client:
        client.post('/login', data={'username': 'ci-admin', 'password': 'ci-password-strong',
            'csrf_token': csrf_token(), 'next': '/'}, follow_redirects=False)
        with SessionLocal() as db:
            server = Server(name='zvol-test', host='localhost', username='test', enabled=False)
            db.add(server)
            db.flush()
            sid = server.id
            db.add(CurrentState(server_id=sid, captured_at=datetime.utcnow(), payload_json=json.dumps({
                'drives': [{'path': '/dev/zd0', 'serial': 'virtual-serial', 'model': '',
                            'size_bytes': 1024**3, 'rotational': False, 'transport': '',
                            'smart': {'smart_passed': False}, 'zfs_memberships': []}]})))
            enclosure = Enclosure(server_id=sid, name='Rack', rows=1, columns=1)
            db.add(enclosure)
            db.flush()
            eid = enclosure.id
            db.commit()
        try:
            page = client.get('/drives')
            assert page.status_code == 200
            assert 'ZFS virtual volume (zvol)' in page.text
            assert 'SMART not applicable' in page.text
            assert 'Physical drives</div><div class="metric-value">0</div>' in page.text
            assert 'Virtual device · no physical bay' in page.text
            page = client.get(f'/servers/{sid}/drive?identity=virtual-serial')
            assert page.status_code == 200
            assert 'ZFS virtual volume (zvol)' in page.text
            assert 'Temperature history' not in page.text
            assert 'Physical bay / location' not in page.text
            page = client.get(f'/drive-bays?server={sid}')
            assert '<option value="virtual-serial">' not in page.text
            assert client.post(f'/enclosures/{eid}/assign', data={'csrf_token': csrf_token(),
                'slot': 1, 'identity': 'virtual-serial'}, follow_redirects=False).status_code == 400
        finally:
            with SessionLocal() as db:
                db.delete(db.get(Server, sid))
                db.commit()
