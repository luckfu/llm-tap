import asyncio
import json
import logging
import traceback
import argparse
import aiohttp
from aiohttp import web
from typing import Dict, Any, Optional
from utils import format_to_sharegpt, init_async_logger, get_async_logger, init_db_path, get_db_connection, save_conversation_async

# ========== 命令行参数解析 ==========
def parse_args():
    parser = argparse.ArgumentParser(description="LLM代理服务器")
    parser.add_argument("-p", "--port", type=int, default=8080, help="服务器监听端口（默认：8080）")
    parser.add_argument("-c", "--config", type=str, default="config.json", help="配置文件路径（默认：config.json）")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="日志级别")
    return parser.parse_args()

# 解析命令行参数
args = parse_args()

# 只设置基本日志级别，不添加处理器，避免重复日志
logging.basicConfig(level=getattr(logging, args.log_level.upper()))
logger = logging.getLogger(__name__)
# 移除所有处理器，防止重复日志
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# ========== 配置加载 ==========
def load_config(config_path: str) -> Dict[str, Any]:
    """加载配置文件"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ 加载配置文件失败: {e}\n{traceback.format_exc()}")
        return {}

# ========== 主要处理逻辑 ==========
class ProxyServer:
    def __init__(self, config_path: str, port: int = 8080):
        self.config = load_config(config_path)
        self.app = web.Application()
        self.port = port
        self.setup_routes()
        
        # 在应用启动时添加异步初始化函数
        self.app.on_startup.append(self.init_async_resources)
    
    def setup_routes(self):
        self.app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
    
    async def init_async_resources(self, app):
        """初始化异步资源（日志和数据库）"""
        # 初始化异步日志
        await asyncio.to_thread(init_async_logger, "proxy_oneapi", "proxy_oneapi.log", getattr(logging, args.log_level.upper()))
        self.async_logger = get_async_logger()
        await self.async_logger.info("✅ 异步日志初始化完成")
        
        # 初始化数据库路径
        await init_db_path("interactions.db")
        await self.async_logger.info("✅ 数据库初始化完成")
    
    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        await self.async_logger.info(f"🚀 代理服务器已启动，监听端口 {self.port}")

    async def handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        # 验证认证信息
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return web.Response(status=401, text=json.dumps({"error": "未提供有效的认证信息"}))
        
        auth_token = auth_header.split(" ")[1]
        if not auth_token.startswith("sk-") or auth_token not in self.config.get("proxy_config", {}).get("auth_tokens", []):
            return web.Response(status=401, text=json.dumps({"error": "无效的认证令牌"}))
        
        try:
            # 解析请求数据
            request_data = await request.json()
            model_id = request_data.get("model")
            if not model_id or model_id not in self.config.get("models", {}):
                return web.Response(status=400, text=json.dumps({"error": "无效的模型ID"}))
            
            # 获取模型配置
            model_config = self.config["models"][model_id]
            
            # 准备转发请求
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {model_config['key']}"
            }
            
            # 修改请求数据中的模型名称
            request_data["model"] = model_config["model"]
            is_stream = request_data.get("stream", False)
            
            await self.async_logger.info(f"📝 处理模型请求: {model_id} -> {model_config['model']}, 流式请求: {is_stream}")
            
            # 记录请求开始时间
            start_time = asyncio.get_event_loop().time()
            
            # 创建异步HTTP客户端会话
            async with aiohttp.ClientSession() as session:
                # 设置更长的超时时间，包括连接和读取超时
                timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_connect=30, sock_read=600)
                async with session.post(
                    model_config["end_point"],
                    headers=headers,
                    json=request_data,
                    timeout=timeout
                ) as resp:
                    # 记录请求耗时
                    elapsed_time = asyncio.get_event_loop().time() - start_time
                    await self.async_logger.info(f"请求耗时: {elapsed_time:.2f}秒")
                    
                    if resp.status != 200:
                        error_text = await resp.text()
                        await self.async_logger.error(f"❌ 模型服务器错误: {error_text}")
                        return web.Response(
                            status=resp.status,
                            text=json.dumps({"error": f"模型服务器错误: {error_text}"})
                        )
                    
                    await self.async_logger.info("✅ 成功接收到目标服务器响应")
                    
                    if is_stream:
                        # 处理流式响应
                        response = web.StreamResponse(
                            status=200,
                            headers={
                                "Content-Type": "text/event-stream",
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive"
                            }
                        )
                        await response.prepare(request)
                        await self.async_logger.info("🌊 开始处理流式响应")
                        
                        complete_response = ""
                        complete_reasoning = ""
                        has_reasoning = False
                        response_id = None
                        saved_to_db = False
                        
                        try:
                            async for line in resp.content:
                                try:
                                    # 检查客户端连接状态
                                    if request.transport is None or request.transport.is_closing():
                                        await self.async_logger.warning("客户端连接已关闭，准备保存数据并终止响应")
                                        break
                                    
                                    line_str = line.decode("utf-8")
                                    #await self.async_logger.debug(f"接收到流式数据: ～{line_str}～")
                                    
                                    # 写入响应前记录日志
                                    await response.write(line)
                                    
                                    if line_str.startswith("data: "):
                                        # 先检查是否为结束标记
                                        if line_str.strip() == "data: [DONE]":
                                            await self.async_logger.info("收到流式响应结束标记")
                                            # 在收到 [DONE] 标记时保存数据
                                            if response_id and not saved_to_db:
                                                try:
                                                    final_response = complete_response
                                                    if has_reasoning and complete_reasoning:
                                                        final_response = f"<think>\n{complete_reasoning}\n</think>\n\n\n{complete_response}"
                                                    
                                                    formatted_conversation = format_to_sharegpt(
                                                        model_config["model"],  # 使用实际的模型名称而不是模型ID
                                                        request_data["messages"],
                                                        final_response
                                                    )
                                                    
                                                    # 使用简单的异步数据库连接
                                                    conn = None
                                                    try:
                                                        # 直接创建新的数据库连接
                                                        conn = await get_db_connection()
                                                        await save_conversation_async(
                                                            conn, 
                                                            response_id, 
                                                            model_config["model"], 
                                                            formatted_conversation
                                                        )
                                                        await self.async_logger.info("✅ 流式响应数据已存入数据库")
                                                        saved_to_db = True
                                                    except Exception as e:
                                                        if "UNIQUE constraint failed" in str(e):
                                                            await self.async_logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                                                            saved_to_db = True
                                                        else:
                                                            await self.async_logger.error(f"❌ 保存流式响应数据时出错: {e}\n{traceback.format_exc()}")
                                                    finally:
                                                        # 使用完后关闭连接
                                                        if conn is not None:
                                                            try:
                                                                await conn.close()
                                                            except Exception as e:
                                                                await self.async_logger.error(f"❌ 关闭数据库连接时出错: {e}\n{traceback.format_exc()}")
                                                except Exception as e:
                                                    await self.async_logger.error(f"保存流式响应数据时出错: {e}\n{traceback.format_exc()}")
                                            break
                                        
                                        try:
                                            json_chunk = json.loads(line_str[6:])
                                            if "id" in json_chunk and not response_id:
                                                response_id = json_chunk["id"]
                                                await self.async_logger.debug(f"获取到响应ID: {response_id}")
                                            
                                            if "choices" in json_chunk and json_chunk["choices"]:
                                                delta = json_chunk["choices"][0].get("delta", {})
                                                #await self.async_logger.debug(f"处理delta数据: {json.dumps(delta, ensure_ascii=False)}")
                                                
                                                reasoning = delta.get("reasoning_content")
                                                if reasoning is not None:
                                                    complete_reasoning += reasoning
                                                    has_reasoning = True
                                                    #await self.async_logger.debug(f"添加reasoning内容: {reasoning}")
                                                
                                                content = delta.get("content")
                                                if content is not None:
                                                    complete_response += content
                                                    #await self.async_logger.debug(f"添加content内容: {content}")
                                        except json.JSONDecodeError as e:
                                            if line_str.strip() != "data: [DONE]":
                                                await self.async_logger.error(f"JSON解析错误: {e}, 原始数据: {line_str}\n{traceback.format_exc()}")
                                        except Exception as e:
                                            await self.async_logger.error(f"处理JSON数据时发生错误: {e}, 原始数据: {line_str}\n{traceback.format_exc()}")
                                except Exception as e:
                                    await self.async_logger.error(f"处理单行数据时发生错误: {e}\n{traceback.format_exc()}")
                                    continue
                                
                        except asyncio.TimeoutError as e:
                            await self.async_logger.error(f"❌ 流式响应处理超时: {e}\n{traceback.format_exc()}")
                        except Exception as e:
                            await self.async_logger.error(f"流式响应处理过程中发生错误: {e}\n{traceback.format_exc()}")
                        finally:
                            # 尝试写入结束标记
                            try:
                                if not response.prepared:
                                    await response.prepare(request)
                                await response.write_eof()
                            except Exception as e:
                                await self.async_logger.error(f"写入结束标记时发生错误: {e}\n{traceback.format_exc()}")
                            
                            return response
                    else:
                        # 处理非流式响应
                        response_json = await resp.json()
                        await self.async_logger.info("✅ 非流式响应处理完成")
                        #await self.async_logger.debug(f"响应内容: {json.dumps(response_json, ensure_ascii=False, indent=2)}")
                        
                        # 解析响应并保存数据
                        response_id = response_json.get("id")
                        if response_id and "choices" in response_json and response_json["choices"]:
                            choice = response_json["choices