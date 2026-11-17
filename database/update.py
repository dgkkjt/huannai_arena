"""下载国服游戏库，校验后原子替换；数据源参考 SonderXiaoming/kanna_note。"""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile

import brotli
import httpx
from loguru import logger
from sqlalchemy.engine import make_url

from ..setting import db_config
from .sqla import pcr_sqla


def database_path() -> Path:
    url = make_url(db_config.pcr_url)
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.database == ":memory:":
        raise ValueError("自动更新需要 pcr_url 指向 SQLite 文件")
    if url.query:
        raise ValueError("自动更新的 pcr_url 不支持查询参数，请使用普通 SQLite 文件路径")
    path = Path(url.database).resolve()
    configured = db_config.pcr_update_path
    if configured and Path(configured).resolve() != path:
        raise ValueError("pcr_update_path 必须与 pcr_url 指向同一文件，或留空")
    if path != Path(pcr_sqla.engine.url.database).resolve():
        raise ValueError("pcr_url 已改变，请重启机器人后更新数据库")
    return path


def validate_database(path: Path) -> dict:
    with path.open("rb") as file:
        if file.read(16) != b"SQLite format 3\x00":
            raise ValueError("下载内容不是 SQLite 数据库")
    # sqlite3 连接的上下文管理器不会关闭连接，显式关闭以便 Windows 换文件。
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite 数据库完整性校验失败")
        # 校验插件实际使用的表、列及关联，防止下载不兼容或空的游戏库。
        rows = connection.execute(
            "SELECT u.unit_id, u.search_area_width, t.talent_id "
            "FROM unit_data AS u JOIN unit_talent AS t ON u.unit_id = t.unit_id"
        ).fetchall()
    if not rows:
        raise ValueError("游戏数据库没有可用的角色和天赋数据")
    return {unit_id: (width, talent) for unit_id, width, talent in rows}


async def download_database(url: str, destination: Path) -> dict:
    decompressor = brotli.Decompressor()
    async with httpx.AsyncClient(
        timeout=db_config.pcr_download_timeout, follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as file:
                async for chunk in response.aiter_bytes(64 * 1024):
                    file.write(decompressor.process(chunk))
    if not decompressor.is_finished():
        raise ValueError("Brotli 数据库压缩流不完整")
    # asyncio.to_thread 在 Python 3.8 中不可用。
    validation = asyncio.get_running_loop().run_in_executor(
        None, validate_database, destination,
    )
    try:
        return await asyncio.shield(validation)
    except asyncio.CancelledError:
        # 等待读取线程关库后再清理临时文件，避免 Windows 文件占用。
        try:
            await validation
        finally:
            raise


async def update_pcr_database(force: bool = False) -> bool:
    if not force and not db_config.pcr_auto_update:
        return False
    async with pcr_sqla.update_lock:
        path = database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
        )
        os.close(descriptor)
        temporary = Path(filename)
        try:
            units = await download_database(db_config.pcr_download_url, temporary)
            await pcr_sqla.replace_database(temporary, units)
            logger.info("国服游戏数据库更新成功：{}，角色数 {}", path, len(units))
            return True
        finally:
            if temporary.exists():
                temporary.unlink()
