import unittest
from pg_store import translate
from cloud_start import nginx_config

class PostgresTranslationTests(unittest.TestCase):
    def test_id_order_does_not_overflow_int32(self):
        self.assertIn('AS BIGINT',translate('SELECT id FROM messages ORDER BY CAST(id AS INTEGER)'))
    def test_upsert_preserves_composite_key(self):
        sql=translate('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)')
        self.assertIn('ON CONFLICT (chat_id,id)',sql)
        self.assertIn('body=EXCLUDED.body',sql)
        self.assertNotIn('id=EXCLUDED.id',sql)
    def test_ignore_is_idempotent(self):
        self.assertTrue(translate('INSERT OR IGNORE INTO settings VALUES (1,?)').endswith('ON CONFLICT DO NOTHING'))
    def test_auto_ids(self):
        self.assertIn('BIGSERIAL PRIMARY KEY',translate('CREATE TABLE tasks (id INTEGER PRIMARY KEY,body TEXT)'))

class CloudGatewayTests(unittest.TestCase):
    def test_browser_has_auth_and_websocket_origin_check(self):
        text=nginx_config(10000,'https://assistant.onrender.com')
        self.assertIn('auth_basic_user_file',text)
        self.assertIn('if ($hh_origin_allowed = 0) { return 403; }',text)
        self.assertIn('"https://assistant.onrender.com" 1;',text)
        self.assertNotIn('PLACEHOLDER',text)
    def test_injected_or_insecure_origin_is_rejected(self):
        for origin in ('http://example.com','https://example.com; arbitrary','https://a.com\nallow all;'):
            with self.assertRaises(ValueError):nginx_config(10000,origin)

if __name__=='__main__':unittest.main()
