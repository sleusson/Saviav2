import unittest

import app


class WebRouteTests(unittest.TestCase):
    def test_frontend_history_api_and_database_remain_available(self):
        client = app.app.test_client()
        with client.get('/') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'/historico', response.data)
        history = client.get('/historico')
        self.assertEqual(history.status_code, 200)
        self.assertIn(b'/api/lecturas?limit=30', history.data)
        self.assertEqual(client.get('/health').json['database'], 'connected')
        self.assertIsInstance(client.get('/api/lecturas?limit=1').json, list)
