import hashlib
import base64
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import AsyncMock, Mock, patch

from oauth import OAuthFlow, telegram_login, token_error
from onedrive import OneDrive


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'CLIENTID': 'client', 'TENANTID': 'tenant'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.flow = OAuthFlow()

    def callback(self, **params):
        return 'http://localhost?' + urlencode({'code': 'authorization-code',
                                               'state': self.flow.state, **params})

    def test_authorization_url_has_matching_pkce(self):
        params = parse_qs(urlsplit(self.flow.authorize_url()).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(self.flow.verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(params['code_challenge'], [expected])
        self.assertEqual(params['response_type'], ['code'])
        self.assertIn('offline_access', params['scope'][0])

    def test_callback_accepts_code_and_rejects_wrong_state_or_target(self):
        self.assertEqual(self.flow.parse_callback(self.callback()), 'authorization-code')
        for uri in (self.callback(state='wrong'), self.callback().replace('localhost', 'evil.example'),
                    self.callback() + '&code=second', self.callback(error='access_denied')):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                self.flow.parse_callback(uri)

    def test_exchange_posts_code_and_verifier_without_returning_uri_tokens(self):
        response = Mock(status_code=200)
        response.json.return_value = {'access_token': 'access', 'refresh_token': 'refresh'}
        with patch('oauth.requests.post', return_value=response) as post:
            self.assertEqual(self.flow.exchange('code')['access_token'], 'access')
        data = post.call_args.kwargs['data']
        self.assertEqual(data['grant_type'], 'authorization_code')
        self.assertEqual(data['code_verifier'], self.flow.verifier)
        self.assertEqual(data['redirect_uri'], 'http://localhost')

    def test_exchange_error_does_not_expose_response_credentials(self):
        response = Mock(status_code=400, text='secret code refresh_token')
        response.json.return_value = {'error_description': 'secret code refresh_token'}
        with patch('oauth.requests.post', return_value=response), self.assertRaises(RuntimeError) as error:
            self.flow.exchange('code')
        self.assertNotIn('secret', str(error.exception))

    def test_missing_client_secret_is_diagnosed_without_echoing_payload(self):
        response = Mock(status_code=401)
        response.json.return_value = {'error_codes': [7000218],
                                     'error_description': 'sensitive-code-sensitive-token'}
        message = token_error(response)
        self.assertIn('AADSTS7000218', message)
        self.assertIn('CLIENTSECRET', message)
        self.assertNotIn('sensitive', message)

    def test_error_code_fallback_and_non_json_response(self):
        response = Mock(status_code=401)
        response.json.return_value = {'error_description': 'AADSTS7000215: sensitive-value'}
        self.assertIn('AADSTS7000215', token_error(response))
        self.assertNotIn('sensitive-value', token_error(response))
        response.json.side_effect = ValueError('sensitive')
        self.assertIn('HTTP 401', token_error(response))

    def test_existing_access_token_skips_token_exchange(self):
        with patch.object(OneDrive, 'request') as request:
            drive = OneDrive('access')
        request.assert_not_called()
        self.assertEqual(drive.token, 'access')


class TelegramOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_private_callback_and_handler_cleanup(self):
        with patch.dict('os.environ', {'CLIENTID': 'client'}, clear=True):
            client = Mock()
            client.send_message = AsyncMock()
            flow = OAuthFlow()
            flow.exchange = Mock(return_value={'access_token': 'access'})

            async def send(owner, text, **kwargs):
                if 'authorize?' not in text:
                    return
                handler = client.add_event_handler.call_args.args[0]
                uri = 'http://localhost?' + urlencode({'code': 'code', 'state': flow.state})
                for sender, private in ((999, True), (123, False), (123, True)):
                    event = Mock(sender_id=sender, is_private=private, raw_text=uri)
                    event.reply = AsyncMock()
                    await handler(event)

            client.send_message.side_effect = send
            with patch('oauth.OAuthFlow', return_value=flow):
                self.assertEqual(await telegram_login(client, 123), 'access')
            flow.exchange.assert_called_once_with('code')
            client.remove_event_handler.assert_called_once()
