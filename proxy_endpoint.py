
import json
import logging
import time
import asyncio
from aiohttp import web
import aiohttp
import argparse
from typing import Dict, Any, Optional
import traceback
import sqlite3
import aiosqlite
from utils import format_to_sharegpt, init_async_logger, get_async_logger, init_db_path, get_db_connection, save_conversation_async

# 配置日志
def parse_args():
    parser = argparse.ArgumentParser(description='Proxy Endpoint Server')
    parser.add_argument('--config', type=str, default='endpoint_config.json', help='配置文件路径')
    parser.add_argument('--port', type=int, default=8080, help='服务器端口')
    parser.add_argument('--log-level', type=str, default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='日志级别')
    return parser.parse_args()

args = parse_args()

# 只设置基本日志级别，不添加处理器，避免重复日志
logging.basicConfig(level=getattr(logging, args.log_level.upper()))
logger = logging.getLogger(__name__)
# 移除所有处理器，防止重复日志
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

class ProxyEndpoint:
    def __init__(self, config_path: str = "endpoint_config.json", port: int = 8080):
        self.app = web.Application()
        self.config_path = config_path
        self.port = port
        self.setup_routes()
        self.load_config()
        
        # 在应用启动时添加异步初始化函数
        self.app.on_startup.append(self.init_async_resources)
    
    def setup_routes(self):
        self.app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        self.app.router.add_post("/chat/completions", self.handle_chat_completions)
        self.app.router.add_post("/v1/embeddings", self.handle_embeddings)
        self.app.router.add_get("/health", self.handle_health_check)
        
    async def init_async_resources(self, app):
        """初始化异步资源（日志和数据库）"""
        # 初始化异步日志
        await asyncio.to_thread(init_async_logger, "proxy_endpoint", "proxy_endpoint.log", getattr(logging, args.log_level.upper()))
        self.async_logger = get_async_logger()
        await self.async_logger.info("✅ 异步日志初始化完成")
        
        # 初始化数据库路径
        await init_db_path("interactions.db")
        await self.async_logger.info("✅ 数据库初始化完成")
        
    async def handle_embeddings(self, request: web.Request) -> web.Response:
        # 检查是否为可疑请求
        if await self.is_suspicious_request(request):
            await self.async_logger.warning(f"🚫 拒绝可疑请求访问 embeddings 接口")
            return web.Response(status=403, text=json.dumps({"error": "Forbidden"}))
            
        # 获取原始请求的headers和body
        headers = dict(request.headers)
        request_data = await request.json()
        
        # 检查请求体大小
        input_text = request_data.get("input", "")
        if isinstance(input_text, list):
            total_chars = sum(len(str(text)) for text in input_text)
        else:
            total_chars = len(str(input_text))
            
        # 设置最大请求大小限制（约8MB）
        max_chars = 8000000
        if total_chars > max_chars:
            await self.async_logger.warning(f"❌ 请求体过大: {total_chars} 字符，超过限制 {max_chars} 字符")
            return web.Response(
                status=413,
                text=json.dumps({"error": "请求体过大，请减小输入数据大小或分批处理"})
            )
            
        # 记录原始请求信息
        await self.async_logger.debug("📝 收到新的embeddings请求")
        # 创建请求头的副本并移除敏感信息
        safe_headers = headers.copy()
        if "Authorization" in safe_headers:
            safe_headers["Authorization"] = "[REDACTED]"
        #await self.async_logger.debug(f"请求头: {json.dumps(safe_headers, ensure_ascii=False, indent=2)}")
        #await self.async_logger.debug(f"请求体: {json.dumps(request_data, ensure_ascii=False, indent=2)}")
        
        # 检查并移除dimensions字段
        ##if "dimensions" in request_data:
        ##    dimensions_value = request_data["dimensions"]
        ##    await self.async_logger.info(f"🔄 移除请求中的dimensions字段，原始值: {dimensions_value}")
        ##    del request_data["dimensions"]
        
        # 获取模型对应的端点配置
        model = request_data.get("model")
        await self.async_logger.info(f"📝 处理embeddings模型请求: {model}")
        
        # 查找支持embeddings的端点
        endpoint_config = None
        for provider, config in self.config.get("endpoints", {}).items():
            if "embeddings_models" in config and model in config.get("embeddings_models", []):
                endpoint_config = {
                    "base_url": config["base_url"],
                    "path": config["embeddings_path"]
                }
                break
        
        if not endpoint_config:
            await self.async_logger.warning(f"❌ 不支持的embeddings模型: {model}")
            return web.Response(
                status=400,
                text=json.dumps({"error": f"不支持的embeddings模型: {model}"})
            )
        
        # 创建新的headers，保持原始认证信息和Content-Length
        forward_headers = {
            "Content-Type": "application/json",
            "Authorization": headers.get("Authorization", ""),
            "Content-Length": headers.get("Content-Length", "")
        }
        
        # 记录请求开始时间
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            target_url = f"{endpoint_config['base_url']}{endpoint_config['path']}"
            try:
                # 设置超时时间
                timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_connect=30, sock_read=600)
                async with session.post(
                    target_url,
                    headers=forward_headers,
                    data=await request.read(),  # 使用原始请求体数据
                    timeout=timeout
                ) as resp:
                    # 记录请求耗时
                    elapsed_time = time.time() - start_time
                    await self.async_logger.info(f"embeddings请求耗时: {elapsed_time:.2f}秒")
                    
                    if resp.status != 200:
                        error_text = await resp.text()
                        await self.async_logger.error(f"❌ embeddings目标服务器错误: {error_text}")
                        return web.Response(
                            status=resp.status,
                            text=json.dumps({"error": f"embeddings目标服务器错误: {error_text}"})
                        )
                    
                    # 处理响应
                    try:
                        response_text = await resp.text()
                        response_json = json.loads(response_text)
                        await self.async_logger.info("✅ embeddings响应处理完成")
                        #await self.async_logger.debug(f"响应内容: {json.dumps(response_json, ensure_ascii=False, indent=2)}")
                        
                        return web.Response(
                            status=200,
                            body=json.dumps(response_json, ensure_ascii=False).encode('utf-8'),
                            content_type="application/json"
                        )
                    except aiohttp.ClientPayloadError as e:
                        await self.async_logger.error(f"❌ 响应数据传输错误: {e}")
                        return web.Response(status=500, text=json.dumps({"error": "响应数据传输错误，请重试"}))
            except json.JSONDecodeError:
                await self.async_logger.error("❌ 无效的embeddings请求数据格式")
                return web.Response(status=400, text=json.dumps({"error": "无效的请求数据格式"}))
            except Exception as e:
                await self.async_logger.error(f"处理embeddings请求时发生错误: {e}\n{traceback.format_exc()}")
                return web.Response(status=500, text=json.dumps({"error": "服务器内部错误"}))

    def load_config(self):
        """从配置文件加载端点配置"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
                logger.info(f"✅ 成功加载配置文件: {self.config_path}")
        except Exception as e:
            logger.error(f"❌ 加载配置文件失败: {e}")
            self.config = {}
    
    def get_endpoint_for_model(self, model: str) -> Optional[Dict[str, Any]]:
        """根据模型名称获取对应的端点配置"""
        if not self.config or "endpoints" not in self.config:
            logger.error("❌ 配置文件中缺少endpoints配置")
            return None
        
        for provider, config in self.config["endpoints"].items():
            if model in config.get("models", []):
                return {
                    "base_url": config["base_url"],
                    "path": config["chat_completion_path"]
                }
        
        logger.warning(f"⚠️ 未找到模型 {model} 的端点配置")
        return None

    # 添加异常访问检测和日志记录
    async def is_suspicious_request(self, request: web.Request) -> bool:
        """检查是否为可疑请求"""
        # 获取请求路径
        path = request.path.lower()
        
        # 获取配置文件中定义的所有合法路径
        valid_paths = [
            '/v1/chat/completions',
            '/chat/completions',
            '/v1/embeddings',
            '/health'
        ]
        
        # 检查请求路径是否在合法路径列表中
        is_valid_path = any(path.endswith(valid_path) for valid_path in valid_paths)
        
        # 如果不是合法路径，则视为可疑请求
        if not is_valid_path:
            client_ip = request.remote
            user_agent = request.headers.get('User-Agent', '')
            await self.async_logger.warning(
                f"⚠️ 检测到未授权的请求 | IP: {client_ip} | 路径: {path} | "
                f"User-Agent: {user_agent}"
            )
            return True
        return False

    async def handle_health_check(self, request: web.Request) -> web.Response:
        # 检查是否为可疑请求
        if await self.is_suspicious_request(request):
            return web.Response(status=403, text=json.dumps({"error": "Forbidden"}))
        return web.Response(status=200, text=json.dumps({"status": "ok"}))

    async def handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        # 检查是否为可疑请求
        if await self.is_suspicious_request(request):
            await self.async_logger.warning(f"🚫 拒绝可疑请求访问 chat/completions 接口")
            return web.Response(status=403, text=json.dumps({"error": "Forbidden"}))
            
        # 获取原始请求的headers和body
        headers = dict(request.headers)
        request_data = await request.json()
        
        # 检查请求体大小
        messages = request_data.get("messages", [])
        total_chars = sum(len(str(msg)) for msg in messages)
            
        # 设置最大请求大小限制（约8MB）
        max_chars = 8000000
        if total_chars > max_chars:
            await self.async_logger.warning(f"❌ 请求体过大: {total_chars} 字符，超过限制 {max_chars} 字符")
            return web.Response(
                status=413,
                text=json.dumps({"error": "请求体过大，请减小输入数据大小或分批处理"})
            )
            
        # 记录原始请求信息
        await self.async_logger.debug("📝 收到新的请求")
        # 创建请求头的副本并移除敏感信息
        safe_headers = headers.copy()
        if "Authorization" in safe_headers:
            safe_headers["Authorization"] = "[REDACTED]"
        #await self.async_logger.debug(f"请求头: {json.dumps(safe_headers, ensure_ascii=False, indent=2)}")
        await self.async_logger.debug(f"请求体: {json.dumps(request_data, ensure_ascii=False, indent=2)}")
        
        # 获取模型对应的端点配置
        model = request_data.get("model")
        await self.async_logger.info(f"📝 处理模型请求: {model}")
        endpoint_config = self.get_endpoint_for_model(model)
        
        if not endpoint_config:
            await self.async_logger.warning(f"❌ 不支持的模型: {model}")
            return web.Response(
                status=400,
                text=json.dumps({"error": f"不支持的模型: {model}"})
            )
        
        # 创建新的headers，保持原始认证信息
        forward_headers = {
            "Content-Type": "application/json",
            "Authorization": headers.get("Authorization", "")
        }
        
        # 判断是否为流式请求
        is_stream = request_data.get("stream", False)
        await self.async_logger.info(f"📡 转发请求到目标服务器: {endpoint_config['base_url']}, 流式请求: {is_stream}")
        
        # 记录请求开始时间
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            target_url = f"{endpoint_config['base_url']}{endpoint_config['path']}"
            try:
                # 设置更长的超时时间，包括连接和读取超时
                timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_connect=30, sock_read=600)
                async with session.post(
                    target_url,
                    headers=forward_headers,
                    json=request_data,
                    timeout=timeout
                ) as resp:
                    # 记录请求耗时
                    elapsed_time = time.time() - start_time
                    await self.async_logger.info(f"请求耗时: {elapsed_time:.2f}秒")
                    
                    if resp.status != 200:
                        error_text = await resp.text()
                        await self.async_logger.error(f"❌ 目标服务器错误: {error_text}")
                        return web.Response(
                            status=resp.status,
                            text=json.dumps({"error": f"目标服务器错误: {error_text}"})
                        )
                    
                    await self.async_logger.info("✅ 成功接收到目标服务器响应")
                    
                    # 判断是否为API调用
                    is_api_call = request.path.startswith('/v1/') or request.path.startswith('/chat/')
                    
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
                                        await self.async_logger.warning("客户端连接已关闭，终止响应")
                                        # 设置标记以跳过数据库写入
                                        saved_to_db = True
                                        break
                                    
                                    line_str = line.decode("utf-8")
                                    #await self.async_logger.debug(f"接收到流式数据: ～{line_str}～")
                                    
                                    # 写入响应前记录日志
                                    await response.write(line)
                                    
                                    if line_str.startswith("data: "):
                                        # 先检查是否为结束标记
                                        if line_str.strip() == "data: [DONE]":
                                            await self.async_logger.info("收到流式响应结束标记")
                                            # 只有在正常完成时才保存数据
                                            if is_api_call and response_id and not saved_to_db:
                                                try:
                                                    # 构建最终响应
                                                    final_response = complete_response
                                                    if has_reasoning and complete_reasoning:
                                                        final_response = f"<think>\n{complete_reasoning}\n</think>\n\n\n{complete_response}"
                                                    await self.async_logger.debug(f"规整后的流式返回内容: ～{final_response}～")
                                                    formatted_conversation = format_to_sharegpt(model, request_data, final_response)
                                                    async with aiosqlite.connect("interactions.db") as conn:
                                                        await conn.execute(
                                                            "INSERT INTO interactions (id, model, conversation) VALUES (?, ?, ?)",
                                                            (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False))
                                                        )
                                                        await conn.commit()
                                                        saved_to_db = True
                                                        await self.async_logger.info("✅ 流式响应数据已存入数据库")
                                                except sqlite3.IntegrityError:
                                                    await self.async_logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                                                except Exception as e:
                                                    await self.async_logger.error(f"❌ 保存流式响应数据时出错: {e}")
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
                            # 只在正常结束时保存数据，异常情况下跳过
                            if response_id and not saved_to_db and complete_response:
                                try:
                                    final_response = complete_response
                                    if has_reasoning and complete_reasoning:
                                        final_response = f"<think>\n{complete_reasoning}\n</think>\n\n\n{complete_response}"
                                    
                                    formatted_conversation = format_to_sharegpt(
                                        model,
                                        request_data,
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
                                            model,
                                            formatted_conversation
                                        )
                                        saved_to_db = True
                                        await self.async_logger.info("✅ 流式响应数据已存入数据库")
                                    except sqlite3.IntegrityError:
                                        await self.async_logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                                        saved_to_db = True
                                    except Exception as e:
                                        await self.async_logger.error(f"❌ 保存流式响应数据时出错: {e}\n{traceback.format_exc()}")
                                    finally:
                                        if conn:
                                            await conn.close()
                                except Exception as e:
                                    await self.async_logger.error(f"❌ 保存流式响应数据时出错: {e}\n{traceback.format_exc()}")
                            
                            # 确保返回响应对象
                            return response
                    else:
                        # 处理非流式响应
                        response_json = await resp.json()
                        await self.async_logger.info("✅ 非流式响应处理完成")
                        await self.async_logger.debug(f"响应内容: {json.dumps(response_json, ensure_ascii=False, indent=2)}")
                        
                        # 解析响应并保存数据
                        response_id = response_json.get("id")
                        if response_id and "choices" in response_json and response_json["choices"]:
                            choice = response_json["choices"][0]
                            response_content = ""
                            reasoning_content = ""
                            
                            if "message" in choice:
                                reasoning = choice["message"].get("reasoning_content")
                                if reasoning is not None:
                                    reasoning_content = reasoning
                                content = choice["message"].get("content")
                                if content is not None:
                                    response_content = content
                            
                            # 格式化最终响应
                            final_response = response_content
                            if reasoning_content:
                                final_response = f"<think>\n{reasoning_content}\n</think>\n\n\n{response_content}"
                            
                            # 保存到数据库
                            try:
                                formatted_conversation = format_to_sharegpt(
                                    model,
                                    request_data,
                                    final_response
                                )
                                
                                async with aiosqlite.connect("interactions.db") as conn:
                                    await conn.execute(
                                        "INSERT INTO interactions (id, model, conversation) VALUES (?, ?, ?)",
                                        (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False))
                                    )
                                    await conn.commit()
                                    await self.async_logger.info("✅ 非流式响应数据已存入数据库")
                            except sqlite3.IntegrityError:
                                await self.async_logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                            except Exception as e:
                                await self.async_logger.error(f"❌ 保存非流式响应数据时出错: {e}")
                        
                        # 返回响应
                        return web.Response(
                            status=200,
                            body=json.dumps(response_json, ensure_ascii=False).encode('utf-8'),
                            content_type="application/json"
                        )
            except json.JSONDecodeError:
                await self.async_logger.error(f"❌ 无效的请求数据格式\n{traceback.format_exc()}")
                return web.Response(status=400, text=json.dumps({"error": "无效的请求数据格式"}))
            except Exception as e:
                await self.async_logger.error(f"处理请求时发生错误: {e}\n{traceback.format_exc()}")
                return web.Response(status=500, text=json.dumps({"error": "服务器内部错误"}))


if __name__ == "__main__":
    args = parse_args()
    proxy = ProxyEndpoint(config_path=args.config, port=args.port)
    try:
        web.run_app(proxy.app, host="0.0.0.0", port=args.port)
    except Exception as e:
        logger.error(f"启动服务器时发生错误: {e}")
    finally:
        logger.info("服务器已关闭")
