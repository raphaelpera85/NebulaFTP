import asyncio
import functools
import inspect
import io
import logging
import math
import os
from asyncio import sleep as asleep
from hashlib import md5
from pathlib import PurePath
from types import SimpleNamespace

import aiohttp
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import raw
from pyrogram.errors import AuthBytesInvalid, FileReferenceExpired, LimitInvalid, OffsetInvalid, RPCError
from pyrogram.file_id import FileId
from pyrogram.methods import Methods
from pyrogram.methods.advanced.save_file import SaveFile
from pyrogram.raw.functions.auth import ExportAuthorization, ImportAuthorization
from pyrogram.raw.functions.upload import GetFile
from pyrogram.raw.types import InputDocumentFileLocation
from pyrogram.session import Auth, Session

logger = logging.getLogger("NebulaFTP")
GETFILE_CHUNK_SIZE = 1024 * 1024
UPLOAD_PART_WORKERS = 1
UPLOAD_PART_TIMEOUT = 180


async def sequential_save_file(
    self,
    path,
    file_id=None,
    file_part=0,
    progress=None,
    progress_args=(),
):
    """Upload bounded Telegram batches and propagate transport failures."""
    async with self.save_file_semaphore:
        if path is None:
            return None

        if isinstance(path, (str, PurePath)):
            fp = open(path, "rb")
            close_file = True
        elif isinstance(path, io.IOBase):
            fp = path
            close_file = False
        else:
            raise ValueError("Invalid file. Expected a path or binary file pointer")

        part_size = 512 * 1024
        fp.seek(0, os.SEEK_END)
        file_size = fp.tell()
        fp.seek(0)
        if file_size == 0:
            if close_file:
                fp.close()
            raise ValueError("File size equals to 0 B")

        file_size_limit_mib = 4000 if self.me.is_premium else 2000
        if file_size > file_size_limit_mib * 1024 * 1024:
            if close_file:
                fp.close()
            raise ValueError(f"Can't upload files bigger than {file_size_limit_mib} MiB")

        file_total_parts = math.ceil(file_size / part_size)
        is_big = file_size > 10 * 1024 * 1024
        is_missing_part = file_id is not None
        file_id = file_id or self.rnd_id()
        md5_sum = md5() if not is_big and not is_missing_part else None
        session = Session(
            self,
            await self.storage.dc_id(),
            await self.storage.auth_key(),
            await self.storage.test_mode(),
            is_media=True,
        )
        try:
            await session.start()
            fp.seek(part_size * file_part)
            workers = 1 if not is_big or is_missing_part else UPLOAD_PART_WORKERS
            while True:
                batch = []
                for _ in range(workers):
                    chunk = fp.read(part_size)
                    if not chunk:
                        break
                    if is_big:
                        request = raw.functions.upload.SaveBigFilePart(
                            file_id=file_id,
                            file_part=file_part,
                            file_total_parts=file_total_parts,
                            bytes=chunk,
                        )
                    else:
                        request = raw.functions.upload.SaveFilePart(
                            file_id=file_id,
                            file_part=file_part,
                            bytes=chunk,
                        )
                    batch.append((file_part, request))
                    if md5_sum is not None:
                        md5_sum.update(chunk)
                    file_part += 1
                if not batch:
                    break

                results = await asyncio.gather(
                    *(
                        session.invoke(request, retries=0, timeout=UPLOAD_PART_TIMEOUT)
                        for _, request in batch
                    )
                )
                rejected = next(
                    (part for (part, _), accepted in zip(batch, results) if not accepted),
                    None,
                )
                if rejected is not None:
                    raise RuntimeError(f"Telegram rejected file part {rejected}")
                if is_missing_part:
                    return None
                if progress:
                    callback = functools.partial(
                        progress,
                        min(file_part * part_size, file_size),
                        file_size,
                        *progress_args,
                    )
                    if inspect.iscoroutinefunction(progress):
                        await callback()
                    else:
                        await self.loop.run_in_executor(self.executor, callback)

            file_name = getattr(fp, "name", "file.bin")
            if is_big:
                return raw.types.InputFileBig(
                    id=file_id,
                    parts=file_total_parts,
                    name=file_name,
                )
            return raw.types.InputFile(
                id=file_id,
                parts=file_total_parts,
                name=file_name,
                md5_checksum=md5_sum.hexdigest(),
            )
        finally:
            await session.stop()
            if close_file:
                fp.close()


def install_reliable_upload():
    SaveFile.save_file = sequential_save_file
    Methods.save_file = sequential_save_file


