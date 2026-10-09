"""OneDrive / SharePoint Graph upload with sequential chunk sessions."""
import os
import time
from urllib.parse import quote, unquote, urlsplit

import requests

GRAPH = 'https://graph.microsoft.com/v1.0'
CHUNK_SIZE = 327680 * 32


def required(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise ValueError(f'缺少环境变量 {name}')
    return value


def folder_parts(folder):
    parts = folder.strip('/').split('/')
    if any(not p or p in ('.', '..') or any(c in p for c in '\\:*?"<>|') for p in parts):
        raise ValueError('目标目录格式无效，请使用 / 分隔目录')
    return parts


def sharepoint_configured():
    return any(os.environ.get(key, '').strip() for key in
               ('SHAREPOINT_DRIVE_ID', 'SHAREPOINT_SITE_ID', 'SHAREPOINT_SITE_URL'))


def site_lookup_url(site_url):
    site = urlsplit(site_url)
    if (site.scheme != 'https' or not site.hostname or site.username or site.password
            or site.port or site.query or site.fragment):
        raise ValueError('SHAREPOINT_SITE_URL 必须为 HTTPS 站点地址，不是分享或文档库链接')
    path = unquote(site.path).rstrip('/')
    if path and not re_site_path(path):
        raise ValueError('SHAREPOINT_SITE_URL 请填写根站点或 /sites/名称、/teams/名称')
    endpoint = f'{GRAPH}/sites/{quote(site.hostname, safe=".")}'
    return endpoint + (':' + quote(path, safe='/') if path else '')


def re_site_path(path):
    parts = path.split('/')
    return len(parts) == 3 and parts[1] in ('sites', 'teams') and parts[2] not in ('', '.', '..')


class OneDrive:
    def __init__(self, access_token=None):
        if access_token is None:
            self.token, self.drive = self.acquire_token()
        else:
            self.token = access_token
            self.drive = f'{GRAPH}/me/drive'
        drive_id = os.environ.get('SHAREPOINT_DRIVE_ID', '').strip()
        site_id = os.environ.get('SHAREPOINT_SITE_ID', '').strip()
        site_url = os.environ.get('SHAREPOINT_SITE_URL', '').strip()
        if drive_id:
            self.drive = f'{GRAPH}/drives/{quote(drive_id, safe="")}'
        elif site_id or site_url:
            if not site_id:
                response = self.request('GET', site_lookup_url(site_url),
                    headers={'Authorization': f'Bearer {self.token}'})
                if response.status_code != 200:
                    raise RuntimeError(f'解析 SharePoint 站点失败（HTTP {response.status_code}）')
                site_id = response.json()['id']
            self.drive = f'{GRAPH}/sites/{quote(site_id, safe="")}/drive'

    def acquire_token(self):
        client_id = required('CLIENTID')
        refresh = os.environ.get('REFRESH_TOKEN', '').strip()
        data = {'client_id': client_id}
        secret = os.environ.get('CLIENTSECRET', '').strip()
        if secret:
            data['client_secret'] = secret
        if refresh:
            tenant = os.environ.get('TENANTID') or 'common'
            data.update(grant_type='refresh_token', refresh_token=refresh)
            self.drive = f'{GRAPH}/me/drive'
        else:
            tenant = required('TENANTID')
            data.update(grant_type='client_credentials',
                        client_secret=required('CLIENTSECRET'),
                        scope='https://graph.microsoft.com/.default')
            if not sharepoint_configured():
                user = quote(required('ONEDRIVE_USER_PRINCIPAL_NAME'), safe='')
                self.drive = f'{GRAPH}/users/{user}/drive'
        response = self.request('POST',
            f'https://login.microsoftonline.com/{quote(tenant, safe="")}/oauth2/v2.0/token',
            data=data)
        if response.status_code != 200:
            raise RuntimeError(f'OneDrive 获取令牌失败（HTTP {response.status_code}），请检查凭据')
        return response.json()['access_token'], getattr(self, 'drive', f'{GRAPH}/me/drive')

    @staticmethod
    def request(method, url, **kwargs):
        # Never log request URLs: upload URLs contain temporary credentials.
        for attempt in range(5):
            try:
                response = requests.request(method, url, timeout=(30, 300), **kwargs)
            except requests.RequestException:
                if attempt == 4:
                    raise RuntimeError('OneDrive 网络请求失败') from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 4:
                return response
            try:
                delay = float(response.headers.get('Retry-After', 2 ** attempt))
            except ValueError:
                delay = 2 ** attempt
            time.sleep(min(max(delay, 0), 120))

    def graph(self, method, suffix, expected=(200,), **kwargs):
        response = self.request(method, self.drive + suffix,
            headers={'Authorization': f'Bearer {self.token}'}, **kwargs)
        if response.status_code not in expected:
            raise RuntimeError(f'OneDrive 操作失败（HTTP {response.status_code}）')
        return response.json()

    def ensure_folder(self, folder):
        parent = self.graph('GET', '/root')['id']
        for part in folder_parts(folder):
            suffix = f'/items/{quote(parent, safe="")}:/{quote(part, safe="")}'
            response = self.request('GET', self.drive + suffix,
                headers={'Authorization': f'Bearer {self.token}'})
            if response.status_code == 404:
                item = self.graph('POST', f'/items/{quote(parent, safe="")}/children',
                    expected=(201,), json={'name': part, 'folder': {},
                    '@microsoft.graph.conflictBehavior': 'fail'})
            elif response.status_code == 200:
                item = response.json()
            else:
                raise RuntimeError(f'读取目标目录失败（HTTP {response.status_code}）')
            if 'folder' not in item:
                raise ValueError(f'目标路径包含同名文件：{part}')
            parent = item['id']
        return parent

    def start_upload(self, name, size, folder):
        if size <= 0:
            raise ValueError('分片上传需要非空文件')
        parent = self.ensure_folder(folder)
        target = f'/items/{quote(parent, safe="")}:/{quote(name, safe="")}:'
        session = self.graph('POST', target + '/createUploadSession',
            json={'item': {'@microsoft.graph.conflictBehavior': 'rename', 'name': name}})
        return UploadSession(self, session['uploadUrl'], size)

    def upload_empty(self, name, folder):
        parent = self.ensure_folder(folder)
        target = f'/items/{quote(parent, safe="")}:/{quote(name, safe="")}:'
        return self.graph('PUT', target + '/content', expected=(200, 201), data=b'')

    def upload(self, path, folder):
        size = path.stat().st_size
        if size == 0:
            return self.upload_empty(path.name, folder)
        session = self.start_upload(path.name, size, folder)
        with path.open('rb') as stream:
            while session.item is None:
                stream.seek(session.offset)
                session.upload_chunk(stream.read(CHUNK_SIZE))
        return session.item


class UploadSession:
    def __init__(self, drive, url, size):
        self.drive = drive
        self.url = url
        self.size = size
        self.offset = 0
        self.item = None

    def upload_chunk(self, chunk):
        end = self.offset + len(chunk)
        if self.item is not None or not chunk or end > self.size:
            raise ValueError('上传分片大小或状态无效')
        if len(chunk) > CHUNK_SIZE or (end < self.size and len(chunk) != CHUNK_SIZE):
            raise ValueError('非末尾分片必须为 10 MiB')
        response = self.drive.request('PUT', self.url, data=chunk, headers={
            'Content-Length': str(len(chunk)),
            'Content-Range': f'bytes {self.offset}-{end - 1}/{self.size}'})
        if response.status_code in (200, 201):
            if end != self.size:
                raise RuntimeError('OneDrive 提前结束上传')
            self.item = response.json()
        else:
            if response.status_code == 416:
                response = self.drive.request('GET', self.url)
                if response.status_code != 200:
                    raise RuntimeError('无法恢复 OneDrive 上传进度')
            elif response.status_code != 202:
                raise RuntimeError(f'上传分片失败（HTTP {response.status_code}）')
            next_offset = int(response.json()['nextExpectedRanges'][0].split('-')[0])
            if next_offset != end or end >= self.size:
                raise RuntimeError('OneDrive 未确认当前分片或上传完成')
        self.offset = end
        print(f'OneDrive 已转存 {end}/{self.size} bytes ({end / self.size:.1%})', flush=True)
        return self.item

    def cancel(self):
        if self.item is None:
            try:
                self.drive.request('DELETE', self.url)
            except Exception:
                pass  # Preserve the original transfer error; session will expire.
