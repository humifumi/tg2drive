"""Encrypted refresh-token storage in a GitHub Gist."""
import json
import os
import re

from cryptography.fernet import Fernet, InvalidToken
import requests

from onedrive import required

FILENAME = 'tg2drive-token.json'


class GistTokenCache:
    def __init__(self, context=None, filename=FILENAME):
        self.filename = filename
        self.gist_id = required('GIST_ID')
        if not re.fullmatch(r'[0-9a-fA-F]+', self.gist_id):
            raise ValueError('GIST_ID 格式无效')
        self.token = required('GIST_TOKEN')
        try:
            self.cipher = Fernet(required('GIST_ENCRYPTION_KEY').encode())
        except (ValueError, TypeError):
            raise ValueError('GIST_ENCRYPTION_KEY 格式无效') from None
        self.context = context if context is not None else {'client_id': required('CLIENTID'),
                        'tenant': os.environ.get('TENANTID', '').strip() or 'common'}

    def request(self, method, **kwargs):
        try:
            response = requests.request(method,
                f'https://api.github.com/gists/{self.gist_id}', timeout=(30, 60),
                headers={'Authorization': f'Bearer {self.token}',
                         'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'}, **kwargs)
        except requests.RequestException:
            raise RuntimeError('Gist token 缓存网络请求失败') from None
        if response.status_code != 200:
            raise RuntimeError(f'Gist token 缓存操作失败（HTTP {response.status_code}）')
        return response.json()

    def load(self):
        item = self.request('GET').get('files', {}).get(self.filename)
        if not item:
            return None
        if item.get('truncated'):
            raise RuntimeError('Gist token 缓存内容被截断')
        try:
            envelope = json.loads(item.get('content', '{}'))
            if not envelope:
                return None
            data = json.loads(self.cipher.decrypt(envelope['encrypted'].encode()))
        except (ValueError, KeyError, TypeError, InvalidToken):
            raise RuntimeError('Gist token 缓存损坏或加密密钥不匹配') from None
        if not isinstance(data, dict) or any(data.get(k) != v for k, v in self.context.items()):
            return None
        token = data.get('refresh_token')
        return token if isinstance(token, str) and token else None

    def save(self, refresh_token):
        if not refresh_token:
            raise RuntimeError('OAuth 未返回 refresh_token，无法保存登录状态')
        data = {**self.context, 'refresh_token': refresh_token}
        encrypted = self.cipher.encrypt(json.dumps(data).encode()).decode()
        self.request('PATCH', json={'files': {self.filename: {
            'content': json.dumps({'version': 1, 'encrypted': encrypted})}}})
