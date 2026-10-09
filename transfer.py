"""Stream one Telegram attachment to OneDrive in sequential 10 MiB blocks."""
import asyncio
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from onedrive import CHUNK_SIZE, OneDrive, folder_parts, required, sharepoint_configured
from oauth import cached_login
from progress import TransferProgress, configure_logs, format_bytes, logger


def safe_name(name, message_id):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name or f'telegram-{message_id}')
    name = name.strip(' .')
    return name or f'telegram-{message_id}'


async def stream_transfer(client, message, drive, name, folder, on_progress=None):
    size = message.file.size
    if size is None or size < 0:
        raise ValueError('无法获取 Telegram 文件大小')
    if size == 0:
        logger.info('文件为空，直接创建目标文件')
        return await asyncio.to_thread(drive.upload_empty, name, folder)
    logger.info('准备上传会话 | 文件大小 %s | 分片大小 10 MiB', format_bytes(size))
    session = await asyncio.to_thread(drive.start_upload, name, size, folder)
    progress = TransferProgress(size, CHUNK_SIZE)
    # Two block slots cover the producer buffer, queue, and active upload.
    queue = asyncio.Queue(maxsize=1)
    slots = asyncio.Semaphore(2)
    iterator = client.iter_download(message.media, request_size=512 * 1024,
                                    chunk_size=512 * 1024, file_size=size)
    logger.info('生产者消费者已启动 | 最多缓存两片 | OneDrive 单消费者顺序上传')

    async def produce():
        downloaded = 0
        while downloaded < size:
            await slots.acquire()
            buffer = bytearray()
            target = min(CHUNK_SIZE, size - downloaded)
            while len(buffer) < target:
                try:
                    piece = await iterator.__anext__()
                except StopAsyncIteration:
                    raise RuntimeError('Telegram 下载不完整') from None
                if not piece or len(buffer) + len(piece) > target:
                    raise RuntimeError('Telegram 返回无效文件分片')
                buffer.extend(piece)
                downloaded += len(piece)
                progress.download(downloaded)
            await queue.put(buffer)
            # Transfer ownership; never modify an enqueued block.
            del buffer
        await queue.put(None)

    async def consume():
        uploaded = 0
        while True:
            block = await queue.get()
            if block is None:
                break
            try:
                progress.upload_start()
                upload = asyncio.create_task(asyncio.to_thread(session.upload_chunk, block))
                try:
                    await asyncio.shield(upload)
                except asyncio.CancelledError:
                    # Thread requests cannot be cancelled. Drain before cancelling
                    # the upload session, avoiding a race with an in-flight PUT.
                    await asyncio.gather(upload, return_exceptions=True)
                    raise
                uploaded += len(block)
                text = progress.upload_done(uploaded)
            finally:
                del block
                slots.release()
            if on_progress:
                await on_progress(text)
        if uploaded != size or session.item is None:
            raise RuntimeError('OneDrive 未确认上传完成')

    tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in tasks:
            if task in done:
                task.result()
        return session.item
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await iterator.close()
        finally:
            if session.item is None:
                logger.warning('转存未完成，正在取消上传会话')
                await asyncio.to_thread(session.cancel)


async def main():
    load_dotenv(Path(__file__).with_name('.ven.local'), override=False)
    logger.info('任务启动 | 检查配置')
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
        logger.info('正在连接 Telegram 机器人')
        await client.start(bot_token=required('TG_BOT_TOKEN'))
        logger.info('Telegram 已连接 | 开始存储账号授权')
        access_token = await cached_login(client, owner) if auth_mode == 'oauth' else None
        logger.info('等待选择文件 | %s 秒内回复附件 /select', wait)
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
            logger.info('文件已选定 | 正在连接目标文档库')
            drive = await asyncio.to_thread(OneDrive, access_token)
            last_update = time.monotonic()

            async def update_status(text):
                nonlocal last_update
                if time.monotonic() - last_update < 10:
                    return
                try:
                    await status.edit(f'正在转存：{name}\n{text}', parse_mode=None)
                except Exception:
                    logger.warning('Telegram 进度消息更新失败，转存继续；详情请查看日志')
                last_update = time.monotonic()

            item = await stream_transfer(client, message, drive, name, folder, update_status)
            url = item.get('webUrl', '')
            await status.edit(f'转存完成：{item["name"]}\n{url}', parse_mode=None)
            logger.info('任务成功 | 文件已存入目标文档库，链接已发送至 Telegram')
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
    configure_logs()
    try:
        asyncio.run(main())
    except Exception as exc:
        # Telegram exceptions can contain sensitive request details; omit them.
        if isinstance(exc, (ValueError, RuntimeError)):
            logger.error('%s', exc)
        else:
            logger.error('任务失败：%s，请检查网络连接及账号配置', type(exc).__name__)
        raise SystemExit(1)
