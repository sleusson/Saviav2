import csv
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import app


class HttpPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.directory.name) / 'lecturas.csv'
        self.path_patch = patch.object(app, 'CSV_PATH', self.csv_path)
        self.token_patch = patch.object(app, 'DEVICE_TOKEN', '')
        self.path_patch.start()
        self.token_patch.start()
        self.sample = dict(boot='http-test-' + uuid.uuid4().hex, seq=1, ts=0,
                           temperature=26.5, humidity=67.0, mode='AUTO', fan=False)
        self.client = app.app.test_client()
        app.init_database()

    def tearDown(self):
        try:
            with app.connect() as conn:
                conn.execute('DELETE FROM lecturas WHERE boot=%s', (self.sample['boot'],))
        finally:
            self.path_patch.stop()
            self.token_patch.stop()
            self.directory.cleanup()

    def test_http_write_read_and_duplicate_in_both_stores(self):
        first = self.client.post('/api/telemetry', json=self.sample)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json, dict(status='ok', inserted=True, database='saved', csv='saved'))
        self.assertEqual(self.client.post('/api/telemetry', json=self.sample).status_code, 200)
        with app.connect() as conn:
            rows = conn.execute('SELECT temperature,humidity,source FROM lecturas WHERE boot=%s',
                                (self.sample['boot'],)).fetchall()
        self.assertEqual(rows, [(26.5, 67.0, 'http_esp32')])
        with self.csv_path.open(newline='') as f:
            exported = list(csv.DictReader(f))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]['boot'], self.sample['boot'])
        self.assertEqual(float(exported[0]['temperature']), 26.5)
        self.assertEqual(float(exported[0]['humidity']), 67.0)
        readings = self.client.get('/api/lecturas?limit=500').json
        self.assertTrue(any(row['boot'] == self.sample['boot'] for row in readings))

    def test_csv_failure_is_not_acknowledged_and_retry_repairs_export(self):
        with patch.object(csv.DictWriter, 'writerow', side_effect=OSError('test CSV unavailable')):
            with self.assertLogs('savia', level='ERROR'):
                response = self.client.post('/api/telemetry', json=self.sample)
        self.assertEqual(response.status_code, 503)
        retry = self.client.post('/api/telemetry', json=self.sample)
        self.assertEqual(retry.status_code, 200)
        self.assertFalse(retry.json['inserted'])
        with self.csv_path.open(newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['boot'], self.sample['boot'])

    def test_http_recovers_csv_after_mqtt_insert(self):
        app.save(self.sample, 'mqtt_telemetry')
        self.csv_path.unlink()
        response = self.client.post('/api/telemetry', json=self.sample)
        self.assertEqual(response.status_code, 200)
        with self.csv_path.open(newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'mqtt_telemetry')

    def test_sensor_failure_preserves_null_readings(self):
        self.sample.update(temperature=None, humidity=None, alarm='SENSOR')
        self.assertEqual(self.client.post('/api/telemetry', json=self.sample).status_code, 201)
        with app.connect() as conn:
            row = conn.execute('SELECT temperature,humidity,alarm FROM lecturas WHERE boot=%s',
                               (self.sample['boot'],)).fetchone()
        self.assertEqual(row, (None, None, 'SENSOR'))
        with self.csv_path.open(newline='') as f:
            exported = next(csv.DictReader(f))
        self.assertEqual(exported['temperature'], '')
        self.assertEqual(exported['humidity'], '')
