from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.display import format_temperature, power_on_duration, temperature_message
from app.main import app
from app.models import CurrentState, Metric, Server
from app.security import csrf_token
from app.settings_store import get_setting, set_setting


@pytest.mark.parametrize('hours,expected', [(None, '—'), (-1, '—'), (float('nan'), '—'),
    (0, '0 hours'), (1, '1 hour'), (23, '23 hours'), (24, '1 day'),
    (48, '2 days'), (720, '1 month'), (1440, '2 months'), (8760, '1 year'), (17520, '2 years')])
def test_power_on_units(hours, expected):
    assert power_on_duration(hours) == expected


def test_temperature_units():
    assert format_temperature(0, 'F') == '32°F'
    assert format_temperature(45, 'F') == '113°F'
    assert format_temperature(None, 'F') == '—'
    assert temperature_message('Drive temperature is 45 C. Critical is 55 C.', 'F') == 'Drive temperature is 113°F. Critical is 131°F.'


def test_display_settings_thresholds_and_pool_history():
    init_db()
    with SessionLocal() as db:
        original = {k: get_setting(db, k) for k in ('temperature_unit', 'drive_temp_warning_c', 'drive_temp_critical_c')}
        server = Server(name='pool-display-test', host='localhost', username='nasitron', enabled=False)
        db.add(server)
        db.flush()
        sid = server.id
        db.add(CurrentState(server_id=sid, captured_at=datetime.utcnow(), payload_json=json.dumps({
            'pools': [{'name': 'tank', 'io': {'read_bps': 1024}}],
            'drives': [{'path': '/dev/sda', 'serial': 'unit-drive', 'size_bytes': 1000000, 'zfs_memberships': [], 'smart': {'temperature_c': 45, 'power_on_hours': 17520}}]})))
        for pool, value in [('tank', 1024), ('retired', 2048)]:
            db.add(Metric(server_id=sid, name='pool.read_bps', scope=pool, value=value, captured_at=datetime.utcnow()))
        db.commit()
    try:
        with TestClient(app) as client:
            client.post('/login', data={'username': 'ci-admin', 'password': 'ci-password-strong', 'csrf_token': csrf_token(), 'next': '/pool-io'})
            def post(**data):
                return client.post('/settings', data={'csrf_token': csrf_token(), **data})
            assert post(section='display', temperature_unit='K').status_code == 400
            assert post(section='display', temperature_unit='F').status_code == 200
            page = client.get('/drives').text
            assert '113°F' in page and '2 years' in page
            page = client.get('/settings?tab=health').text
            assert 'Drive temperature warning °F' in page
            assert post(section='health', threshold_temperature_unit='F', drive_temp_warning=113, drive_temp_critical=131).status_code == 200
            with SessionLocal() as db:
                assert float(get_setting(db, 'drive_temp_warning_c')) == 45
                assert float(get_setting(db, 'drive_temp_critical_c')) == 55
                assert get_setting(db, 'temperature_unit') == 'F'
            assert post(section='health', threshold_temperature_unit='F', drive_temp_warning=150, drive_temp_critical=100).status_code == 400
            assert post(section='health', drive_temp_warning='nan', drive_temp_critical=50).status_code == 400
            for pool in ('tank', 'retired'):
                response = client.get('/pool-io', params={'server_id': sid, 'identity': pool})
                assert response.status_code == 200
                assert f'data-identity="{pool}"' in response.text
                assert 'data-metric-prefix="pool."' in response.text
                assert 'Statistics' in response.text
            response = client.get(f'/api/servers/{sid}/metrics/batch', params={'pairs': json.dumps(['pool.read_bps','tank']), 'hours':1})
            assert response.status_code == 200
            assert response.json()['series'][0]['points'][0]['v'] == 1024
            assert client.get('/pool-io?server_id=999999999').status_code == 404
            assert post(section='display', temperature_unit='C').status_code == 200
            assert '45°C' in client.get('/drives').text
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            for key, value in original.items():
                set_setting(db, key, value)
            db.commit()
