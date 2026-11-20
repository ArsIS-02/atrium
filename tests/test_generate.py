import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import generate


class GenerateTests(unittest.TestCase):
    def test_unproxied_service_keeps_catalog_entry_without_caddy_route(self):
        config = {
            'services': [
                {
                    'name': 'internal', 'title': 'Internal', 'backend': 'service:8080',
                    'href': 'https://service.example.lan/', 'proxy': False,
                },
                {
                    'name': 'proxied', 'title': 'Proxied', 'backend': 'other:8080',
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'services.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
            with patch.object(generate, 'ROOT', root), \
                    patch.object(generate, 'OUT', root / 'generated'), \
                    patch.object(generate, 'ZONE', 'apps.example.lan'):
                self.assertEqual(generate.main(), 0)
            caddy = (root / 'generated' / 'Caddyfile.apps').read_text(encoding='utf-8')
            homepage = yaml.safe_load((root / 'generated' / 'homepage-services.yaml').read_text(encoding='utf-8'))
        self.assertNotIn('internal.apps.example.lan', caddy)
        self.assertIn('proxied.apps.example.lan', caddy)
        entries = homepage[0]['Other']
        self.assertEqual(entries[0]['Internal']['href'], 'https://service.example.lan/')


if __name__ == '__main__':
    unittest.main()
