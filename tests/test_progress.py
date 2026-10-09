import unittest
from unittest.mock import patch
from progress import TransferProgress, format_bytes, format_duration


class ProgressTests(unittest.TestCase):
    def test_readable_units_and_duration(self):
        self.assertEqual(format_bytes(10485760), '10.0 MiB')
        self.assertEqual(format_duration(3661), '01:01:01')

    def test_download_finished_does_not_report_upload_finished(self):
        with patch('progress.time.monotonic', return_value=100):
            progress = TransferProgress(200, 100)
        with patch('progress.time.monotonic', return_value=110):
            progress.download(200)
            text = progress.upload_done(100)
        self.assertIn('50.0%', text)
        self.assertIn('分片 1/2', text)
        self.assertIn('剩余 00:00:10', text)
        self.assertNotIn('100.0%', text)

    def test_rounding_never_shows_100_before_confirmation(self):
        progress = TransferProgress(100000, 100)
        progress.uploaded = 99999
        self.assertIn('99.9%', progress.text('下载中'))
        self.assertIn('100.0%', progress.upload_done(100000))
