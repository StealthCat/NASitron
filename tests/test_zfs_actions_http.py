from datetime import datetime, timedelta
import json
import re
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import zfs_actions as actions
from app.db import SessionLocal
from app.main import app
from app.models import MaintenanceAction, Server, WebUser
from app.security import csrf_token


@pytest.fixture
def web(monkeypatch):
    plan = dict(request={'action': 'offline', 'pool': 'primary-z2', 'target': '300'},
        command='zpool offline primary-z2 scsi-SATA_WDC_serial', confirmation='OFFLINE primary-z2',
        warning='Reduces redundancy', dry_run='', fingerprint='abc')
    helper = Mock(return_value=plan)
    runner = Mock(return_value={'exit': 0, 'stdout': 'OK', 'stderr': ''})
    monkeypatch.setattr(actions, 'helper_json', helper)
    monkeypatch.setattr(actions, 'helper_call', runner)
    monkeypatch.setattr(actions, 'trigger_now', lambda *a: None)
    with TestClient(app) as client:
        response = client.post('/login', data={'username': 'ci-admin', 'password': 'ci-password-strong',
            'csrf_token': csrf_token(), 'next': '/'}, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            server = Server(name='ZFS action test', host='127.0.0.1', username='nasitron', enabled=False)
            db.add(server)
            db.commit()
            sid = server.id
        yield client, sid, helper, runner, plan
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            db.commit()


def preview(web):
    client, sid, helper, runner, plan = web
    response = client.post(f'/servers/{sid}/zfs-actions/preview', data={'action': 'offline',
        'pool': 'primary-z2', 'target': '300', 'csrf_token': csrf_token()})
    assert response.status_code == 200, response.text
    assert plan['command'] in response.text
    assert runner.call_count == 0
    return int(re.search(r'name="action_id" value="(\d+)"', response.text)[1])


def submit(web, action_id, **kw):
    client, sid, *_ = web
    return client.post(f'/servers/{sid}/zfs-actions/execute', data={
        'action_id': action_id, 'confirm_text': 'OFFLINE primary-z2', 'csrf_token': csrf_token(), **kw}, follow_redirects=False)


def test_confirmation_csrf_and_one_use(web):
    aid = preview(web)
    assert submit(web, aid, confirm_text='OFFLINE wrong').status_code == 400
    assert submit(web, aid, csrf_token='invalid').status_code == 403
    assert web[3].call_count == 0
    assert submit(web, aid).status_code == 303
    assert submit(web, aid).status_code == 409
    assert web[3].call_count == 1
    with SessionLocal() as db:
        row = db.get(MaintenanceAction, aid)
        assert row.success and row.state == 'accepted' and row.completed_at
        assert row.actor == 'ci-admin'


@pytest.mark.parametrize('change,expected', [('expired', 409), ('actor', 404), ('server', 404)])
def test_preview_bound_to_actor_server_and_expiry(web, change, expected):
    aid = preview(web)
    with SessionLocal() as db:
        row = db.get(MaintenanceAction, aid)
        if change == 'expired':
            row.created_at = datetime.utcnow() - timedelta(minutes=11)
        elif change == 'actor':
            row.actor = 'another-admin'
        db.commit()
    target_web = (web[0], 0, *web[2:]) if change == 'server' else web
    assert submit(target_web, aid).status_code == expected
    assert web[3].call_count == 0


@pytest.mark.parametrize('result,state', [({'exit': 2, 'stderr': 'topology changed'}, 'failed'),
    ({'exit': 124, 'stderr': 'timeout'}, 'unknown'),
    ({'exit': 255, 'stderr': 'Remote command deadline exceeded'}, 'unknown'),
    ({'exit': -1, 'stderr': 'No exit status'}, 'unknown'), (RuntimeError('connection lost'), 'unknown')])
def test_failed_and_unknown_outcomes_cannot_replay(web, result, state):
    aid = preview(web)
    if isinstance(result, Exception):
        web[3].side_effect = result
    else:
        web[3].return_value = result
    assert submit(web, aid).status_code == 303
    assert submit(web, aid).status_code == 409
    with SessionLocal() as db:
        row = db.get(MaintenanceAction, aid)
        assert row.state == state and not row.success
    assert web[3].call_count == 1


def test_old_helper_error_and_preview_failure(web):
    client, sid, helper, runner, _ = web
    helper.side_effect = ValueError('Unknown helper command')
    response = client.get(f'/servers/{sid}/zfs-actions')
    assert response.status_code == 200
    assert 'Install the current' in response.text
    response = client.post(f'/servers/{sid}/zfs-actions/preview', data={
        'action': 'offline', 'pool': 'primary-z2', 'csrf_token': csrf_token()})
    assert response.status_code == 400
    assert not runner.called


@pytest.mark.parametrize('role', ['operator', 'viewer'])
def test_only_admins_can_use_zfs_actions(web, role):
    client, sid, helper, runner, _ = web
    with SessionLocal() as db:
        user = db.scalar(select(WebUser).where(WebUser.username == 'ci-admin'))
        user.is_admin = False
        user.role = role
        db.commit()
    try:
        assert client.get(f'/servers/{sid}/zfs-actions').status_code == 403
        assert client.post(f'/servers/{sid}/zfs-actions/preview', data={
            'action': 'destroy', 'pool': 'primary-z2', 'csrf_token': csrf_token()}).status_code == 403
        assert not helper.called and not runner.called
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(WebUser).where(WebUser.username == 'ci-admin'))
            user.is_admin = True
            user.role = 'admin'
            db.commit()


def test_inventory_renders_and_json_is_escaped(web, tmp_path):
    client, sid, helper, _, _ = web
    helper.return_value = {'protocol': 1, 'actions': [{'id': 'replace', 'label': 'Replace', 'warning': 'Review'}],
        'pools': [{'name': 'primary-z2', 'members': []}], 'disks': [{'id': 'scsi-</script>example'}], 'importable': []}
    response = client.get(f'/servers/{sid}/zfs-actions')
    assert response.status_code == 200
    assert 'scsi-</script>' not in response.text
    encoded = re.search(r'<script id="zfs-inventory" type="application/json">(.*?)</script>', response.text)[1]
    assert json.loads(encoded)['protocol'] == 1


def test_incompatible_helper_does_not_render_action_form(web):
    client, sid, helper, runner, _ = web
    helper.return_value = {'protocol': 2, 'actions': 'invalid'}
    response = client.get(f'/servers/{sid}/zfs-actions')
    assert response.status_code == 200
    assert 'id="zfs-action-form"' not in response.text
    assert 'Update the remote helper' in response.text


def test_busy_preview_and_execute_are_conflicts_without_consuming_preview(web):
    from app.maintenance import maintenance_lock
    aid = preview(web)
    client, sid, helper, runner, _ = web
    with maintenance_lock(sid):
        response = client.post(f'/servers/{sid}/zfs-actions/preview', data={
            'action': 'offline', 'pool': 'primary-z2', 'csrf_token': csrf_token()})
        assert response.status_code == 409
        assert submit(web, aid).status_code == 409
    assert not runner.called
    with SessionLocal() as db:
        assert db.get(MaintenanceAction, aid).state == 'preview'
    assert submit(web, aid).status_code == 303


def test_refresh_failure_does_not_hide_successful_execution(web, monkeypatch):
    aid = preview(web)
    monkeypatch.setattr(actions, 'trigger_now', Mock(side_effect=RuntimeError('Scheduler unavailable')))
    assert submit(web, aid).status_code == 303
    with SessionLocal() as db:
        assert db.get(MaintenanceAction, aid).state == 'accepted'
    assert web[3].call_count == 1
