import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from utils import auto_update
from utils.config import normalize_chat_base_url


class CommunityUpdateTests(unittest.TestCase):
    @staticmethod
    def public_metadata(version='4.1.10'):
        return {'addons': {'pdf2zh-community@q1351998764.github.io': {'updates': [{
            'version': version,
            'update_link': (
                f'https://github.com/{auto_update.OWNER}/{auto_update.REPO}/releases/'
                f'download/v{version}/zotero-pdf2zh-community.xpi'
            ),
        }]}}}

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

    def test_rate_limited_api_uses_public_metadata_and_checks_matching_archive(self):
        limited = urllib.error.HTTPError('https://api.github.com/example', 403, 'rate limit exceeded', {}, None)
        metadata = json.dumps(self.public_metadata()).encode()
        with (
            patch.object(auto_update, '_http_get', side_effect=[limited, (metadata, 'application/json')]) as fetch,
            patch.object(auto_update, '_verify_server_release_asset') as verify,
            contextlib.redirect_stdout(io.StringIO()) as console,
        ):
            self.assertEqual(auto_update.check_for_updates('4.1.9'), ('4.1.9', '4.1.10'))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0],
                         'https://github.com/Q1351998764/zotero-pdf2zh-community/releases/latest/download/update.json')
        verify.assert_called_once_with('4.1.10')
        self.assertIn('公开发布资源', console.getvalue())
        self.assertNotIn('所有更新源都无法', console.getvalue())

    def test_api_html_or_invalid_json_can_use_public_release(self):
        for payload, content_type in ((b'<html>error</html>', 'text/html'), (b'{invalid', 'application/json')):
            with (
                self.subTest(payload=payload),
                patch.object(auto_update, '_http_get', return_value=(payload, content_type)),
                patch.object(auto_update, '_latest_version_from_public_release', return_value='4.1.10') as fallback,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(auto_update._latest_version_from_github(), '4.1.10')
                fallback.assert_called_once_with()

    def test_invalid_public_metadata_never_checks_or_offers_an_archive(self):
        def encode(value):
            return json.dumps(value).encode(), 'application/json'

        wrong_host = self.public_metadata()
        wrong_host['addons']['pdf2zh-community@q1351998764.github.io']['updates'][0]['update_link'] = 'https://example.org/other.xpi'
        wrong_version = self.public_metadata()
        wrong_version['addons']['pdf2zh-community@q1351998764.github.io']['updates'][0]['version'] = '4.1.11'
        multiple = self.public_metadata()
        multiple['addons']['another-addon@example.org'] = {'updates': []}
        cases = [
            (b'<html>login</html>', 'text/html'), (b'{broken', 'application/json'),
            encode([]), encode({'addons': None}), encode(multiple),
            encode(wrong_host), encode(wrong_version), encode(self.public_metadata('4.1.11-beta.1')),
        ]
        for payload in cases:
            with (
                self.subTest(payload=payload),
                patch.object(auto_update, '_http_get', return_value=payload),
                patch.object(auto_update, '_verify_server_release_asset') as verify,
            ):
                with self.assertRaises(RuntimeError):
                    auto_update._latest_version_from_public_release()
                verify.assert_not_called()

    def test_missing_public_archive_does_not_offer_an_update(self):
        missing = urllib.error.HTTPError('https://github.com/example/server.zip', 404, 'Not Found', {}, None)
        metadata = json.dumps(self.public_metadata()).encode()
        with (
            patch.object(auto_update, '_http_get', return_value=(metadata, 'application/json')),
            patch.object(auto_update, '_verify_server_release_asset', side_effect=missing),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                auto_update._latest_version_from_public_release()

    def test_archive_availability_uses_head_without_reading_body(self):
        response = MagicMock()
        response.status = 200
        response.headers = {'Content-Type': 'application/octet-stream', 'Content-Length': '12345'}
        response.__enter__.return_value = response
        with patch.object(auto_update.urllib.request, 'urlopen', return_value=response) as open_url:
            auto_update._verify_server_release_asset('4.1.10')
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), 'HEAD')
        self.assertEqual(request.full_url,
                         'https://github.com/Q1351998764/zotero-pdf2zh-community/releases/download/v4.1.10/server.zip')
        response.read.assert_not_called()

    def test_html_or_empty_archive_is_rejected(self):
        for headers in ({'Content-Type': 'text/html'}, {'Content-Length': '0'}):
            response = MagicMock()
            response.status = 200
            response.headers = headers
            response.__enter__.return_value = response
            with (
                self.subTest(headers=headers),
                patch.object(auto_update.urllib.request, 'urlopen', return_value=response),
                self.assertRaisesRegex(RuntimeError, 'server.zip'),
            ):
                auto_update._verify_server_release_asset('4.1.10')

    def test_full_api_endpoint_normalizes_without_losing_custom_path(self):
        self.assertEqual(normalize_chat_base_url('openailiked', ' https://integrate.api.nvidia.com/v1/chat/completions/ '), 'https://integrate.api.nvidia.com/v1')
        self.assertEqual(normalize_chat_base_url('openai', 'https://example.org/custom/v1/chat/completions'), 'https://example.org/custom/v1')
        self.assertEqual(normalize_chat_base_url('gemini', 'https://example.org/chat/completions'), 'https://example.org/chat/completions')


if __name__ == '__main__':
    unittest.main()
