"""Microsoft authorization-code login via an owner-only Telegram callback."""
import asyncio
import base64
import hashlib
import os
import re
import secrets
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests
from telethon import events

from onedrive import required


def token_error(response):
    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    codes = payload.get('error_codes', [])
    codes = codes if isinstance(codes, list) else []
    numbers = [str(code) for code in codes if re.fullmatch(r'\d{4,10}', str(code))]
    if not numbers:
        numbers = re.findall(r'AADSTS(\d{4,10})\b', str(payload.get('error_description', '')))
    hints = {
        '7000218': '应用要求客户端密钥。请填写 CLIENTSECRET；若使用无密钥登录，请将此回调注册为“移动和桌面应用”，而非 Web。',
        '7000215': '客户端密钥无效。CLIENTSECRET 应填写密钥 Value，而非 Secret ID。',
        '7000222': '客户端密钥已过期，请生成新密钥并更新 GitHub Secret CLIENTSECRET。',
        '50011': '回调 URI 与微软应用注册不一致，请检查 OAUTH_REDIRECT_URI。',
        '70000': '授权码无效或已过期，请重新运行，使用新链接登录并及时发送完整回调 URI。',
        '700016': '找不到应用，请检查 CLIENTID 和 TENANTID。',
        '65001': '尚未同意委托权限，请完成用户或管理员授权。',
    }
    detail = '、'.join('AADSTS' + number for number in numbers[:5])
    hint = next((hints[number] for number in numbers if number in hints),
                '请检查应用的客户端类型、密钥、回调 URI 和委托权限。')
    return f'OAuth 换取 token 失败（HTTP {response.status_code}' + (f'，{detail}' if detail else '') + f'）。{hint}'


class OAuthFlow:
    def __init__(self):
        self.client_id = required('CLIENTID')
        self.tenant = os.environ.get('TENANTID', '').strip() or 'common'
        self.redirect = os.environ.get('OAUTH_REDIRECT_URI', '').strip() or 'http://localhost'
        uri = urlsplit(self.redirect)
        if uri.scheme != 'http' or uri.hostname != 'localhost' or uri.query or uri.fragment or uri.username:
            raise ValueError('OAUTH_REDIRECT_URI 请填写已注册的 http://localhost 回调地址')
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.scope = 'offline_access https://graph.microsoft.com/Files.ReadWrite.All https://graph.microsoft.com/Sites.Read.All'
        self.base = f'https://login.microsoftonline.com/{quote(self.tenant, safe="")}/oauth2/v2.0'

    def authorize_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        return self.base + '/authorize?' + urlencode({
            'client_id': self.client_id, 'response_type': 'code',
            'redirect_uri': self.redirect, 'response_mode': 'query', 'scope': self.scope,
            'state': self.state, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'prompt': 'select_account'})

    def parse_callback(self, callback):
        uri = urlsplit(callback)
        target = urlsplit(self.redirect)
        if (uri.scheme, uri.netloc, uri.path.rstrip('/')) != (
                target.scheme, target.netloc, target.path.rstrip('/')) or uri.fragment:
            raise ValueError('回调地址不匹配，请复制本次登录跳转后的完整 URI')
        params = parse_qs(uri.query, keep_blank_values=True)
        states = params.get('state', [])
        if len(states) != 1 or not secrets.compare_digest(states[0], self.state):
            raise ValueError('state 校验失败，请使用本次授权链接重新登录')
        if 'error' in params:
            raise ValueError('微软授权未完成或已被拒绝，请重新打开授权链接')
        codes = params.get('code', [])
        if len(codes) != 1 or not codes[0]:
            raise ValueError('URI 缺少授权码 code，请复制完整地址')
        return codes[0]

    def exchange(self, code):
        data = {'client_id': self.client_id, 'grant_type': 'authorization_code',
                'code': code, 'redirect_uri': self.redirect, 'scope': self.scope,
                'code_verifier': self.verifier}
        secret = os.environ.get('CLIENTSECRET', '').strip()
        if secret:
            data['client_secret'] = secret
        try:
            # Codes are single-use: do not blindly retry this request.
            response = requests.post(self.base + '/token', data=data, timeout=(30, 60))
        except requests.RequestException:
            raise RuntimeError('OAuth 换取 token 网络失败，请重新运行并登录') from None
        if response.status_code != 200:
            raise RuntimeError(token_error(response))
        tokens = response.json()
        if not tokens.get('access_token'):
            raise RuntimeError('OAuth 响应缺少 access_token')
        return tokens


async def telegram_login(client, owner):
    flow = OAuthFlow()
    wait = int(os.environ.get('OAUTH_WAIT_SECONDS', '600'))
    if not 30 <= wait <= 1800:
        raise ValueError('OAUTH_WAIT_SECONDS 必须在 30–1800 之间')
    callback = asyncio.get_running_loop().create_future()

    async def receive(event):
        if not event.is_private or event.sender_id != owner or callback.done():
            return
        text = event.raw_text.strip()
        if text.startswith('/oauth '):
            text = text[len('/oauth '):].strip()
        elif not text.startswith('http://localhost'):
            return
        try:
            code = flow.parse_callback(text)
        except ValueError as exc:
            await event.reply(str(exc), parse_mode=None)
            return
        if not callback.done():
            callback.set_result(code)

    builder = events.NewMessage(incoming=True, from_users=owner)
    client.add_event_handler(receive, builder)
    try:
        await client.send_message(owner,
            f'请在 {wait} 秒内打开以下链接，在本地浏览器登录微软账号。\n'
            '登录后会跳转 localhost，即使页面无法打开，也请复制地址栏完整 URI，发送到本私聊。\n'
            '也可以发送：/oauth 完整URI\n\n' + flow.authorize_url(),
            parse_mode=None, link_preview=False)
        try:
            code = await asyncio.wait_for(callback, wait)
        except asyncio.TimeoutError:
            raise RuntimeError('OAuth 登录等待超时') from None
        try:
            tokens = await asyncio.to_thread(flow.exchange, code)
        except RuntimeError as exc:
            await client.send_message(owner, str(exc), parse_mode=None)
            raise
        await client.send_message(owner, '微软授权成功，本次运行将使用该账号转存文件。')
        return tokens['access_token']
    finally:
        client.remove_event_handler(receive, builder)
