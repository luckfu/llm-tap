import json
import logging
import asyncio
import aiosqlite
import traceback
from logging.handlers import QueueHandler, QueueListener
from queue import Queue
from typing import Optional

# 配置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.NullHandler())

# 异步日志类
class AsyncLogger:
    def __init__(self, name: str, log_file: str, level=logging.DEBUG):
        self.queue = Queue()
        self.logger = logging.getLogger(name)
        
        # 清除所有现有的处理器，防止重复日志
        if self.logger.handlers:
            for handler in self.logger.handlers[:]:  # 使用副本进行迭代
                self.logger.removeHandler(handler)
        
        self.logger.setLevel(level)
        self.logger.propagate = False  # 防止日志传播到根记录器
        
        # 创建文件处理器和控制台处理器
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        console_handler = logging.StreamHandler()
        
        # 设置日志格式
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # 设置队列处理器
        queue_handler = QueueHandler(self.queue)
        self.logger.addHandler(queue_handler)
        
        # 创建队列监听器
        self.listener = QueueListener(
            self.queue,
            file_handler,
            console_handler,
            respect_handler_level=True
        )
        self.listener.start()
    
    def __del__(self):
        self.listener.stop()
    
    async def debug(self, msg: str):
        self.logger.debug(msg)
    
    async def info(self, msg: str):
        self.logger.info(msg)
    
    async def warning(self, msg: str):
        self.logger.warning(msg)
    
    async def error(self, msg: str):
        self.logger.error(msg)

# 全局异步日志实例
_async_logger: Optional[AsyncLogger] = None

# 初始化异步日志
def init_async_logger(name: str, log_file: str, level=logging.DEBUG) -> AsyncLogger:
    """初始化并返回异步日志实例"""
    global _async_logger
    # 如果已存在实例，先尝试清理资源
    if _async_logger is not None:
        try:
            _async_logger.listener.stop()
        except Exception as e:
            logger.warning(f"停止现有日志监听器时出错: {e}")
    
    # 确保根日志配置不会干扰我们的日志器
    root_logger = logging.getLogger()
    root_level = root_logger.level
    
    # 创建新的实例
    _async_logger = AsyncLogger(name, log_file, level)
    
    # 恢复根日志器的级别
    root_logger.setLevel(root_level)
    
    return _async_logger

# 获取异步日志实例
def get_async_logger() -> Optional[AsyncLogger]:
    """获取全局异步日志实例"""
    return _async_logger

# 数据库路径全局变量
_db_path: str = "interactions.db"

# 初始化数据库路径
async def init_db_path(db_path: str = "interactions.db") -> str:
    """初始化数据库路径"""
    global _db_path
    _db_path = db_path
    # 测试连接并确保表存在
    conn = None
    try:
        conn = await aiosqlite.connect(db_path)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY,
                model TEXT,
                conversation TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await conn.commit()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.error(f"初始化数据库时出错: {e}\n{traceback.format_exc()}")
        raise
    finally:
        if conn:
            await conn.close()
    return _db_path

# 简化版：直接创建数据库连接
async def get_db_connection() -> aiosqlite.Connection:
    """创建并返回一个新的数据库连接"""
    global _db_path
    try:
        conn = await aiosqlite.connect(_db_path)
        return conn
    except Exception as e:
        logger.error(f"创建数据库连接时出错: {e}\n{traceback.format_exc()}")
        raise

