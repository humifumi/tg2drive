import tempfile
import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onedrive import CHUNK_SIZE, GRAPH, OneDrive, folder_parts, site_lookup_url
from transfer import safe_name, stream_transfer


def response(status, payload):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


class TransferTests(unittest.TestCase):
    def test_untrusted_filename_stays_inside_download_directory(self):
        name = safe_name('../../evil\\file.zip', 12)
        self.assertNotIn('/', name)
        self.assertNotIn('\\', name)
        self.assertEqual(safe_name('..', 12), 'telegram-12')

    def test_invalid_remote_folder(self):
        for folder in ('../x', 'a//b', 'a\\b', ''):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                folder_parts(folder)
        self.assertEqual(folder_parts('/Public/中文/'), ['Public', '中文'])

    def test_upload_chunks_without_bearer_and_requires_completion(self):
        drive = OneDrive.__new__(OneDrive)
        drive.ensure_folder = Mock(return_value='parent')
        drive.graph = Mock(return_value={'uploadUrl': 'https://upload.example'})
        drive.request = Mock(side_effect=[
            response(202, {'nextExpectedRanges': [f'{CHUNK_SIZE}-']}),
            response(201, {'name': 'file.bin'})])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file.bin'
            with path.open('wb') as stream:
                stream.truncate(CHUNK_SIZE + 7)
            self.assertEqual(drive.upload(path, 'Public')['name'], 'file.bin')
        calls = drive.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotIn('Authorization', calls[0].kwargs['headers'])
        self.assertEqual(calls[1].kwargs['headers']['Content-Range'],
                         f'bytes {CHUNK_SIZE}-{CHUNK_SIZE + 6}/{CHUNK_SIZE + 7}')

    def test_no_false_success_on_final_accepted_chunk(self):
        drive = OneDrive.__new__(OneDrive)
        drive.ensure_folder = Mock(return_value='parent')
        drive.graph = Mock(return_value={'uploadUrl': 'https://upload.example'})
        drive.request = Mock(return_value=response(202, {'nextExpectedRanges': ['7-']}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file.bin'
            path.write_bytes(b'1234567')
            with self.assertRaises(RuntimeError):
                drive.upload(path, 'Public')

    def test_application_auth_uses_named_user_drive(self):
        env = {'CLIENTID': 'id', 'CLIENTSECRET': 'secret', 'TENANTID': 'tenant',
               'ONEDRIVE_USER_PRINCIPAL_NAME': 'user@example.com'}
        with patch.dict('os.environ', env, clear=True), patch.object(
                OneDrive, 'request', return_value=response(200, {'access_token': 'token'})) as request:
            drive = OneDrive()
        self.assertIn('/users/user%40example.com/drive', drive.drive)
        self.assertEqual(request.call_args.kwargs['data']['grant_type'], 'client_credentials')

    def test_refresh_auth_uses_me_drive(self):
        with patch.dict('os.environ', {'CLIENTID': 'id', 'REFRESH_TOKEN': 'refresh'}, clear=True), patch.object(
                OneDrive, 'request', return_value=response(200, {'access_token': 'token'})):
            self.assertTrue(OneDrive().drive.endswith('/me/drive'))

    def test_sharepoint_root_site_resolves_without_user_upn(self):
        env = {'CLIENTID': 'id', 'CLIENTSECRET': 'secret', 'TENANTID': 'tenant',
               'SHAREPOINT_SITE_URL': 'https://humilr.sharepoint.com'}
        with patch.dict('os.environ', env, clear=True), patch.object(OneDrive, 'request', side_effect=[
                response(200, {'access_token': 'token'}),
                response(200, {'id': 'host,site,web'})]) as request:
            drive = OneDrive()
        self.assertEqual(request.call_args.args, ('GET', f'{GRAPH}/sites/humilr.sharepoint.com'))
        self.assertEqual(drive.drive, f'{GRAPH}/sites/host%2Csite%2Cweb/drive')

    def test_sharepoint_drive_id_has_priority_over_site_url(self):
        env = {'CLIENTID': 'id', 'REFRESH_TOKEN': 'refresh',
               'SHAREPOINT_DRIVE_ID': 'b!library', 'SHAREPOINT_SITE_URL': 'invalid'}
        with patch.dict('os.environ', env, clear=True), patch.object(OneDrive, 'request',
                return_value=response(200, {'access_token': 'token'})) as request:
            drive = OneDrive()
        self.assertEqual(drive.drive, f'{GRAPH}/drives/b%21library')
        self.assertEqual(request.call_count, 1)

    def test_sharepoint_site_id_skips_lookup(self):
        env = {'CLIENTID': 'id', 'CLIENTSECRET': 'secret', 'TENANTID': 'tenant',
               'SHAREPOINT_SITE_ID': 'site-id'}
        with patch.dict('os.environ', env, clear=True), patch.object(OneDrive, 'request',
                return_value=response(200, {'access_token': 'token'})) as request:
            self.assertEqual(OneDrive().drive, f'{GRAPH}/sites/site-id/drive')
        self.assertEqual(request.call_count, 1)

    def test_site_url_requires_site_rather_than_library_link(self):
        self.assertEqual(site_lookup_url('https://tenant.sharepoint.com/sites/中文'),
                         f'{GRAPH}/sites/tenant.sharepoint.com:/sites/%E4%B8%AD%E6%96%87')
        with self.assertRaises(ValueError):
            site_lookup_url('https://humilr-my.sharepoint.com/shared?id=abc')


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def transfer_fixture(self, size, pieces, fail_upload=False):
        trace = []
        drive = OneDrive.__new__(OneDrive)
        drive.ensure_folder = Mock(return_value='parent')
        drive.graph = Mock(return_value={'uploadUrl': 'https://upload.example'})
        uploads = []

        def request(method, url, **kwargs):
            if method == 'DELETE':
                trace.append('cancel')
                return response(204, {})
            trace.append('upload')
            uploads.append(bytes(kwargs['data']))
            if fail_upload:
                return response(403, {})
            end = sum(map(len, uploads))
            if end == size:
                return response(201, {'name': 'file.bin'})
            return response(202, {'nextExpectedRanges': [f'{end}-']})

        drive.request = Mock(side_effect=request)

        class Iterator:
            def __init__(self):
                self.pieces = iter(pieces)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    piece = next(self.pieces)
                except StopIteration:
                    raise StopAsyncIteration
                trace.append('download')
                return piece

            async def close(self):
                trace.append('close')

        client = Mock()
        client.iter_download.return_value = Iterator()
        message = Mock()
        message.file.size = size
        return client, message, drive, trace, uploads

    async def test_pipeline_preserves_file_content_and_upload_order(self):
        pieces = [b'a' * (512 * 1024)] * 20 + [b'tail']
        client, message, drive, trace, uploads = await self.transfer_fixture(CHUNK_SIZE + 4, pieces)
        item = await stream_transfer(client, message, drive, 'file.bin', 'Public')
        self.assertEqual(item['name'], 'file.bin')
        self.assertEqual(trace.count('upload'), 2)
        self.assertEqual(trace[-1], 'close')
        self.assertEqual(list(map(len, uploads)), [CHUNK_SIZE, 4])
        self.assertEqual(b''.join(uploads), b''.join(pieces))

    async def test_exact_block_does_not_need_extra_download(self):
        client, message, drive, trace, uploads = await self.transfer_fixture(
            CHUNK_SIZE, [b'a' * (512 * 1024)] * 20)
        await stream_transfer(client, message, drive, 'file.bin', 'Public')
        self.assertEqual(trace, ['download'] * 20 + ['upload', 'close'])

    async def test_download_overlaps_upload_but_only_two_blocks_are_buffered(self):
        pieces = ([b'a' * (512 * 1024)] * 20 + [b'b' * (512 * 1024)] * 20
                  + [b'c' * (512 * 1024)] * 20)
        client, message, drive, trace, uploads = await self.transfer_fixture(CHUNK_SIZE * 3, pieces)
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = drive.request.side_effect

        def slow_first_upload(method, url, **kwargs):
            if method == 'PUT' and not uploads:
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise RuntimeError('test upload timed out')
            return original(method, url, **kwargs)

        drive.request.side_effect = slow_first_upload
        transfer = asyncio.create_task(stream_transfer(client, message, drive, 'file.bin', 'Public'))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(0)
            self.assertEqual(trace.count('download'), 40)
            self.assertEqual(uploads, [])
        finally:
            release.set()
            await asyncio.wait_for(transfer, 5)
        self.assertEqual(b''.join(uploads), b''.join(pieces))
        self.assertEqual(len(uploads), 3)

    async def test_truncated_download_cancels_session(self):
        client, message, drive, trace, uploads = await self.transfer_fixture(100, [b'short'])
        with self.assertRaisesRegex(RuntimeError, '下载不完整'):
            await stream_transfer(client, message, drive, 'file.bin', 'Public')
        self.assertEqual(uploads, [])
        self.assertEqual(trace[-2:], ['close', 'cancel'])

    async def test_failed_upload_stops_further_downloads(self):
        client, message, drive, trace, uploads = await self.transfer_fixture(
            CHUNK_SIZE + 4, [b'a' * (512 * 1024)] * 20 + [b'tail'], fail_upload=True)
        with self.assertRaisesRegex(RuntimeError, '上传分片失败'):
            await stream_transfer(client, message, drive, 'file.bin', 'Public')
        self.assertEqual(trace.count('upload'), 1)
        self.assertEqual(trace[-2:], ['close', 'cancel'])

    async def test_empty_file_uses_direct_upload(self):
        client = Mock()
        drive = Mock()
        drive.upload_empty.return_value = {'name': 'empty'}
        message = Mock()
        message.file.size = 0
        self.assertEqual(await stream_transfer(client, message, drive, 'empty', 'Public'), {'name': 'empty'})
        client.iter_download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
