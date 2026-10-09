"""Stream one Telegram attachment to OneDrive in sequential 10 MiB blocks."""
import asyncio
import os
import re
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from onedrive import CHUNK_SIZE, OneDrive, folder_parts, required
from oauth import cached_login
from google_oauth import cached_login as google_login
from googledrive import GoogleDrive
from progress import TransferProgress, configure_logs, format_bytes, logger
from buffering import buffer_plan

PROGRESS_INTERVAL = 10


def safe_name(name, message_id):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name or f'telegram-{message_id}')
    name = name.strip(' .')
    return name or f'telegram-{message_id}'


async def stream_transfer(client, message, drive, name, folder, on_progress=None):
    size = message.file.size
    if size is None or size < 0:
        raise ValueError('无法获取 Telegram 文件大小')
    workers = int(os.environ.get('DOWNLOAD_WORKERS', '4'))
    if not 1 <= workers <= 8:
        raise ValueError('DOWNLOAD_WORKERS 必须在 1–8 之间')
    if size == 0:
        logger.info('文件为空，直接创建目标文件')
        return await asyncio.to_thread(drive.upload_empty, name, folder)
    count = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    plan = buffer_plan(workers, count, CHUNK_SIZE)
    logger.info('自动缓冲 | 可用内存 %s | 可用磁盘 %s | %s 路下载 | 最多 %s 片（%s）',
                format_bytes(plan.memory_available), format_bytes(plan.disk_available),
                plan.workers, plan.slots, format_bytes(plan.slots * CHUNK_SIZE))
    logger.info('准备上传会话 | 文件大小 %s | 分片大小 10 MiB', format_bytes(size))
    session = await asyncio.to_thread(drive.start_upload, name, size, folder)
    progress = TransferProgress(size, CHUNK_SIZE)
    workers = plan.workers
    slots = asyncio.Semaphore(plan.slots)
    cache = tempfile.TemporaryDirectory(prefix='tg2drive-buffer-')
    ready = asyncio.Condition()
    blocks = {}
    next_index = 0
    downloaded = 0
    active = 0
    finished = 0
    logger.info('多路下载已启动 | %s 路 | 最多 %s 片在途 | 临时磁盘缓存 | 目标网盘顺序上传', workers, plan.slots)

    async def disk_io(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except OSError:
            raise RuntimeError('临时缓存读写失败，请检查磁盘剩余空间和目录权限') from None
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def produce():
        nonlocal next_index, downloaded, active, finished
        while next_index < count:
            await slots.acquire()
            if next_index >= count:
                slots.release()
                return
            index = next_index
            next_index += 1
            target = min(CHUNK_SIZE, size - index * CHUNK_SIZE)
            iterator = client.iter_download(message.media, offset=index * CHUNK_SIZE,
                limit=(target + 524287) // 524288, request_size=524288,
                chunk_size=524288, file_size=size)
            buffer = bytearray()
            active += 1
            progress.download_state = f'{active} 路下载中'
            try:
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
            finally:
                active -= 1
                await iterator.close()
            path = Path(cache.name) / f'{index}.part'
            await disk_io(path.write_bytes, buffer)
            del buffer
            async with ready:
                blocks[index] = path
                finished += 1
                progress.download_state = ('下载完成' if finished == count else
                    f'{active} 路下载中' if active else '等待缓冲空间')
                ready.notify_all()

    async def consume():
        uploaded = 0
        for index in range(count):
            progress.upload_state = '等待下载分片'
            async with ready:
                await ready.wait_for(lambda: index in blocks)
                path = blocks.pop(index)
            block = None
            try:
                block = await disk_io(path.read_bytes)
                progress.upload_state = '上传中（失败时自动重试）'
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
                progress.upload_done(uploaded)
            finally:
                del block
                await disk_io(lambda: path.unlink(missing_ok=True))
                slots.release()
        if uploaded != size or session.item is None:
            raise RuntimeError('目标网盘未确认上传完成')
        progress.upload_state = '上传完成'

    async def report():
        while True:
            try:
                await on_progress(progress.telegram_text() +
                    f'\n实际下载并发：{workers} 路 · 缓冲上限：{format_bytes(plan.slots * CHUNK_SIZE)}')
            except Exception:
                logger.warning('Telegram 进度消息更新失败，转存继续')
            await asyncio.sleep(PROGRESS_INTERVAL)

    tasks = [asyncio.create_task(produce()) for _ in range(workers)]
    tasks.append(asyncio.create_task(consume()))
    reporter = asyncio.create_task(report()) if on_progress else None
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in tasks:
            if task in done:
                task.result()
        return session.item
    finally:
        if reporter:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        blocks.clear()
        cache.cleanup()
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
    provider = os.environ.get('STORAGE_PROVIDER', 'onedrive').strip().lower()
    if provider not in ('onedrive', 'google'):
        raise ValueError('STORAGE_PROVIDER 必须为 onedrive 或 google')
    required('GOOGLE_CLIENT_ID' if provider == 'google' else 'CLIENTID')
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
        access_token = await (google_login(client, owner) if provider == "google" else cached_login(client, owner))
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
            drive = await asyncio.to_thread(GoogleDrive if provider == "google" else OneDrive, access_token)
            async def update_status(text):
                await status.edit(f'正在转存：{name}\n{text}', parse_mode=None)

            item = await stream_transfer(client, message, drive, name, folder, update_status)
            url = item.get('webUrl', '')
            await status.edit(f'转存完成：{item["name"]}\n{url}', parse_mode=None)
            logger.info('任务成功 | 文件已存入目标文档库，链接已发送至 Telegram')
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            if summary:
                with open(summary, 'a', encoding='utf-8') as stream:
                    stream.write('Telegram 文件已成功转存至目标网盘，详情请查看机器人消息。\n')
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
