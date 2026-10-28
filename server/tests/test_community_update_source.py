import json
import unittest
from unittest.mock import patch

from utils import auto_update
from utils.config import normalize_chat_base_url


class CommunityUpdateTests(unittest.TestCase):
    def test_only_maintained_release_hosts_are_used(self):
        for preferred in ('gitee', 'github', None):
            urls = auto_update._server_download_urls(auto_update.OWNER, auto_update.REPO, '4.1.8', preferred)
            self.assertTrue(urls)
            self.assertTrue(all('/Q1351998764/zotero-pdf2zh-community/' in url for url in urls))
            self.assertTrue(all('gitee.com' not in url for url in urls))

    def test_unreleased_main_never_creates_an_update(self):
        payload = {'tag_name': 'v4.1.8', 'assets': [{'name': 'server.zip'}]}
        with patch.object(auto_update, '_http_get', return_value=(json.dumps(payload).encode(), 'application/json')) as fetch:
            self.assertEqual(auto_update._latest_version_from_github(), '4.1.8')
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(fetch.call_args.args[0].endswith('/releases/latest'))

    def test_release_without_server_archive_is_rejected(self):
        payload = {'tag_name': 'v4.1.8', 'assets': []}
        with patch.object(auto_update, '_http_get', return_value=(json.dumps(payload).encode(), 'application/json')):
            with self.assertRaisesRegex(RuntimeError, 'server.zip'):
                auto_update._latest_version_from_github()

    def test_full_api_endpoint_normalizes_without_losing_custom_path(self):
        self.assertEqual(normalize_chat_base_url('openailiked', ' https://integrate.api.nvidia.com/v1/chat/completions/ '), 'https://integrate.api.nvidia.com/v1')
        self.assertEqual(normalize_chat_base_url('openai', 'https://example.org/custom/v1/chat/completions'), 'https://example.org/custom/v1')
        self.assertEqual(normalize_chat_base_url('gemini', 'https://example.org/chat/completions'), 'https://example.org/chat/completions')


if __name__ == '__main__':
    unittest.main()