def format_to_sharegpt(model: str, messages: dict, response: str) -> dict:
    """将对话格式化为目标格式"""
     # 获取异步日志器实例
    async_logger = get_async_logger()
    if async_logger:
        asyncio.create_task(async_logger.debug(f"format_to_sharegpt 消息: {messages}"))
        asyncio.create_task(async_logger.debug(f"format_to_sharegpt 响应内容: {response}"))
    # 从 messages 字典中提取实际的对话消息列表
    actual_messages = messages.get('messages', [])
    conversations = []
    
    # 遍历对话消息
    for msg in actual_messages:
        if not isinstance(msg, dict):
            continue  # 跳过非字典消息
        
        role = msg.get("role")
        if role == "user":
            content = msg.get("content", "")
            if content:
                # Ensure content is a string before stripping
                if not isinstance(content, str):
                    content_str = json.dumps(content, ensure_ascii=False)
                else:
                    content_str = content
                
                conversations.append({
                    "from": "human",
                    "value": content_str.strip()
                })
        elif role == "assistant":
            # 先处理 assistant 的文本内容（如果有）
            content = msg.get("content")
            if content:
                 # Ensure content is a string before stripping
                if not isinstance(content, str):
                    content_str = json.dumps(content, ensure_ascii=False)
                else:
                    content_str = content
                conversations.append({
                    "from": "gpt",
                    "value": content_str.strip()
                })
            # 再处理 tool_calls（如果有）
            if "tool_calls" in msg:
                for tool_call in msg["tool_calls"]:
                    # 提取 function details
                    function_details = tool_call.get("function", {})
                    function_name = function_details.get("name", "")
                    function_args = function_details.get("arguments", "{}") # Default to empty JSON string
                    
                    # Ensure arguments are valid JSON string before parsing
                    try:
                        # Attempt to parse arguments to ensure they are valid JSON
                        json.loads(function_args)
                    except json.JSONDecodeError:
                        # If arguments are not valid JSON, represent them as a string
                        function_args_str = str(function_args)
                        # Log a warning or handle as appropriate
                        # For now, we'll just use the string representation
                        pass # Keep function_args as is if already string
                    except TypeError:
                         # Handle cases where arguments might not be string-like (e.g., None)
                        function_args = json.dumps(function_args) # Convert to JSON string

                    conversations.append({
                        "from": "function_call",
                        "value": json.dumps({
                            "id": tool_call.get("id", ""), # Include tool_call_id
                            "function": {
                                "name": function_name,
                                "arguments": function_args
                            },
                            "type": tool_call.get("type", "function") # Include type if available
                        }, ensure_ascii=False)
                    })
        elif role == "tool":
            content = msg.get("content", "")
            if content:
                # Check if content is a list, convert to string if necessary
                if isinstance(content, list):
                    content_str = json.dumps(content, ensure_ascii=False)
                else:
                    # Ensure content is treated as a string
                    content_str = str(content)
                # Ensure content is a string before stripping
                if not isinstance(content_str, str):
                    # If it became a list/dict dump again, just use the string representation
                    content_str = str(content)
                    
                conversations.append({
                    "from": "observation",
                    # Include tool_call_id if available in the original message
                    "tool_call_id": msg.get("tool_call_id", ""), 
                    "value": content_str.strip()
                })
    
    # 添加助手回复（如果有）
    if response:
        conversations.append({
            "from": "gpt",
            "value": response.strip()
        })

    # 从messages中提取system消息
    system_message = ""
    for msg in actual_messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_message = msg.get("content", "")
            break
    
    result = {
        "conversations": conversations,
        "system": system_message
    }
    
    # 只有当 messages 中包含 tools 字段时才添加到返回值中
    if 'tools' in messages:
        result['tools'] = messages['tools']
    
    return result


def save_conversation(conn, response_id: str, model: str, conversation: dict):
    """保存对话数据到数据库（同步版本）"""
    try:
        with conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO interactions (id, model, conversation)
                VALUES (?, ?, ?)""",
                (response_id, model, json.dumps(conversation, ensure_ascii=False))
            )
    except Exception as e:
        logger.error(f"保存对话数据时发生错误: {e}")
        raise

async def save_conversation_async(conn, response_id: str, model: str, conversation: dict):
    """保存对话数据到数据库（异步版本）"""
    try:
        # 检查连接类型，确保使用正确的方法
        if isinstance(conn, aiosqlite.Connection):
            await conn.execute(
                """INSERT INTO interactions (id, model, conversation)
                VALUES (?, ?, ?)""",
                (response_id, model, json.dumps(conversation, ensure_ascii=False))
            )
            await conn.commit()
        else:
            # 如果不是异步连接，记录错误
            logger.error(f"错误的连接类型: {type(conn)}，需要aiosqlite.Connection")
            raise TypeError(f"需要aiosqlite.Connection类型，但收到了{type(conn)}")
    except Exception as e:
        logger.error(f"异步保存对话数据时发生错误: {e}")
        raise