"""Readable logs and confirmed transfer progress, without credential output."""
import logging
import sys
import time

logger = logging.getLogger('tg2drive')


def configure_logs():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
        format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
    logging.getLogger('telethon').setLevel(logging.WARNING)


def format_bytes(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


class TransferProgress:
    def __init__(self, total, chunk_size):
        self.total = total
        self.chunk_size = chunk_size
        self.started = time.monotonic()
        self.last_log = self.started - 5
        self.uploaded = 0
        self.downloaded = 0

    def text(self, stage):
        elapsed = max(time.monotonic() - self.started, 0.001)
        rate = self.uploaded / elapsed
        eta = format_duration((self.total - self.uploaded) / rate) if rate else '估算中'
        percent = self.uploaded / self.total * 100 if self.total else 100
        # Only a committed upload can show 100%, even after rounding.
        shown = f'{min(percent, 99.9):.1f}%' if self.uploaded < self.total else '100.0%'
        count = (self.total + self.chunk_size - 1) // self.chunk_size
        current = min(self.uploaded // self.chunk_size + 1, count)
        if stage in ('分片上传成功', '转存完成'):
            current = (self.uploaded + self.chunk_size - 1) // self.chunk_size
        return (f'{stage} | 转存 {shown} | 已上传 {format_bytes(self.uploaded)}/{format_bytes(self.total)}'
                f' | 已下载 {format_bytes(self.downloaded)} | 分片 {current}/{count}'
                f' | 平均 {format_bytes(rate)}/s | 已用 {format_duration(elapsed)} | 剩余 {eta}')

    def download(self, current):
        self.downloaded = current
        now = time.monotonic()
        if now - self.last_log >= 5:
            logger.info(self.text('Telegram 下载中'))
            self.last_log = now

    def upload_start(self):
        logger.info(self.text('上传当前分片'))

    def upload_done(self, current):
        self.uploaded = current
        text = self.text('转存完成' if current == self.total else '分片上传成功')
        logger.info(text)
        self.last_log = time.monotonic()
        return text
