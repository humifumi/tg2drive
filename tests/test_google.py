import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit, urlencode

from google_oauth import GoogleOAuthFlow, refresh_login
from googledrive import GoogleUploadSession, GoogleDrive
from onedrive import CHUNK_SIZE


def response(status, headers=None, item=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = item or {}
    return result


class GoogleTests(unittest.TestCase):
    def test_google_oauth_pkce_offline_and_callback_security(self):
        with patch.dict('os.environ', {'GOOGLE_CLIENT_ID': 'google'}, clear=True):
            flow = GoogleOAuthFlow()
        query = parse_qs(urlsplit(flow.authorize_url()).query)
        self.assertEqual(query['access_type'], ['offline'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        uri = flow.redirect + '?' + urlencode({'code': 'code', 'state': flow.state})
        self.assertEqual(flow.parse_callback(uri), 'code')
        with self.assertRaises(ValueError):
            flow.parse_callback(uri.replace('localhost', 'evil.example'))

    def test_google_refresh_preserves_existing_refresh_token(self):
        with patch.dict('os.environ', {'GOOGLE_CLIENT_ID': 'id', 'GOOGLE_CLIENT_SECRET': 'secret'}), \
                patch('google_oauth.token_request', return_value={'access_token': 'new'}):
            self.assertEqual(refresh_login('old')['refresh_token'], 'old')

    def test_sequential_chunks_and_link_normalization(self):
        drive = Mock()
        drive.request.side_effect = [response(308, {'Range': f'bytes=0-{CHUNK_SIZE-1}'}),
                                     response(200, item={'id': 'file', 'name': 'test'})]
        session = GoogleUploadSession(drive, 'url', CHUNK_SIZE + 3)
        session.upload_chunk(b'x' * CHUNK_SIZE)
        self.assertEqual(session.offset, CHUNK_SIZE)
        item = session.upload_chunk(b'end')
        self.assertIn('/file/d/file/view', item['webUrl'])
        self.assertEqual(drive.request.call_args.kwargs['headers']['Content-Range'],
                         f'bytes {CHUNK_SIZE}-{CHUNK_SIZE+2}/{CHUNK_SIZE+3}')

    def test_lost_final_response_is_recovered_by_probe(self):
        drive = Mock()
        drive.request.side_effect = [RuntimeError('network'), response(200, item={'id': 'id', 'name': 'n'})]
        session = GoogleUploadSession(drive, 'url', 3)
        with patch('googledrive.time.sleep'):
            session.upload_chunk(b'end')
        self.assertEqual(session.offset, 3)
        self.assertEqual(drive.request.call_args.kwargs['headers']['Content-Range'], 'bytes */3')

    def test_partial_chunk_recovery_sends_only_missing_bytes(self):
        drive = Mock()
        drive.request.side_effect = [response(503), response(308, {'Range': 'bytes=0-262143'}),
                                     response(200, item={'id': 'id', 'name': 'n'})]
        session = GoogleUploadSession(drive, 'url', 524288)
        with patch('googledrive.time.sleep'):
            session.upload_chunk(b'x' * 524288)
        self.assertEqual(len(drive.request.call_args.kwargs['data']), 262144)

    def test_wrong_acknowledgement_is_rejected(self):
        drive = Mock()
        drive.request.return_value = response(308, {'Range': 'bytes=0-999'})
        session = GoogleUploadSession(drive, 'url', 3)
        with self.assertRaises(RuntimeError):
            session.upload_chunk(b'end')

    def test_empty_file_created_in_target_folder(self):
        drive = GoogleDrive({'access_token': 'token'})
        with patch.object(drive, 'ensure_folder', return_value='folder'), \
                patch.object(drive, 'api', return_value={'id': 'id', 'name': 'empty'}) as api:
            drive.upload_empty('empty', 'target')
        self.assertEqual(api.call_args.kwargs['json']['parents'], ['folder'])

    def test_access_token_expiry_refreshes_before_request(self):
        drive = GoogleDrive({'access_token': 'old', 'refresh_token': 'refresh', 'expires_in': 0})
        with patch('googledrive.refresh_login', return_value={'access_token': 'new', 'refresh_token': 'refresh'}), \
                patch('googledrive.requests.request', return_value=response(200)) as request:
            drive.request('GET', 'https://www.googleapis.com/drive/v3/files')
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer new')
