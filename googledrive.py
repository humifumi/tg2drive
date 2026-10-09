"""Google Drive sequential resumable uploads, including shared drives."""
import os
import re
import time
from urllib.parse import urlsplit

import requests

from google_oauth import refresh_login
from onedrive import CHUNK_SIZE, folder_parts
from progress import logger

API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
FIELDS = 'id,name,webViewLink'
FOLDER = 'application/vnd.google-apps.folder'


class GoogleDrive:
    def __init__(self, tokens):
        self.tokens = tokens
        self.expires = time.monotonic() + int(tokens.get('expires_in', 3600)) - 60
        self.root = os.environ.get('GOOGLE_FOLDER_ID', '').strip() or 'root'

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        for attempt in range(5):
            if time.monotonic() >= self.expires:
                self.refresh()
            headers['Authorization'] = 'Bearer ' + self.tokens['access_token']
            try:
                response = requests.request(method, url, headers=headers,
                    timeout=(30, 300), allow_redirects=False, **kwargs)
            except requests.RequestException:
                # Upload callers probe the server before retrying a PUT.
                raise RuntimeError('Google Drive 网络请求失败') from None
            if response.status_code == 401 and attempt < 4:
                self.refresh()
                continue
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 4:
                return response
            if method == 'PUT':
                return response
            logger.warning('Google Drive 暂不可用，正在重试')
            time.sleep(2 ** attempt)

    def refresh(self):
        token = self.tokens.get('refresh_token')
        tokens = refresh_login(token) if token else None
        if not tokens:
            raise RuntimeError('Google 登录已失效，请重新运行并授权')
        self.tokens = tokens
        self.expires = time.monotonic() + int(tokens.get('expires_in', 3600)) - 60

    def api(self, method, expected=(200,), **kwargs):
        response = self.request(method, API, **kwargs)
        if response.status_code not in expected:
            raise RuntimeError(f'Google Drive 操作失败（HTTP {response.status_code}）')
        return response.json()

    def ensure_folder(self, folder):
        parent = self.root
        for part in folder_parts(folder):
            escape = lambda value: value.replace('\\', '\\\\').replace("'", "\\'")
            params = {'q': f"'{escape(parent)}' in parents and name = '{escape(part)}' and trashed = false and mimeType = '{FOLDER}'",
                      'fields': 'files(id)', 'supportsAllDrives': 'true',
                      'includeItemsFromAllDrives': 'true', 'pageSize': 1}
            items = self.api('GET', params=params).get('files', [])
            if items:
                parent = items[0]['id']
            else:
                parent = self.api('POST', expected=(200, 201),
                    params={'supportsAllDrives': 'true', 'fields': 'id'},
                    json={'name': part, 'mimeType': FOLDER, 'parents': [parent]})['id']
        return parent

    def start_upload(self, name, size, folder):
        parent = self.ensure_folder(folder)
        response = self.request('POST', UPLOAD,
            params={'uploadType': 'resumable', 'supportsAllDrives': 'true', 'fields': FIELDS},
            headers={'X-Upload-Content-Type': 'application/octet-stream',
                     'X-Upload-Content-Length': str(size)},
            json={'name': name, 'parents': [parent]})
        if response.status_code not in (200, 201):
            raise RuntimeError(f'创建 Google 上传会话失败（HTTP {response.status_code}）')
        url = response.headers.get('Location', '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com' or parsed.username:
            raise RuntimeError('Google 返回无效上传地址')
        return GoogleUploadSession(self, url, size)

    def upload_empty(self, name, folder):
        item = self.api('POST', expected=(200, 201),
            params={'supportsAllDrives': 'true', 'fields': FIELDS},
            json={'name': name, 'mimeType': 'application/octet-stream',
                  'parents': [self.ensure_folder(folder)]})
        return normalize(item)


def normalize(item):
    return {**item, 'webUrl': item.get('webViewLink') or
            f'https://drive.google.com/file/d/{item["id"]}/view'}


class GoogleUploadSession:
    def __init__(self, drive, url, size):
        self.drive, self.url, self.size = drive, url, size
        self.offset = 0
        self.item = None

    def upload_chunk(self, chunk):
        end = self.offset + len(chunk)
        if (self.item is not None or not chunk or end > self.size or len(chunk) > CHUNK_SIZE
                or (end < self.size and len(chunk) != CHUNK_SIZE)):
            raise ValueError('Google 上传分片大小或状态无效')
        start = self.offset
        for attempt in range(5):
            try:
                response = self.drive.request('PUT', self.url, data=chunk[start - self.offset:],
                    headers={'Content-Type': 'application/octet-stream',
                             'Content-Range': f'bytes {start}-{end - 1}/{self.size}'})
            except RuntimeError:
                response = None
            if response is None or response.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                response = self.drive.request('PUT', self.url, data=b'',
                    headers={'Content-Range': f'bytes */{self.size}'})
            if response.status_code in (200, 201):
                if end != self.size:
                    raise RuntimeError('Google Drive 提前结束上传')
                self.item = normalize(response.json())
                self.offset = end
                return self.item
            if response.status_code != 308:
                raise RuntimeError(f'Google 分片上传失败（HTTP {response.status_code}）')
            value = response.headers.get('Range', '')
            match = re.fullmatch(r'bytes=0-(\d+)', value)
            if value and not match:
                raise RuntimeError('Google 返回无效上传进度')
            confirmed = int(match.group(1)) + 1 if match else 0
            if not start <= confirmed <= end:
                raise RuntimeError('Google 上传进度不匹配')
            if confirmed == end and end < self.size:
                self.offset = end
                return None
            if confirmed == self.size:
                raise RuntimeError('Google 未返回已完成文件信息')
            start = confirmed
        raise RuntimeError('Google 分片上传重试次数已耗尽')

    def cancel(self):
        if self.item is None:
            try:
                self.drive.request('DELETE', self.url)
            except Exception:
                pass
