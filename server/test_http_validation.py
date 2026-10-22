import unittest
from unittest.mock import patch

import app


class HttpValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_invalid_payloads_do_not_write(self):
        with patch.object(app, 'connect') as connect, patch.object(app, 'DEVICE_TOKEN', ''):
            for sample in ([], {}, {'boot': 'test', 'seq': True},
                           {'boot': 'test', 'seq': 2**63},
                           {'boot': 'test', 'seq': 1, 'temperature': 10**400},
                           {'boot': 'test', 'seq': 1, 'humidity': 101}):
                self.assertEqual(self.client.post('/api/telemetry', json=sample).status_code, 400)
            connect.assert_not_called()
            self.assertEqual(self.client.post('/api/telemetry', data='text').status_code, 415)
            self.assertEqual(self.client.post('/api/telemetry', data='{',
                                             content_type='application/json').status_code, 400)
            self.assertEqual(self.client.post('/api/telemetry', data=' ' * 17000,
                                             content_type='application/json').status_code, 413)

    def test_optional_token_is_enforced_when_configured(self):
        with patch.object(app, 'DEVICE_TOKEN', 'test-only-token'), patch.object(app, 'save') as save:
            for headers in ({}, {'X-Device-Token': 'wrong'}):
                self.assertEqual(self.client.post('/api/telemetry', json={}, headers=headers).status_code, 401)
            save.assert_not_called()
            save.return_value = True
            response = self.client.post('/api/telemetry', json={'boot': 'test', 'seq': 1},
                                        headers={'X-Device-Token': 'test-only-token'})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json['csv'], 'saved')

    def test_database_failure_does_not_acknowledge_the_reading(self):
        with patch.object(app, 'DEVICE_TOKEN', ''), patch.object(
                app, 'save', side_effect=app.psycopg.OperationalError('test database unavailable')):
            with self.assertLogs('savia', level='ERROR'):
                response = self.client.post('/api/telemetry', json={'boot': 'test', 'seq': 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['status'], 'error')
