"""
工具模块：异步日志 + 数据库初始化
"""

import logging
import asyncio
import aiosqlite
import traceback
from logging.handlers import QueueHandler, QueueListener
from queue import Queue
from typing import Optional

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.NullHandler())


# ========== 异步日志 ==========

class AsyncLogger:
    def __init__(self, name: str, log_file: str, level=logging.DEBUG):
        self.queue = Queue()
        self.logger = logging.getLogger(name)

        if self.logger.handlers:
            for handler in self.logger.handlers[:]:
                self.logger.removeHandler(handler)

        self.logger.setLevel(level)
        self.logger.propagate = False

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        console_handler = logging.StreamHandler()

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        queue_handler = QueueHandler(self.queue)
        self.logger.addHandler(queue_handler)

        self.listener = QueueListener(
            self.queue,
            file_handler,
            console_handler,
            respect_handler_level=True
        )
        self.listener.start()

    async def info(self, msg: str):
        self.logger.info(msg)

    async def debug(self, msg: str):
        self.logger.debug(msg)

    async def warning(self, msg: str):
        self.logger.warning(msg)

    async def error(self, msg: str):
        self.logger.error(msg)


_async_logger: Optional[AsyncLogger] = None


def init_async_logger(name: str, log_file: str, level=logging.DEBUG) -> AsyncLogger:
    """初始化并返回异步日志实例"""
    global _async_logger
    if _async_logger is not None:
        try:
            _async_logger.listener.stop()
        except Exception as e:
            logger.warning(f"Error stopping existing log listener: {e}")
    _async_logger = AsyncLogger(name, log_file, level)
    return _async_logger


def get_async_logger() -> Optional[AsyncLogger]:
    """获取全局异步日志实例"""
    return _async_logger


# ========== 数据库初始化 ==========

_db_path: str = "calls.db"


async def init_db_path(db_path: str = "calls.db") -> str:
    """初始化数据库路径，确保数据库可连接"""
    global _db_path
    _db_path = db_path
    conn = None
    try:
        conn = await aiosqlite.connect(db_path)
        await conn.commit()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database init error: {e}\n{traceback.format_exc()}")
        raise
    finally:
        if conn:
            await conn.close()
    return _db_path
