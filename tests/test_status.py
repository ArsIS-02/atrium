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


class ServiceFormTests(unittest.TestCase):
    def form(self, **changes):
        data = {
            'name': 'demo', 'title': 'Demo', 'group': 'Monitoring',
            'backend': 'service:8080', 'scheme': 'http', 'host': '',
        }
        data.update(changes)
        return data

    def test_hostdeck_host_is_normalized_and_saved(self):
        svc, err = main.svc_from_form(self.form(host=' Workstation.Example.Lan '))
        self.assertIsNone(err)
        self.assertEqual(svc['host'], 'workstation.example.lan')

    def test_empty_hostdeck_host_is_not_saved(self):
        svc, err = main.svc_from_form(self.form())
        self.assertIsNone(err)
        self.assertNotIn('host', svc)

    def test_invalid_hostdeck_host_is_rejected(self):
        svc, err = main.svc_from_form(self.form(host='host.example/path'))
        self.assertIsNone(svc)
        self.assertIn('Хост HostDeck', err)


class HostDeckStatusTests(unittest.TestCase):
    def test_csv_is_indexed_by_full_and_short_names(self):
        response = Mock(text=(
            'key,name,os,profile,status,index,agent_version,online,expected_offline,maintenance_until,maintenance_reason\n'
            'workstation,Workstation.Example.Lan,linux,laptop,ok,90,1.0.0,0,1,1893456000,Плановое обновление\n'
            'workstation.example.lan,Workstation.Example.Lan,linux,laptop,ok,90,1.0.0,0,1,1893456000,Плановое обновление\n'
        ))
        response.raise_for_status.return_value = None
        with patch.object(main, 'HOSTDECK_URL', 'http://hostdeck:8080'), \
                patch.object(main, 'HOSTDECK_EXPORT_TOKEN', 'secret'), \
                patch('app.main.requests.get', return_value=response) as get:
            states = main.load_hostdeck_states()
        self.assertEqual(states['workstation'], states['workstation.example.lan'])
        self.assertFalse(states['workstation']['online'])
        self.assertTrue(states['workstation']['expected_offline'])
        self.assertEqual(states['workstation']['maintenance_until'], 1893456000)
        self.assertEqual(states['workstation']['maintenance_reason'], 'Плановое обновление')
        get.assert_called_once_with(
            'http://hostdeck:8080/api/export/hosts.csv?key=name',
            headers={'X-HostDeck-Token': 'secret'}, timeout=3,
        )
        response.close.assert_called_once()

    def test_hostdeck_error_falls_back_to_http_only(self):
        with patch.object(main, 'HOSTDECK_URL', 'http://hostdeck:8080'), \
                patch.object(main, 'HOSTDECK_EXPORT_TOKEN', 'secret'), \
                patch('app.main.requests.get', side_effect=main.requests.ConnectionError()):
            self.assertEqual(main.load_hostdeck_states(), {})

    def test_expected_offline_requires_bad_probe_and_offline_host(self):
        svc = {'name': 'demo', 'backend': 'service:8080', 'host': 'workstation.example.lan'}
        cases = [
            ({'code': 0, 'class': 'bad', 'checked': 1},
             {'online': False, 'expected_offline': True}, 'expected_offline'),
            ({'code': 0, 'class': 'bad', 'checked': 1},
             {'online': True, 'expected_offline': True}, 'bad'),
            ({'code': 0, 'class': 'bad', 'checked': 1},
             {'online': False, 'expected_offline': False}, 'bad'),
            ({'code': 200, 'class': 'ok', 'checked': 1},
             {'online': False, 'expected_offline': True}, 'ok'),
            ({'code': 401, 'class': 'warn', 'checked': 1},
             {'online': False, 'expected_offline': True}, 'warn'),
        ]
        for probe_result, state, expected in cases:
            with self.subTest(expected=expected, state=state, code=probe_result['code']), \
                    patch('app.main.probe', return_value=probe_result.copy()):
                result = main.probe_with_hostdeck(svc, {'workstation.example.lan': state})
                self.assertEqual(result['class'], expected)
                self.assertEqual(result['host'], 'workstation.example.lan')

    def test_maintenance_requires_bad_probe_and_takes_priority(self):
        svc = {'name': 'demo', 'backend': 'service:8080', 'host': 'workstation.example.lan'}
        active = {'online': False, 'expected_offline': True,
                  'maintenance_until': main.time.time() + 3600, 'maintenance_reason': 'Обновление'}
        with patch('app.main.probe', return_value={'code': 0, 'class': 'bad', 'checked': 1}):
            result = main.probe_with_hostdeck(svc, {'workstation.example.lan': active})
        self.assertEqual(result['class'], 'maintenance')
        self.assertEqual(result['maintenance_reason'], 'Обновление')
        self.assertGreater(result['maintenance_until'], main.time.time())
        cases = [
            ({'code': 200, 'class': 'ok', 'checked': 1}, active, 'ok'),
            ({'code': 401, 'class': 'warn', 'checked': 1}, active, 'warn'),
            ({'code': 0, 'class': 'bad', 'checked': 1},
             dict(active, maintenance_until=main.time.time() - 1), 'expected_offline'),
        ]
        for probe_result, state, expected in cases:
            with self.subTest(expected=expected), patch('app.main.probe', return_value=probe_result):
                self.assertEqual(main.probe_with_hostdeck(svc, {'workstation.example.lan': state})['class'], expected)

    def test_missing_hostdeck_row_does_not_hide_failure(self):
        svc = {'name': 'demo', 'backend': 'service:8080', 'host': 'missing.example.lan'}
        with patch('app.main.probe', return_value={'code': 0, 'class': 'bad', 'checked': 1}):
            result = main.probe_with_hostdeck(svc, {})
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
