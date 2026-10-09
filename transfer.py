"""Stream one Telegram attachment to OneDrive in sequential 10 MiB blocks."""
import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from onedrive import CHUNK_SIZE, OneDrive, folder_parts, required, sharepoint_configured
from oauth import telegram_login


def safe_name(name, message_id):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name or f'telegram-{message_id}')
    name = name.strip(' .')
    return name or f'telegram-{message_id}'


async def stream_transfer(client, message, drive, name, folder):
    size = message.file.size
    if size is None or size < 0:
        raise ValueError('无法获取 Telegram 文件大小')
    if size == 0:
        return await asyncio.to_thread(drive.upload_empty, name, folder)
    session = await asyncio.to_thread(drive.start_upload, name, size, folder)
    # Telegram requests 512 KiB at a time; only collect one 10 MiB block.
    iterator = client.iter_download(message.media, request_size=512 * 1024,
                                    chunk_size=512 * 1024, file_size=size)
    buffer = bytearray()
    downloaded = 0
    try:
        async for piece in iterator:
            if not piece or downloaded + len(piece) > size:
                raise RuntimeError('Telegram 返回无效文件分片')
            downloaded += len(piece)
            buffer.extend(piece)
            if len(buffer) > CHUNK_SIZE:
                raise RuntimeError('Telegram 分片超过缓冲区大小')
            if len(buffer) == CHUNK_SIZE or downloaded == size:
                print(f'Telegram 已下载 {downloaded}/{size} bytes，上传当前分片', flush=True)
                # Await confirmation before requesting the next Telegram block.
                await asyncio.to_thread(session.upload_chunk, buffer)
                buffer.clear()
                if downloaded == size:
                    break
        if downloaded != size or session.item is None:
            raise RuntimeError('Telegram 下载不完整或 OneDrive 未确认完成')
        return session.item
    finally:
        try:
            await iterator.close()
        finally:
            if session.item is None:
                await asyncio.to_thread(session.cancel)


async def main():
    load_dotenv(Path(__file__).with_name('.ven.local'), override=False)
    owner = int(required('TG_BOT_CREATOR_ID'))
    wait = int(os.environ.get('WAIT_SECONDS', '300'))
    if not 30 <= wait <= 1800:
        raise ValueError('WAIT_SECONDS 必须在 30–1800 之间')
    folder = os.environ.get('TARGET_FOLDER', 'Public/Telegram')
    folder_parts(folder)
    required('CLIENTID')
    auth_mode = os.environ.get('AUTH_MODE', 'oauth').strip().lower()
    if auth_mode not in ('oauth', 'auto'):
        raise ValueError('AUTH_MODE 必须是 oauth 或 auto')
    if auth_mode == 'auto' and not os.environ.get('REFRESH_TOKEN', '').strip():
        for key in ('TENANTID', 'CLIENTSECRET'):
            required(key)
        if not sharepoint_configured():
            required('ONEDRIVE_USER_PRINCIPAL_NAME')
    client = TelegramClient(MemorySession(), int(required('TG_BOT_API_ID')),
                            required('TG_BOT_API_HASH'))
    selected = asyncio.get_running_loop().create_future()

    @client.on(events.NewMessage(incoming=True, from_users=owner,
                                pattern=r'^/select(?:@\w+)?\s*$'))
    async def select(event):
        if not event.is_private or selected.done():
            return
        message = await event.get_reply_message()
        if not message or not message.file:
            await event.reply('请回复文件、视频、音频或图片消息：/select')
            return
        if not selected.done():
            selected.set_result(message)

    try:
        await client.start(bot_token=required('TG_BOT_TOKEN'))
        access_token = await telegram_login(client, owner) if auth_mode == 'oauth' else None
        await client.send_message(owner,
            f'转存已启动。请在 {wait} 秒内发送或转发文件到此私聊，再回复该文件 /select。',
            parse_mode=None)
        try:
            message = await asyncio.wait_for(selected, wait)
        except asyncio.TimeoutError:
            await client.send_message(owner, '等待超时，本次任务已结束。')
            raise RuntimeError('等待选择文件超时') from None
        name = safe_name(message.file.name or f'telegram-{message.id}{message.file.ext or ""}', message.id)
        status = await client.send_message(owner, f'正在分片转存：{name}（每片 10 MiB）', parse_mode=None)
        try:
            drive = await asyncio.to_thread(OneDrive, access_token)
            item = await stream_transfer(client, message, drive, name, folder)
            url = item.get('webUrl', '')
            await status.edit(f'转存完成：{item["name"]}\n{url}', parse_mode=None)
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            if summary:
                with open(summary, 'a', encoding='utf-8') as stream:
                    stream.write('Telegram 文件已成功转存至 OneDrive，详情请查看机器人消息。\n')
        except Exception:
            await client.send_message(owner, '转存失败，请查看 GitHub Actions 日志。')
            raise
    finally:
        await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        # Telegram exceptions can contain sensitive request details; omit them.
        if isinstance(exc, (ValueError, RuntimeError)):
            print(str(exc), flush=True)
        else:
            print(f'任务失败：{type(exc).__name__}', flush=True)
        raise SystemExit(1)
