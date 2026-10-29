"""Credential-free integration coverage, using real SQLite and mocked Zoom HTTP."""
import datetime as dt
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database import get_db
from main import app
from models import Base, Event, EventMeeting, CreationRequest, ZoomConnection, ZoomOAuthState
from zoom import now, store_tokens

SCOPES = 'user:read:user meeting:write:meeting meeting:read:list_meetings meeting:read:meeting'


class FakeZoom:
    def __init__(self):
        self.meetings = []
        self.posts = []
        self.mode = 'ok'
        self.revoked = set()
        self.refreshes = 0
        self.mutex = threading.Lock()

    def __call__(self, method, url, **kwargs):
        if url.endswith('/oauth/token'):
            if kwargs['data']['grant_type'] == 'refresh_token':
                self.refreshes += 1
                if self.mode == 'refresh_denied':
                    return httpx.Response(400, json={})
            return httpx.Response(200, json={'access_token': 'host-a', 'refresh_token': 'rotated', 'expires_in': 3600, 'scope': SCOPES})
        if url.endswith('/oauth/revoke'):
            return httpx.Response(200, json={})
        host = kwargs['headers']['Authorization'].split(' ')[1]
        if host in self.revoked:
            return httpx.Response(401, json={})
        if url.endswith('/users/me'):
            return httpx.Response(200, json={'id': host})
        if method == 'GET' and url.endswith('/users/me/meetings'):
            return httpx.Response(200, json={'meetings': [{'id': m['id']} for m in self.meetings if m['host_id'] == host]})
        if method == 'GET' and '/meetings/' in url:
            return httpx.Response(200, json=next(m for m in self.meetings if str(m['id']) == url.rsplit('/', 1)[1]))
        if method == 'POST' and url.endswith('/users/me/meetings'):
            with self.mutex:
                self.posts.append((host, kwargs['json']))
                if self.mode == 'rejected':
                    return httpx.Response(429, json={})
                if self.mode == 'forbidden':
                    return httpx.Response(403, json={})
                if self.mode == 'transient':
                    return httpx.Response(503, json={})
                if self.mode == 'timeout_absent':
                    raise httpx.ReadTimeout('ambiguous')
                result = {**kwargs['json'], 'id': len(self.meetings) + 100, 'host_id': host,
                          'join_url': f'https://zoom.us/j/{len(self.meetings) + 100}', 'start_url': 'DO NOT EXPOSE'}
                self.meetings.append(result)
                if self.mode == 'timeout_created':
                    raise httpx.ReadTimeout('ambiguous')
                if self.mode == 'malformed':
                    return httpx.Response(201, json={'id': result['id']})
                return httpx.Response(201, json=result)
        raise AssertionError((method, url))


class ZoomFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + self.temp.name + '/test.db', connect_args={'check_same_thread': False, 'timeout': 30})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        def database():
            with self.sessions() as session:
                yield session
        app.dependency_overrides[get_db] = database
        self.environment = patch.dict(os.environ, {'JWT_SECRET_KEY': 'test-secret-' * 5,
            'ZOOM_CLIENT_ID': 'test-client', 'ZOOM_CLIENT_SECRET': 'test-secret',
            'ZOOM_REDIRECT_URI': 'http://localhost:8000/zoom/callback', 'ZOOM_TOKEN_KEY': Fernet.generate_key().decode()})
        self.environment.start()
        self.zoom = FakeZoom()
        self.transport = patch('zoom.transport', self.zoom)
        self.transport.start()
        self.client = TestClient(app)
        self.a = self.register('alice')
        self.b = self.register('bob')
        self.body = {'title': 'Planning', 'start': '2026-11-01T01:30:00', 'end': '2026-11-01T02:30:00', 'timezone': 'America/Los_Angeles'}

    def tearDown(self):
        self.client.close()
        self.transport.stop()
        self.environment.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp.cleanup()

    def register(self, username):
        result = self.client.post('/auth/register', json={'username': username, 'password': 'long-test-password'})
        self.assertEqual(result.status_code, 200)
        return {'Authorization': 'Bearer ' + result.json()['access_token']}

    def connect(self, organizer=1, host='host-a', expired=False):
        with self.sessions() as db:
            connection = ZoomConnection(organizer_id=organizer, zoom_user_id=host)
            store_tokens(connection, {'access_token': host, 'refresh_token': 'secret-refresh', 'expires_in': 3600, 'scope': SCOPES})
            if expired:
                connection.expires_at = now() - dt.timedelta(seconds=1)
            db.merge(connection)
            db.commit()

    def create(self, body=None, headers=None, key=None):
        return self.client.post('/events', json=body or self.body, headers={**(headers or self.a), 'Idempotency-Key': key or str(uuid4())})

    def count_events(self):
        with self.sessions() as db:
            return db.query(Event).count()

    def test_authentication_missing_denied_and_revoked_block_without_saving(self):
        self.assertEqual(self.client.post('/events', json=self.body).status_code, 401)
        result = self.create()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['detail']['code'], 'zoom_connection_required')
        self.assertEqual(self.count_events(), 0)
        self.connect()
        self.zoom.revoked.add('host-a')
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(len(self.zoom.posts), 0)
        self.zoom.revoked.clear()
        self.connect()
        self.assertEqual(self.create().status_code, 200)

    def test_account_isolation_ownership_association_and_repeat(self):
        self.connect()
        self.connect(2, 'host-b')
        key = str(uuid4())
        first = self.create(key=key)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.create(key=key)
        self.assertEqual(first.json(), second.json())
        other = self.create(headers=self.b)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual([p[0] for p in self.zoom.posts], ['host-a', 'host-b'])
        self.assertEqual(first.json()['event']['organizer_id'], 1)
        self.assertEqual(first.json()['event']['zoom_host_id'], 'host-a')
        self.assertNotIn('start_url', first.text)
        listing = self.client.get('/events?start=2026-10-31&end=2026-11-03', headers=self.b)
        self.assertEqual(len(listing.json()), 1)
        self.assertEqual(listing.json()[0]['zoom_host_id'], 'host-b')
        for method in ('put', 'delete'):
            result = getattr(self.client, method)(f"/events/{first.json()['event']['id']}", headers=self.b, **({'json': {'title': 'stolen'}} if method == 'put' else {}))
            self.assertEqual(result.status_code, 404)
        self.assertEqual(self.create(body={**self.body, 'title': 'changed'}, key=key).status_code, 409)
        self.assertEqual(len(self.zoom.posts), 2)

    def test_timeout_reconciles_original_without_reposting(self):
        self.connect()
        self.zoom.mode = 'timeout_created'
        key = str(uuid4())
        self.assertEqual(self.create(key=key).status_code, 503)
        self.assertEqual(self.count_events(), 0)
        self.zoom.mode = 'ok'
        result = self.create(key=key)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.zoom.posts), 1)
        self.assertEqual(result.json()['event']['zoom_meeting_id'], '100')
        self.assertEqual(self.count_events(), 1)

    def test_absent_uncertain_and_5xx_never_blindly_retry(self):
        self.connect()
        for mode in ('timeout_absent', 'transient'):
            self.zoom.mode = mode
            key = str(uuid4())
            self.assertEqual(self.create(key=key).status_code, 503)
            self.zoom.mode = 'ok'
            self.assertEqual(self.create(key=key).status_code, 409)
        self.assertEqual(len(self.zoom.posts), 2)
        self.assertEqual(self.count_events(), 0)

    def test_definitive_rejection_can_retry_same_request(self):
        self.connect()
        self.zoom.mode = 'rejected'
        key = str(uuid4())
        self.assertEqual(self.create(key=key).status_code, 503)
        self.zoom.mode = 'ok'
        self.assertEqual(self.create(key=key).status_code, 200)
        self.assertEqual(len(self.zoom.meetings), 1)

    def test_missing_permissions_disable_connection(self):
        self.connect()
        self.zoom.mode = 'forbidden'
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.count_events(), 0)
        self.assertFalse(self.client.get('/zoom/connection', headers=self.a).json()['connected'])

    def test_concurrent_repeated_request_one_event_and_meeting(self):
        self.connect()
        key = str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.create(key=key), range(2)))
        self.assertTrue(any(r.status_code == 200 for r in results), [r.text for r in results])
        self.assertTrue(all(r.status_code in (200, 409) for r in results))
        self.assertEqual(self.create(key=key).status_code, 200)
        self.assertEqual(len(self.zoom.posts), 1)
        self.assertEqual(self.count_events(), 1)

    def test_refresh_rotates_encrypted_tokens_and_denial_requires_reconnect(self):
        self.connect(expired=True)
        self.assertTrue(self.client.get('/zoom/connection', headers=self.a).json()['connected'])
        self.assertEqual(self.zoom.refreshes, 1)
        with self.sessions() as db:
            connection = db.get(ZoomConnection, 1)
            self.assertNotIn('host-a', connection.access_token)
            self.assertNotEqual(connection.refresh_token, 'rotated')
            connection.expires_at = now() - dt.timedelta(seconds=1)
            db.commit()
        self.zoom.mode = 'refresh_denied'
        self.assertFalse(self.client.get('/zoom/connection', headers=self.a).json()['connected'])
        self.assertEqual(self.create().status_code, 409)

    def test_oauth_browser_bound_single_use_and_denied(self):
        from urllib.parse import urlparse, parse_qs
        result = self.client.post('/zoom/connect', headers=self.a)
        self.assertEqual(result.status_code, 200)
        state = parse_qs(urlparse(result.json()['url']).query)['state'][0]
        stranger = TestClient(app)
        try:
            invalid = stranger.get('/zoom/callback', params={'state': state, 'code': 'fake'})
            self.assertIn('failed', invalid.text)
        finally:
            stranger.close()
        denied = self.client.get('/zoom/callback', params={'state': state, 'error': 'access_denied'})
        self.assertIn('denied', denied.text)
        with self.sessions() as db:
            self.assertEqual(db.query(ZoomConnection).count(), 0)
            self.assertEqual(db.query(ZoomOAuthState).count(), 0)
        result = self.client.post('/zoom/connect', headers=self.b)
        state = parse_qs(urlparse(result.json()['url']).query)['state'][0]
        self.assertIn('connected', self.client.get('/zoom/callback', params={'state': state, 'code': 'fake'}).text)
        with self.sessions() as db:
            self.assertIsNone(db.get(ZoomConnection, 1))
            self.assertEqual(db.get(ZoomConnection, 2).zoom_user_id, 'host-a')
        replay = self.client.get('/zoom/callback', params={'state': state, 'code': 'fake'})
        self.assertIn('invalid_state', replay.text)

    def test_original_zoom_account_required_for_pending_request(self):
        self.connect()
        key = str(uuid4())
        self.zoom.mode = 'timeout_created'
        self.create(key=key)
        self.connect(host='host-different')
        self.zoom.mode = 'ok'
        result = self.create(key=key)
        self.assertEqual(result.json()['detail']['code'], 'zoom_original_account_required')
        self.assertEqual(len(self.zoom.posts), 1)

    def test_dst_gap_fold_offset_and_recurring_schedules(self):
        self.connect()
        cases = [
            ('2026-03-08T02:30:00', '2026-03-08T04:00:00', '2026-03-08T10:00:00Z', 60),
            ('2026-11-01T01:30:00', '2026-11-01T02:30:00', '2026-11-01T08:30:00Z', 120),
            ('2026-11-01T01:30:00-08:00', '2026-11-01T02:30:00-08:00', '2026-11-01T09:30:00Z', 60),
        ]
        for start, end, expected, duration in cases:
            result = self.create(body={**self.body, 'start': start, 'end': end})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['event']['start'], expected)
            self.assertEqual(self.zoom.posts[-1][1]['start_time'], expected)
            self.assertEqual(self.zoom.posts[-1][1]['duration'], duration)
        body = {**self.body, 'start': '2026-03-01T09:00:00', 'end': '2026-03-01T10:00:00',
                'recurrence': {'freq': 'WEEKLY', 'count': 3}}
        result = self.create(body=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([m['start'] for m in result.json()['meetings']], ['2026-03-01T17:00:00Z', '2026-03-08T16:00:00Z', '2026-03-15T16:00:00Z'])
        listing = self.client.get('/events?start=2026-03-01&end=2026-03-20', headers=self.a).json()
        recurring = [event for event in listing if event['is_recurring']]
        self.assertEqual(len(recurring), 3)
        self.assertEqual(len({event['zoom_meeting_id'] for event in recurring}), 3)
        for freq in ('DAILY', 'MONTHLY', 'YEARLY'):
            result = self.create(body={**body, 'recurrence': {'freq': freq, 'count': 2}})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(len(result.json()['meetings']), 2)

    def test_finite_recurrence_validation_and_inclusive_until(self):
        self.connect()
        for body in ({**self.body, 'end': self.body['start']}, {**self.body, 'timezone': 'unknown'},
                     {**self.body, 'recurrence': {'freq': 'WEEKLY'}},
                     {**self.body, 'recurrence': {'freq': 'BAD', 'count': 2}}):
            self.assertEqual(self.create(body=body).status_code, 422)
        body = {'title': 'finite', 'start': '2026-01-01T09:00:00', 'end': '2026-01-01T10:00:00',
                'recurrence': {'freq': 'DAILY', 'until': '2026-01-02'}}
        result = self.create(body=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()['meetings']), 2)

    def test_partial_series_failure_resumes_without_duplicate_occurrences(self):
        self.connect()
        original = self.zoom.__call__
        calls = 0
        def fail_second(method, url, **kwargs):
            nonlocal calls
            if method == 'POST' and url.endswith('/meetings'):
                calls += 1
                self.zoom.mode = 'timeout_created' if calls == 2 else 'ok'
            return original(method, url, **kwargs)
        body = {**self.body, 'recurrence': {'freq': 'DAILY', 'count': 3}}
        key = str(uuid4())
        with patch('zoom.transport', fail_second):
            self.assertEqual(self.create(body=body, key=key).status_code, 503)
            self.assertEqual(self.count_events(), 0)
            result = self.create(body=body, key=key)
            self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.zoom.posts), 3)
        self.assertEqual(len(result.json()['meetings']), 3)

    def test_existing_events_connect_and_edit_never_backfill(self):
        with self.sessions() as db:
            event = Event(title='legacy', start_utc=dt.datetime(2026, 11, 1, 12), end_utc=dt.datetime(2026, 11, 1, 13), timezone='UTC')
            db.add(event)
            db.commit()
            legacy_id = event.id
        self.connect()
        self.client.get('/zoom/connection', headers=self.a)
        result = self.client.get('/events?start=2026-11-01&end=2026-11-03', headers=self.a)
        self.assertIsNone(result.json()[0]['zoom_meeting_id'])
        self.client.put(f'/events/{legacy_id}', json={'title': 'edited'}, headers=self.a)
        self.assertEqual(len(self.zoom.posts), 0)
        with self.sessions() as db:
            self.assertEqual(db.query(EventMeeting).count(), 0)
            self.assertIsNone(db.get(Event, legacy_id).organizer_id)

    def test_replay_after_event_deletion_does_not_recreate_remote_meeting(self):
        self.connect()
        key = str(uuid4())
        result = self.create(key=key)
        self.client.delete(f"/events/{result.json()['event']['id']}", headers=self.a)
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(self.create(key=key).json(), result.json())
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(len(self.zoom.posts), 1)

    def test_missing_scope_never_establishes_usable_connection(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            store_tokens(ZoomConnection(organizer_id=1, zoom_user_id='host-a'),
                         {'access_token': 'secret', 'refresh_token': 'secret', 'expires_in': 3600, 'scope': 'user:read:user'})

    def test_malformed_success_is_reconciled_without_reposting(self):
        self.connect()
        self.zoom.mode = 'malformed'
        key = str(uuid4())
        self.assertEqual(self.create(key=key).status_code, 409)
        self.zoom.mode = 'ok'
        result = self.create(key=key)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.zoom.posts), 1)

    def test_pagination_and_full_agenda_reconciliation(self):
        self.connect()
        self.zoom.mode = 'timeout_created'
        key = str(uuid4())
        self.create(key=key)
        original = self.zoom.__call__
        def paginated(method, url, **kwargs):
            if url.endswith('/users/me/meetings') and method == 'GET' and not kwargs['params']['next_page_token']:
                return httpx.Response(200, json={'meetings': [], 'next_page_token': 'next'})
            return original(method, url, **kwargs)
        with patch('zoom.transport', paginated):
            result = self.create(key=key)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.zoom.posts), 1)

    def test_finalize_failure_and_restart_reuse_durable_meeting(self):
        self.connect()
        key = str(uuid4())
        with patch('routes.detect_conflicts_for_event', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                self.create(key=key)
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(self.create(key=key).status_code, 200)
        self.assertEqual(len(self.zoom.posts), 1)

    def test_recurring_explicit_fold_and_first_rule_date(self):
        self.connect()
        body = {**self.body, 'start': '2026-11-01T01:30:00-08:00', 'end': '2026-11-01T02:30:00-08:00',
                'recurrence': {'freq': 'DAILY', 'count': 2}}
        result = self.create(body=body)
        self.assertEqual(result.status_code, 200, result.text)
        listing = self.client.get('/events?start=2026-11-01&end=2026-11-04', headers=self.a).json()
        self.assertEqual(listing[0]['start'], '2026-11-01T09:30:00Z')
        self.assertEqual(listing[0]['zoom_meeting_id'], result.json()['meetings'][0]['zoom_meeting_id'])
        self.assertEqual(self.create(body={**body, 'recurrence': {'freq': 'WEEKLY', 'count': 2, 'byday': 'MO'}}).status_code, 422)

    def test_startup_creates_no_zoom_meetings_and_legacy_rows_have_no_owner(self):
        with patch('database.engine', self.engine), patch('main.SessionLocal', self.sessions):
            with TestClient(app):
                pass
        self.assertEqual(len(self.zoom.posts), 0)
        with self.sessions() as db:
            self.assertTrue(db.query(Event).count() > 0)
            self.assertEqual(db.query(EventMeeting).count(), 0)
            self.assertTrue(all(event.organizer_id is None for event in db.query(Event).all()))

    def test_key_rotation_requires_reconnection_and_disconnect_still_erases_tokens(self):
        self.connect()
        with patch.dict(os.environ, {'ZOOM_TOKEN_KEY': Fernet.generate_key().decode()}):
            result = self.client.get('/zoom/connection', headers=self.a)
            self.assertFalse(result.json()['connected'])
            self.assertEqual(result.json()['code'], 'zoom_reconnect_required')
            self.assertEqual(self.client.delete('/zoom/connection', headers=self.a).status_code, 200)
        with self.sessions() as db:
            self.assertIsNone(db.get(ZoomConnection, 1))

    def test_editing_created_occurrence_retains_link_without_synchronizing_zoom(self):
        self.connect()
        result = self.create(body={**self.body, 'recurrence': {'freq': 'DAILY', 'count': 2}})
        self.assertEqual(result.status_code, 200)
        event_id = result.json()['event']['id']
        first_meeting = result.json()['meetings'][0]['zoom_meeting_id']
        self.client.put(f'/events/{event_id}/occurrence/2026-11-01', headers=self.a,
                        json={'start': '2026-11-01T12:00:00', 'end': '2026-11-01T13:00:00'})
        listing = self.client.get('/events?start=2026-11-01&end=2026-11-02', headers=self.a).json()
        self.assertEqual(listing[0]['zoom_meeting_id'], first_meeting)
        self.assertEqual(len(self.zoom.posts), 2)

    def test_oauth_state_expiry_auth_and_no_store(self):
        self.assertEqual(self.client.post('/zoom/connect').status_code, 401)
        self.assertEqual(self.client.get('/zoom/connection').status_code, 401)
        self.assertEqual(self.client.delete('/zoom/connection').status_code, 401)
        from urllib.parse import parse_qs, urlparse
        result = self.client.post('/zoom/connect', headers=self.a)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        state = parse_qs(urlparse(result.json()['url']).query)['state'][0]
        with self.sessions() as db:
            record = db.query(ZoomOAuthState).one()
            record.expires_at = now() - dt.timedelta(seconds=1)
            db.commit()
        result = self.client.get('/zoom/callback', params={'state': state, 'code': 'sensitive-code'})
        self.assertIn('invalid_state', result.text)
        self.assertNotIn('sensitive-code', result.text)
        with self.sessions() as db:
            self.assertEqual(db.query(ZoomConnection).count(), 0)

    def test_callback_access_logs_redact_authorization_code(self):
        import logging
        from main import RedactOAuthQuery
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s - "%s %s HTTP/%s" %s',
                                   ('127.0.0.1', 'GET', '/zoom/callback?code=private-code&state=private-state', '1.1', 200), None)
        RedactOAuthQuery().filter(record)
        self.assertNotIn('private-code', record.getMessage())
        self.assertNotIn('private-state', record.getMessage())

    def test_migration_is_additive_and_idempotent(self):
        from database import migrate_schema
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine('sqlite:///' + directory + '/old.db')
            with engine.begin() as connection:
                connection.execute(text('CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT, start_utc DATETIME, end_utc DATETIME, timezone TEXT, is_recurring BOOLEAN, created_at DATETIME, updated_at DATETIME)'))
                connection.execute(text("INSERT INTO events (id, title) VALUES (1, 'old')"))
            with patch('database.engine', engine):
                migrate_schema()
                migrate_schema()
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SELECT id, title, organizer_id FROM events')).one(), (1, 'old', None))
                self.assertEqual(connection.execute(text('SELECT count(*) FROM event_meetings')).scalar(), 0)
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
