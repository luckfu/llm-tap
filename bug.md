import json
import logging
import time
import asyncio
from aiohttp import web
import aiohttp
import sqlite3
import argparse
from typing import Dict, Any, Optional

# ========== 命令行参数解析 ==========
def parse_args():
    parser = argparse.ArgumentParser(description="LLM代理服务器")
    parser.add_argument("-p", "--port", type=int, default=8080, help="服务器监听端口（默认：8080）")
    parser.add_argument("-c", "--config", type=str, default="endpoint_config.json", help="配置文件路径（默认：endpoint_config.json）")
    return parser.parse_args()

# ========== 数据库初始化 ==========
conn = sqlite3.connect("interactions.db", check_same_thread=False)
c = conn.cursor()
c.execute(
    """CREATE TABLE IF NOT EXISTS interactions (
        id TEXT PRIMARY KEY,
        model TEXT,
        conversation TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )"""
)
conn.commit()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('proxy_endpoint.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def format_to_sharegpt(model: str, messages: list, response: str) -> dict:
    """将对话格式化为目标格式"""
    system_message = ""
    conversations = []
    
    # 处理原始消息
    for msg in messages:
        if msg["role"] == "system":
            system_message = msg["content"]
        else:
            # 将 user 转换为 human，assistant 转换为 gpt
            role = "human" if msg["role"] == "user" else "gpt"
            conversations.append({
                "from": role,
                "value": msg["content"].strip()
            })
            
            # 如果消息中包含工具调用，添加相应的 function_call 和 observation
            if msg.get("tool_calls"):
                for tool_call in msg["tool_calls"]:
                    # 添加函数调用
                    conversations.append({
                        "from": "function_call",
                        "value": json.dumps(tool_call, ensure_ascii=False)
                    })
                    # 如果有工具调用结果，添加 observation
                    if "function" in tool_call and "output" in tool_call:
                        conversations.append({
                            "from": "observation",
                            "value": tool_call["output"]
                        })
    
    # 处理助手的回复
    conversations.append({
        "from": "gpt",
        "value": response.strip()
    })
    
    # 如果最后的响应包含工具调用，也需要添加
    try:
        response_data = json.loads(response)
        if isinstance(response_data, dict) and response_data.get("tool_calls"):
            for tool_call in response_data["tool_calls"]:
                conversations.append({
                    "from": "function_call",
                    "value": json.dumps(tool_call, ensure_ascii=False)
                })
    except json.JSONDecodeError:
        pass
    
    return {
        "conversations": conversations,
        "system": system_message,
        "tools": []  # 可以根据实际工具定义填充此字段
    }

class ProxyEndpoint:
    def __init__(self, config_path: str = "endpoint_config.json", port: int = 8080):
        self.app = web.Application()
        self.config_path = config_path
        self.port = port
        self.setup_routes()
        self.load_config()
    
    def setup_routes(self):
        self.app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        self.app.router.add_post("/chat/completions", self.handle_chat_completions)
        self.app.router.add_get("/health", self.handle_health_check)

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
    def is_suspicious_request(self, request: web.Request) -> bool:
        """检查是否为可疑请求"""
        # 获取客户端IP
        client_ip = request.remote
        
        # 检查请求路径
        path = request.path.lower()
        suspicious_paths = [
            '/manager/',
            '/phpmyadmin/',
            '/wp-admin/',
            '/wp-login',
            '/admin/',
            '.php',
            '.asp',
            '.aspx',
            '/download/powershell/',
            '/get.php'
        ]
        
        # 检查User-Agent
        user_agent = request.headers.get('User-Agent', '').lower()
        suspicious_agents = [
            'zgrab',
            'masscan',
            'nmap',
            'nikto',
            'sqlmap',
            'dirbuster',
            'gobuster'
        ]
        
        # 检查是否为可疑路径
        is_suspicious_path = any(sus_path in path for sus_path in suspicious_paths)
        
        # 检查是否为可疑User-Agent
        is_suspicious_agent = any(agent in user_agent for agent in suspicious_agents)
        
        # 检查请求头格式
        has_invalid_headers = '\n' in str(request.headers)
        
        if is_suspicious_path or is_suspicious_agent or has_invalid_headers:
            logger.warning(
                f"⚠️ 检测到可疑请求 | IP: {client_ip} | 路径: {path} | "
                f"User-Agent: {user_agent} | 请求头: {dict(request.headers)}"
            )
            return True
        return False

    async def handle_health_check(self, request: web.Request) -> web.Response:
        # 检查是否为可疑请求
        if self.is_suspicious_request(request):
            return web.Response(status=403, text=json.dumps({"error": "Forbidden"}))
        return web.Response(status=200, text=json.dumps({"status": "ok"}))

    async def handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        # 检查是否为可疑请求
        if self.is_suspicious_request(request):
            logger.warning(f"🚫 拒绝可疑请求访问 chat/completions 接口")
            return web.Response(status=403, text=json.dumps({"error": "Forbidden"}))
            
        # 获取原始请求的headers和body
        headers = dict(request.headers)
        request_data = await request.json()
        
        # 记录原始请求信息
        logger.debug("📝 收到新的请求")
        logger.debug(f"请求头: {json.dumps(headers, ensure_ascii=False, indent=2)}")
        logger.debug(f"请求体: {json.dumps(request_data, ensure_ascii=False, indent=2)}")
        
        # 获取模型对应的端点配置
        model = request_data.get("model")
        logger.info(f"📝 处理模型请求: {model}")
        endpoint_config = self.get_endpoint_for_model(model)
        
        if not endpoint_config:
            logger.warning(f"❌ 不支持的模型: {model}")
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
        logger.info(f"📡 转发请求到目标服务器: {endpoint_config['base_url']}, 流式请求: {is_stream}")
        
        # 记录请求开始时间
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            target_url = f"{endpoint_config['base_url']}{endpoint_config['path']}"
            try:
                # 设置更长的超时时间，包括连接和读取超时
                timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_connect=30, sock_read=30)
                async with session.post(
                    target_url,
                    headers=forward_headers,
                    json=request_data,
                    timeout=timeout
                ) as resp:
                    # 记录请求耗时
                    elapsed_time = time.time() - start_time
                    logger.info(f"请求耗时: {elapsed_time:.2f}秒")
                    
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"❌ 目标服务器错误: {error_text}")
                        return web.Response(
                            status=resp.status,
                            text=json.dumps({"error": f"目标服务器错误: {error_text}"})
                        )
                    
                    logger.info("✅ 成功接收到目标服务器响应")
                    
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
                        logger.info("🌊 开始处理流式响应")
                        
                        complete_response = ""
                        complete_reasoning = ""
                        has_reasoning = False
                        response_id = None
                        
                        try:
                            retry_count = 0
                            max_retries = 3
                            while retry_count < max_retries:
                                try:
                                    async for line in resp.content:
                                        try:
                                            line_str = line.decode("utf-8")
                                            logger.debug(f"接收到流式数据: {line_str}")
                                            
                                            # 写入响应前记录日志
                                            logger.debug("准备写入响应数据")
                                            await asyncio.wait_for(response.write(line), timeout=10.0)
                                            logger.debug("响应数据写入成功")
                                            retry_count = 0  # 成功后重置重试计数
                                            
                                            if line_str.startswith("data: "):
                                                try:
                                                    json_chunk = json.loads(line_str[6:])
                                                    logger.debug(f"解析JSON数据: {json.dumps(json_chunk, ensure_ascii=False)}")
                                                    
                                                    if "id" in json_chunk and not response_id:
                                                        response_id = json_chunk["id"]
                                                        logger.debug(f"获取到响应ID: {response_id}")
                                                    
                                                    if "choices" in json_chunk and json_chunk["choices"]:
                                                        delta = json_chunk["choices"][0].get("delta", {})
                                                        logger.debug(f"处理delta数据: {json.dumps(delta, ensure_ascii=False)}")
                                                        
                                                        reasoning = delta.get("reasoning_content")
                                                        if reasoning is not None:
                                                            complete_reasoning += reasoning
                                                            has_reasoning = True
                                                            logger.debug(f"添加reasoning内容: {reasoning}")
                                                        
                                                        content = delta.get("content")
                                                        if content is not None:
                                                            complete_response += content
                                                            logger.debug(f"添加content内容: {content}")
                                                except json.JSONDecodeError as e:
                                                    logger.error(f"JSON解析错误: {e}, 原始数据: {line_str}")
                                                except Exception as e:
                                                    logger.error(f"处理JSON数据时发生错误: {e}")
                                            
                                            if line_str.strip() == "data: [DONE]":
                                                logger.info("收到流式响应结束标记")
                                                # 跳过对结束标记的JSON解析
                                                await response.write(line)
                                                # 保存对话数据
                                                if response_id:
                                                    final_response = complete_response
                                                    if has_reasoning and complete_reasoning:
                                                        final_response = f"<think>\n{complete_reasoning}\n</think>\n\n\n{complete_response}"
                                                    
                                                    formatted_conversation = format_to_sharegpt(
                                                        model,
                                                        request_data["messages"],
                                                        final_response
                                                    )
                                                    
                                                    try:
                                                        with sqlite3.connect("interactions.db") as conn:
                                                            c = conn.cursor()
                                                            c.execute(
                                                                """INSERT INTO interactions (id, model, conversation)
                                                                VALUES (?, ?, ?)""",
                                                                (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False))
                                                            )
                                                            conn.commit()
                                                            logger.info("✅ 流式响应数据已存入数据库")
                                                    except sqlite3.IntegrityError:
                                                        logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                                                    except Exception as e:
                                                        logger.error(f"❌ 保存流式响应数据时出错: {e}")
                                                
                                                logger.info("✅ 流式响应处理完成")
                                                await response.write_eof()
                                                return response
                                        except UnicodeDecodeError as e:
                                            logger.error(f"解码错误: {e}, 原始数据: {line}")
                                        except Exception as e:
                                            logger.error(f"处理单行数据时发生错误: {e}")
                                            raise
                                except Exception as e:
                                    logger.error(f"流式响应处理过程中发生错误: {e}")
                                    raise
                                
                                retry_count += 1
                                if retry_count >= max_retries:
                                    raise Exception("达到最大重试次数")
                                await asyncio.sleep(1)  # 重试前等待1秒
                        except Exception as e:
                            logger.error(f"流式响应处理过程中发生错误: {e}")
                            raise
                            
                            if line.strip() == b"data: [DONE]":
                                # 保存对话数据
                                if response_id:
                                    final_response = complete_response
                                    if has_reasoning and complete_reasoning:
                                        final_response = f"<think>\n{complete_reasoning}\n</think>\n\n\n{complete_response}"
                                    
                                    formatted_conversation = format_to_sharegpt(
                                        model,
                                        request_data["messages"],
                                        final_response
                                    )
                                    
                                    try:
                                        with sqlite3.connect("interactions.db") as conn:
                                            c = conn.cursor()
                                            c.execute(
                                                """INSERT INTO interactions (id, model, conversation)
                                                VALUES (?, ?, ?)""",
                                                (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False))
                                            )
                                            conn.commit()
                                            logger.info("✅ 流式响应数据已存入数据库")
                                    except sqlite3.IntegrityError:
                                        logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                                    except Exception as e:
                                        logger.error(f"❌ 保存流式响应数据时出错: {e}")
                                
                                logger.info("✅ 流式响应处理完成")
                                await response.write_eof()
                                return response
                    else:
                        # 处理非流式响应
                        response_json = await resp.json()
                        logger.info("✅ 非流式响应处理完成")
                        logger.debug(f"响应内容: {json.dumps(response_json, ensure_ascii=False, indent=2)}")
                        
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
                            
                            # 保存对话数据
                            formatted_conversation = format_to_sharegpt(
                                model,
                                request_data["messages"],
                                final_response
                            )
                            
                            try:
                                with sqlite3.connect("interactions.db") as conn:
                                    c = conn.cursor()
                                    c.execute(
                                        """INSERT INTO interactions (id, model, conversation)
                                        VALUES (?, ?, ?)""",
                                        (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False))
                                    )
                                    conn.commit()
                                    logger.info("✅ 非流式响应数据已存入数据库")
                            except sqlite3.IntegrityError:
                                logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                            except Exception as e:
                                logger.error(f"❌ 保存非流式响应数据时出错: {e}")
                        
                        return web.Response(
                            status=200,
                            body=json.dumps(response_json, ensure_ascii=False).encode('utf-8'),
                            content_type="application/json"
                        )
            except json.JSONDecodeError:
                logger.error("❌ 无效的请求数据格式")
                return web.Response(status=400, text=json.dumps({"error": "无效的请求数据格式"}))
            except Exception as e:
                logger.error(f"处理请求时发生错误: {e}")
                return web.Response(status=500, text=json.dumps({"error": "服务器内部错误"}))


if __name__ == "__main__":
    args = parse_args()
    proxy = ProxyEndpoint(config_path=args.config, port=args.port)
    try:
        web.run_app(proxy.app, host="127.0.0.1", port=args.port)
    except Exception as e:
        logger.error(f"启动服务器时发生错误: {e}")
    finally:
        logger.info("服务器已关闭")
