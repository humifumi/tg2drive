"""Google desktop OAuth via owner-only Telegram callback and encrypted Gist."""
import asyncio
import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode, urlsplit

import requests

from oauth import OAuthFlow, telegram_login
from onedrive import required
from token_cache import GistTokenCache

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/drive'


def token_request(data, refresh=False):
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=(30, 60))
    except requests.RequestException:
        raise RuntimeError('Google OAuth 网络请求失败') from None
    if response.status_code != 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if refresh and isinstance(payload, dict) and payload.get('error') == 'invalid_grant':
            return None
        raise RuntimeError(f'Google OAuth 失败（HTTP {response.status_code}），请检查客户端配置或重新授权')
    tokens = response.json()
    if not tokens.get('access_token'):
        raise RuntimeError('Google OAuth 响应缺少 access_token')
    return tokens


class GoogleOAuthFlow(OAuthFlow):
    def __init__(self):
        self.client_id = required('GOOGLE_CLIENT_ID')
        self.redirect = os.environ.get('GOOGLE_REDIRECT_URI', '').strip() or 'http://localhost:8080'
        uri = urlsplit(self.redirect)
        if (uri.scheme != 'http' or uri.hostname != 'localhost' or uri.username
                or uri.password or uri.query or uri.fragment):
            raise ValueError('GOOGLE_REDIRECT_URI 请填写 http://localhost:端口 回调地址')
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)

    def authorize_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect,
            'response_type': 'code', 'scope': SCOPE, 'state': self.state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'access_type': 'offline', 'prompt': 'consent'})

    def parse_callback(self, callback):
        try:
            return super().parse_callback(callback)
        except ValueError as exc:
            raise ValueError(str(exc).replace('微软', 'Google')) from None

    def exchange(self, code):
        return token_request({'client_id': self.client_id,
            'client_secret': required('GOOGLE_CLIENT_SECRET'),
            'grant_type': 'authorization_code', 'code': code,
            'redirect_uri': self.redirect, 'code_verifier': self.verifier})


def refresh_login(refresh_token):
    tokens = token_request({'client_id': required('GOOGLE_CLIENT_ID'),
        'client_secret': required('GOOGLE_CLIENT_SECRET'),
        'grant_type': 'refresh_token', 'refresh_token': refresh_token}, refresh=True)
    if tokens:
        tokens.setdefault('refresh_token', refresh_token)
    return tokens


async def cached_login(client, owner):
    flow = GoogleOAuthFlow()
    required('GOOGLE_CLIENT_SECRET')
    cache = None
    tokens = None
    if os.environ.get('GIST_ID', '').strip():
        cache = GistTokenCache(context={'provider': 'google', 'client_id': flow.client_id},
                               filename='tg2drive-google-token.json')
        refresh = await asyncio.to_thread(cache.load)
        if refresh:
            tokens = await asyncio.to_thread(refresh_login, refresh)
    if tokens is None:
        tokens = await telegram_login(client, owner, return_tokens=True, flow=flow, provider='Google')
    if cache:
        await asyncio.to_thread(cache.save, tokens.get('refresh_token'))
    return tokens
