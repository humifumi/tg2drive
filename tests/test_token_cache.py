import unittest
from unittest.mock import AsyncMock, Mock, patch
from cryptography.fernet import Fernet
from token_cache import FILENAME, GistTokenCache
from oauth import cached_login, refresh_login


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'GIST_ID': 'abc123', 'GIST_TOKEN': 'github-token',
            'GIST_ENCRYPTION_KEY': Fernet.generate_key().decode(), 'CLIENTID': 'client'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cache = GistTokenCache()

    def test_encrypted_roundtrip_and_missing_cache(self):
        self.cache.request = Mock(return_value={})
        self.assertIsNone(self.cache.load())
        self.cache.save('refresh-secret')
        payload = self.cache.request.call_args.kwargs['json']
        self.assertNotIn('refresh-secret', str(payload))
        self.cache.request.return_value = payload
        self.assertEqual(self.cache.load(), 'refresh-secret')

    def test_invalid_ciphertext_is_not_treated_as_missing_token(self):
        self.cache.request = Mock(return_value={'files': {FILENAME: {'content': '{"encrypted":"bad"}'}}})
        with self.assertRaises(RuntimeError):
            self.cache.load()

    def test_google_cache_is_separate_from_microsoft(self):
        google = GistTokenCache(context={'provider': 'google', 'client_id': 'google-client'},
                                filename='tg2drive-google-token.json')
        google.request = Mock(return_value={})
        google.save('google-refresh')
        payload = google.request.call_args.kwargs['json']
        self.assertNotIn(FILENAME, payload['files'])
        google.request.return_value = payload
        self.assertEqual(google.load(), 'google-refresh')
        self.cache.request = Mock(return_value=payload)
        self.assertIsNone(self.cache.load())

    def test_refresh_revoked_grant_needs_login_but_invalid_client_fails(self):
        response = Mock(status_code=400)
        response.json.return_value = {'error': 'invalid_grant'}
        with patch('oauth.requests.post', return_value=response):
            self.assertIsNone(refresh_login('old'))
        response.status_code = 401
        response.json.return_value = {'error': 'invalid_client', 'error_codes': [7000218]}
        with patch('oauth.requests.post', return_value=response), self.assertRaises(RuntimeError):
            refresh_login('old')


class CacheLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_rotates_refresh_without_interactive_login(self):
        client = Mock(send_message=AsyncMock())
        cache = Mock()
        cache.load.return_value = 'old'
        with patch.dict('os.environ', {'GIST_ID': 'abc'}, clear=True), patch(
                'oauth.GistTokenCache', return_value=cache), patch('oauth.refresh_login',
                return_value={'access_token': 'access', 'refresh_token': 'new'}), patch(
                'oauth.telegram_login', new_callable=AsyncMock) as login:
            self.assertEqual(await cached_login(client, 1), 'access')
        login.assert_not_awaited()
        cache.save.assert_called_once_with('new')

    async def test_cache_miss_prompts_and_persists_new_refresh(self):
        client = Mock(send_message=AsyncMock())
        cache = Mock()
        cache.load.return_value = None
        with patch.dict('os.environ', {'GIST_ID': 'abc'}, clear=True), patch(
                'oauth.GistTokenCache', return_value=cache), patch('oauth.telegram_login',
                new_callable=AsyncMock, return_value={'access_token': 'access', 'refresh_token': 'new'}) as login:
            self.assertEqual(await cached_login(client, 1), 'access')
        login.assert_awaited_once_with(client, 1, return_tokens=True)
        cache.save.assert_called_once_with('new')