async def send_document_bot_api(bot, chat_id, data, file_name):
    token = getattr(bot, "_nebula_bot_token", None)
    if not isinstance(token, str) or not token:
        raise ConnectionError("Bot token unavailable for HTTPS upload")

    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("disable_notification", "true")
    form.add_field(
        "document",
        io.BytesIO(data),
        filename=file_name,
        content_type="application/octet-stream",
    )
    timeout = aiohttp.ClientTimeout(total=900, sock_connect=30, sock_read=900)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data=form,
            ) as response:
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise ConnectionError(
            f"Telegram Bot API transport failed: {type(exc).__name__}"
        ) from exc

    if not payload.get("ok"):
        raise ConnectionError(
            "Telegram Bot API rejected upload: "
            f"HTTP {response.status}, code={payload.get('error_code')}, "
            f"description={payload.get('description', 'unknown')}"
        )
    result = payload.get("result") or {}
    document = result.get("document") or {}
    if not document.get("file_id") or not result.get("message_id"):
        raise ConnectionError("Telegram Bot API returned incomplete upload metadata")
    return SimpleNamespace(
        id=result["message_id"],
        document=SimpleNamespace(file_id=document["file_id"]),
    )


def get_file_limit(offset):
    return min(GETFILE_CHUNK_SIZE, GETFILE_CHUNK_SIZE - (offset % GETFILE_CHUNK_SIZE))


class File:
    def __init__(self, id, client, chat_id=None, message_id=None):
        self.client = client
        self.chat_id = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        self.message_id = message_id
        self.reference_refreshed = False
        self._set_id(id)

    def _set_id(self, id):
        self.file_id = id
        self.id = FileId.decode(id)
        self.loc = InputDocumentFileLocation(id=self.id.media_id, access_hash=self.id.access_hash, file_reference=self.id.file_reference, thumb_size=self.id.thumbnail_size)

    async def getChunkAt(self, offset=0, refreshed=False):
        session = await get_media_session(self.client, self.id)
        try:
            return (
                await session.send(
                    GetFile(
                        location=self.loc,
                        offset=offset,
                        limit=get_file_limit(offset),
                        precise=True,
                    )
                )
            ).bytes
        except (TimeoutError, asyncio.TimeoutError):
            await asleep(1)
            return await self.getChunkAt(offset, refreshed)
        except FileReferenceExpired:
            if refreshed or not self.chat_id or not self.message_id:
                return b""
            message = await self.client.get_messages(self.chat_id, self.message_id)
            if not message or not message.document:
                return b""
            self._set_id(message.document.file_id)
            self.reference_refreshed = True
            logger.info(
                "Referencia Telegram renovada no cliente %s para mensagem %s",
                getattr(self.client, "name", "?"),
                self.message_id,
            )
            return await self.getChunkAt(offset, True)
        except (OffsetInvalid, LimitInvalid):
            return b""
        except RPCError as exc:
            logger.warning("Telegram recusou leitura no cliente %s: %s", getattr(self.client, "name", "?"), exc)
            return b""

    async def stream(self, offset=0):
        try:
            aligned_offset = offset - (offset % 4096)
            skip = offset - aligned_offset
            while data := await self.getChunkAt(aligned_offset):
                aligned_offset += len(data)
                if skip:
                    data = data[skip:]
                    skip = 0
                if data:
                    yield data
        except (ConnectionError, TimeoutError, asyncio.TimeoutError, RPCError) as exc:
            logger.debug("tg stream interrupted: %s", exc)

async def get_media_session(client, file_id):
    if media_session := client.media_sessions.get(file_id.dc_id):
        return media_session
    async with client.media_sessions_lock:
        if media_session := client.media_sessions.get(file_id.dc_id):
            return media_session
        if file_id.dc_id != await client.storage.dc_id():
            media_session = Session(client, file_id.dc_id, await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(), await client.storage.test_mode(), is_media=True)
            await media_session.start()

            for _ in range(6):
                exported_auth = await client.invoke(ExportAuthorization(dc_id=file_id.dc_id))
                try:
                    await media_session.invoke(ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes))
                    break
                except AuthBytesInvalid:
                    continue
            else:
                await media_session.stop()
                raise AuthBytesInvalid
        else:
            media_session = Session(client, file_id.dc_id, await client.storage.auth_key(), await client.storage.test_mode(), is_media=True)
            await media_session.start()
        client.media_sessions[file_id.dc_id] = media_session
    return media_session

async def stream_file(parts, bot):
    parts.sort(key=lambda x: x["part_id"])
    parts = [p["tg_file"] for p in parts]
    for part in parts:
        file = File(part, bot)
        async for chunk in file.stream():
            yield chunk
