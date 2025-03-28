# LLM代理服务器系统说明文档

## 项目概述
本项目包含三个独立的代理服务器程序，每个程序都针对不同的使用场景，为LLM（大语言模型）服务提供不同层次的代理功能。

## 组件说明

### 1. proxy_server.py
> 类似 oneapi 代理 通过json配置，设定模型名称，base_url,api_key,auth_type等配置，然后转发到实际的模型服务。
#### 功能特点
- 提供统一的LLM模型代理服务
- 支持自定义认证令牌验证
- 支持模型ID到实际服务端点的映射
- 支持流式和非流式响应
- 自动保存对话记录到数据库

#### 适用场景
- 需要统一管理多个LLM服务接入点
- 需要对不同模型进行访问控制
- 需要统一收集和存储对话数据

#### 使用方法
```bash
python proxy_server.py -p 8080 -c config.json
```
配置文件示例（config.json）：
```json
{
    "proxy_config": {
        "auth_tokens": ["sk-your-token-1", "sk-your-token-2"]
    },
    "models": {
        "111": {
            "model": "Pro/deepseek-ai/DeepSeek-R1",
            "key": "sk-actual-api-key",
            "end_point": "http://api.siliconflow.cn/v1/chat/completions"
        }
    }
}
```

### 2. proxy_endpoint.py
> 主要取代 模型设置中的api地址，key，模型名称，需要你实际拥有的，它主要用于在各种app中，把api地址设置成代理地址后，可以正常调用
#### 功能特点
- 支持基于配置的端点转发
- 支持流式和非流式响应处理
- 提供健康检查接口
- 支持请求重试机制

#### 适用场景
- 需要对特定模型服务进行代理转发
- 需要统一的错误处理和日志记录
- 需要支持流式输出的场景


#### 使用方法
```bash
python proxy_endpoint.py -p 8080 -c endpoint_config.json
```
配置文件示例（endpoint_config.json）：
```json
{
    "endpoints": {
        "provider1": {
            "base_url": "http://api.example.com",
            "chat_completion_path": "/v1/chat/completions",
            "models": ["model1", "model2"],
            "auth_type": "bearer"
        }
    }
}
```

### 3. proxy_gateway.py
#### 功能特点
- 支持对话数据的解析和存储
- 支持流式响应的处理
- 提供统一的数据格式转换

#### 适用场景
- 需要收集和存储模型对话数据
- 需要对响应数据进行格式转换
- 需要支持流式输出的场景
- 在app中设置代理，但是这种模式不支持https(很多模型服务商支持http)

## 部署说明

### 环境要求
- Python 3.7+
- 依赖包：aiohttp, sqlite3

### 安装步骤
1. 克隆代码库
2. 安装依赖：`pip install -r requirements.txt`
3. 配置相应的配置文件
4. 运行所需的代理服务器

## 注意事项
1. 请妥善保管API密钥和认证令牌
2. 建议在生产环境中使用HTTPS
3. 定期备份数据库文件
4. 监控日志文件大小，适时归档

## 常见问题
1. 如遇到连接超时，请检查网络连接和目标服务器状态
2. 数据库报错时，确保有适当的写入权限
3. 流式响应中断时，检查客户端连接状态

## 技术支持
如有问题，请查看各程序生成的日志文件：
- proxy_server.log
- proxy_endpoint.log
- proxy_gateway.log