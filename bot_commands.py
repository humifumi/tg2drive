"""Register Telegram command suggestions in the owner's private chat."""
import requests

COMMANDS = [
    {'command': 'start', 'description': '查看转存使用说明'},
    {'command': 'select', 'description': '回复附件，选择本次转存文件'},
    {'command': 'oauth', 'description': '发送 /oauth 完整回调URI，完成授权'},
]


def register_commands(token, owner):
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/setMyCommands',
            json={'commands': COMMANDS, 'scope': {'type': 'chat', 'chat_id': owner},
                  'language_code': ''}, timeout=(15, 30))
        if response.status_code != 200 or not response.json().get('ok'):
            raise RuntimeError('Telegram 命令菜单设置失败')
    except (requests.RequestException, ValueError):
        raise RuntimeError('Telegram 命令菜单设置失败，请检查网络和机器人配置') from None
