import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ['STATUS_POLL'] = '0'

from app import main


class ClassifyTests(unittest.TestCase):
    def test_http_status_classes(self):
        expected = {
            200: 'ok', 302: 'ok', 399: 'ok',
            401: 'warn', 403: 'warn',
            0: 'bad', 404: 'bad', 500: 'bad',
        }
        for code, cls in expected.items():
            with self.subTest(code=code):
                self.assertEqual(main.classify(code), cls)


class ProbeTests(unittest.TestCase):
    @patch('app.main.requests.get')
    def test_probe_uses_backend_path_and_closes_response(self, get):
        response = Mock(status_code=204)
        get.return_value = response
        result = main.probe({'backend': 'service:8080', 'scheme': 'http', 'path': '/health'})
        self.assertEqual(result['class'], 'ok')
        self.assertEqual(result['code'], 204)
        get.assert_called_once_with(
            'http://service:8080/health', verify=True, timeout=3.0,
            allow_redirects=False, stream=True,
        )
        response.close.assert_called_once()

    @patch('app.main.requests.get', side_effect=main.requests.ConnectionError())
    def test_probe_marks_connection_error_unavailable(self, _get):
        result = main.probe({'backend': 'service:8080', 'scheme': 'http'})
        self.assertEqual(result['code'], 0)
        self.assertEqual(result['class'], 'bad')


class StatusPageTests(unittest.TestCase):
    def test_index_has_text_statuses_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'services.yaml').write_text(
                'services:\n'
                '- name: demo\n'
                '  title: Demo\n'
                '  backend: service:8080\n'
                '  scheme: http\n',
                encoding='utf-8',
            )
            with patch.object(main, 'SERVICES_YAML', root / 'services.yaml'), \
                    patch.object(main, 'ADMIN_FILE', root / 'admin.json'), \
                    patch.object(main, 'SETTINGS_FILE', root / 'settings.json'):
                response = main.app.test_client().get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="status-summary-text"', html)
        self.assertIn('data-status-label', html)
        self.assertIn('data-status-state="pending"', html)
        self.assertIn('aria-hidden="true"', html)

    def test_status_api_returns_current_snapshot(self):
        with main._status_lock:
            previous = main._status.copy()
            main._status.clear()
            main._status.update({'demo': {'code': 200, 'class': 'ok', 'checked': 123}})
        try:
            response = main.app.test_client().get('/api/status')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['demo']['class'], 'ok')
        finally:
            with main._status_lock:
                main._status.clear()
                main._status.update(previous)


if __name__ == '__main__':
    unittest.main()
