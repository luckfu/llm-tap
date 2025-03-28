import asyncio
import socket
import json
import sqlite3
import logging

# ========== 日志配置 ==========
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

file_handler = logging.FileHandler("proxy_gateway.log")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

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

def format_to_sharegpt(model, messages, response):
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
    
    # 从原始消息中提取tools字段
    tools = []
    for msg in messages:
        if msg.get("tools"):
            tools.extend(msg["tools"])
    
    return {
        "conversations": conversations,
        "system": system_message,
        "tools": tools
    }

def save_to_db(model, conversation):
    """存储格式化的对话数据到 SQLite"""
    try:
        with sqlite3.connect("interactions.db") as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO interactions (model, conversation)
                VALUES (?, ?)""",
                (model, conversation),
            )
            conn.commit()
            logger.info("✅ 数据已存入数据库")
    except Exception as e:
        logger.error(f"❌ 数据库错误: {e}", exc_info=True)

# ========== SOCKS5 代理 ==========
async def handle_client(reader, writer):
    """处理 SOCKS5 代理请求"""
    try:
        # 添加连接超时控制
        async def read_with_timeout(reader, size):
            try:
                return await asyncio.wait_for(reader.read(size), timeout=10)  # 10秒超时
            except asyncio.TimeoutError:
                logger.warning("读取数据超时")
                raise
        
        # 修改原有的 read 调用为带超时的版本
        ver = await read_with_timeout(reader, 1)
        if not ver or ver[0] != 0x05:
            logger.warning("⛔ 非 SOCKS5 连接，拒绝访问")
            writer.close()
            return

        nmethods = await read_with_timeout(reader, 1)
        methods = await read_with_timeout(reader, nmethods[0])
        writer.write(b"\x05\x00")
        await writer.drain()

        req = await reader.read(4)
        if len(req) < 4:
            logger.warning("⛔ 不完整的 SOCKS5 连接请求")
            writer.close()
            return

        _, cmd, _, atyp = req

        if cmd != 0x01:
            logger.warning("⛔ 仅支持 CONNECT 请求")
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            writer.close()
            return

        if atyp == 1:
            addr = socket.inet_ntoa(await reader.read(4))
        elif atyp == 3:
            domain_len = await reader.read(1)
            addr = await reader.read(domain_len[0])
            addr = addr.decode()
        elif atyp == 4:
            addr = socket.inet_ntop(socket.AF_INET6, await reader.read(16))
        else:
            logger.warning("⛔ 不支持的地址类型")
            writer.close()
            return

        port_bytes = await reader.read(2)
        port = int.from_bytes(port_bytes, "big")

        try:
            target_reader, target_writer = await asyncio.open_connection(addr, port)
        except Exception as e:
            logger.error(f"❌ 无法连接到 {addr}:{port} - {e}")
            writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            writer.close()
            return

        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        await writer.drain()

        request_data = ""
        response_data = ""
        model = ""
        messages = []
        is_api_call = False  # 判断是否为 API 调用
        response_id = ""
        saved_to_db = False  # 将 saved_to_db 移到外部作用域
        
        async def client_to_target():
            nonlocal request_data, model, messages, is_api_call
            request_buffer = ""
            try:
                while True:
                    try:
                        data = await read_with_timeout(reader, 4096)
                        if not data:
                            logger.debug("client_to_target: No more data from client, exiting")
                            break
                        request_buffer += data.decode("utf-8", errors="ignore")
                        # 确保收到完整的请求数据
                        if "\r\n\r\n" in request_buffer:
                            headers, body = request_buffer.split("\r\n\r\n", 1)
                            try:
                                # 只在第一次解析时尝试获取 model 和 messages
                                if not is_api_call and body.strip():
                                    json_data = json.loads(body)
                                    logger.debug(f"Request JSON: {json_data}")
                                    if "model" in json_data and "messages" in json_data:
                                        is_api_call = True
                                        model = json_data["model"]
                                        messages = json_data["messages"]
                                        logger.debug(f"API call detected. model: {model}, messages: {messages}")
                            except json.JSONDecodeError:
                                logger.debug("Not a JSON request or incomplete JSON data")
                            except Exception as e:
                                logger.error(f"Error parsing request: {e}")
                        
                        target_writer.write(data)
                        await target_writer.drain()
                    except asyncio.TimeoutError:
                        logger.warning("客户端读取超时")
                        break
                    except Exception as e:
                        logger.error(f"Error in client_to_target loop: {e}")
                        break
            except Exception as e:
                logger.error(f"Error in client_to_target: {e}")
            finally:
                logger.debug("client_to_target: Exited successfully")

        async def target_to_client():
            nonlocal response_data, response_id, saved_to_db  # 添加 saved_to_db 到 nonlocal 声明
            headers_complete = False
            body = ""
            reasoning_content = ""
            final_content = ""
            has_reasoning = False
            complete_response = ""
            complete_reasoning = ""
            is_stream = False
            response_buffer = ""
            saved_to_db = False  # 添加标志位记录是否已保存到数据库
            try:
                while True:
                    try:
                        data = await read_with_timeout(target_reader, 4096)
                        if not data:
                            logger.debug("target_to_client: No more data from target, breaking")
                            break
                        decoded_data = data.decode("utf-8", errors="ignore")
                        response_buffer += decoded_data

                        if not headers_complete and "\r\n\r\n" in response_buffer:
                            headers, body = response_buffer.split("\r\n\r\n", 1)
                            headers_complete = True
                            is_stream = "transfer-encoding: chunked" in headers.lower()
                            response_buffer = body

                        # 处理流式响应
                        if headers_complete and is_stream:
                            for line in decoded_data.split("\n"):
                                line = line.strip()
                                if line == "data: [DONE]":
                                    logger.info("Received [DONE] marker, ending stream")
                                    # 在这里处理数据库写入，确保只有在正常结束时才写入
                                    if is_api_call and response_id and response_data and not saved_to_db:
                                        logger.debug("正在保存流式响应数据")
                                        formatted_conversation = format_to_sharegpt(model, messages, response_data)
                                        try:
                                            with sqlite3.connect("interactions.db") as conn:
                                                c = conn.cursor()
                                                c.execute(
                                                    """INSERT INTO interactions (id, model, conversation)
                                                    VALUES (?, ?, ?)""",
                                                    (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False)),
                                                )
                                                conn.commit()
                                                logger.info("✅ 流式响应数据已存入数据库")
                                                saved_to_db = True  # 标记已保存
                                        except sqlite3.IntegrityError:
                                            logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
                                            saved_to_db = True  # 如果记录已存在，也标记为已保存
                                        except Exception as e:
                                            logger.error(f"❌ 保存数据时发生错误: {e}")
                                    break
                                if line.startswith("data: "):
                                    logger.debug(f"Raw stream data: {line}")
                                    try:
                                        json_chunk = json.loads(line[6:])
                                        if "id" in json_chunk and not response_id:
                                            response_id = json_chunk["id"]
                                        if "choices" in json_chunk and json_chunk["choices"]:
                                            delta = json_chunk["choices"][0].get("delta", {})
                                            reasoning = delta.get("reasoning_content")
                                            if reasoning is not None:
                                                reasoning_content += reasoning
                                                has_reasoning = True
                                            # 处理 content
                                            content = delta.get("content")
                                            if content is not None:
                                                complete_response += content
                                            # 如果 content 为 None，尝试使用 reasoning_content
                                            elif delta.get("reasoning_content") is not None:
                                                complete_reasoning += delta["reasoning_content"]
                                            else:
                                                logger.warning("收到的 delta 中 content 和 reasoning_content 都为 None")
                                                
                                    except json.JSONDecodeError:
                                        logger.error(f"JSON parsing error in stream: {line}")
                        # 处理非流式响应
                        elif headers_complete and not is_stream:
                            try:
                                json_response = json.loads(response_buffer)
                                logger.debug(f"Non-stream response: {json_response}")
                                if "id" in json_response:
                                    response_id = json_response["id"]
                                if "choices" in json_response and json_response["choices"]:
                                    choice = json_response["choices"][0]
                                    if "message" in choice:
                                        reasoning = choice["message"].get("reasoning_content")
                                        if reasoning is not None:
                                            reasoning_content = reasoning
                                            has_reasoning = True
                                        content = choice["message"].get("content")
                                        if content is not None:
                                            final_content = content
                                    else:
                                        reasoning = choice.get("reasoning_content")
                                        if reasoning is not None:
                                            reasoning_content = reasoning
                                            has_reasoning = True
                                        content = choice.get("content")
                                        if content is not None:
                                            final_content = content
                            
                            except json.JSONDecodeError:
                                logger.error(f"JSON parsing error in non-stream response: {response_buffer}")
                        
                        writer.write(data)
                        await writer.drain()
                    except asyncio.TimeoutError:
                        logger.warning("目标服务器读取超时")
                        break
                    except Exception as e:
                        logger.error(f"Error in target_to_client loop: {e}")
                        break
            except Exception as e:
                logger.error(f"Error in target_to_client: {e}")
            finally:
                # 确保在流结束时更新 response_data
                if has_reasoning and complete_reasoning:
                    response_data_parts = []
                    response_data_parts.append(f"<think>\n{complete_reasoning}\n</think>")
                    if complete_response:
                        response_data_parts.append(complete_response)
                    response_data = "\n\n\n".join(response_data_parts)
                else:
                    response_data = complete_response or final_content

                logger.debug(f"Final response data: {response_data}")

        await asyncio.gather(client_to_target(), target_to_client())
        logger.debug("Both client_to_target and target_to_client completed")
        
        # 只有在确认是 API 调用且尚未保存时才保存到数据库
        if is_api_call and response_id and not saved_to_db:  # 添加 saved_to_db 检查
            logger.debug("正在保存数据")
            formatted_conversation = format_to_sharegpt(model, messages, response_data)
            try:
                with sqlite3.connect("interactions.db") as conn:
                    c = conn.cursor()
                    c.execute(
                        """INSERT INTO interactions (id, model, conversation)
                        VALUES (?, ?, ?)""",
                        (response_id, model, json.dumps(formatted_conversation, ensure_ascii=False)),
                    )
                    conn.commit()
                    logger.info("✅ 数据已存入数据库")
            except sqlite3.IntegrityError:
                logger.warning(f"⚠️ ID {response_id} 已存在，跳过保存")
            except Exception as e:
                logger.error(f"❌ 数据库错误: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"❌ 处理客户端错误: {e}")
    finally:
        try:
            writer.close()
            logger.debug("Client writer closed")
        except Exception as e:
            logger.error(f"❌ Error closing client writer: {e}")
        try:
            target_writer.close()
            logger.debug("Target writer closed")
        except Exception as e:
            logger.error(f"❌ Error closing target writer: {e}")

async def start_socks5_proxy():
    """启动 SOCKS5 代理"""
    server = await asyncio.start_server(handle_client, "0.0.0.0", 1080)
    async with server:
        logger.info("🚀 SOCKS5 代理已启动，监听端口 1080")
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(start_socks5_proxy())