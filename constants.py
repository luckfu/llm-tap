# 常量和配置项定义

# HTTP相关常量
DEFAULT_PORT = 8080
DEFAULT_CONFIG_PATH = 'endpoint_config.json'
DEFAULT_LOG_LEVEL = 'INFO'
MAX_REQUEST_SIZE = 8000000  # 约8MB

# 日志相关常量
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
LOG_FILE_NAME = 'proxy_endpoint.log'
DB_FILE_NAME = 'interactions.db'

# 可疑请求检测配置
SUSPICIOUS_PATHS = [
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

SUSPICIOUS_AGENTS = [
    'zgrab',
    'masscan',
    'nmap',
    'nikto',
    'sqlmap',
    'dirbuster',
    'gobuster'
]

# API路由配置
API_ROUTES = {
    'chat_completions': [
        '/v1/chat/completions',
        '/chat/completions'
    ],
    'embeddings': [
        '/v1/embeddings'
    ],
    'health': [
        '/health'
    ]
}

# 响应状态码
STATUS_CODES = {
    'OK': 200,
    'BAD_REQUEST': 400,
    'FORBIDDEN': 403,
    'PAYLOAD_TOO_LARGE': 413,
    'INTERNAL_SERVER_ERROR': 500
}

# 错误消息
ERROR_MESSAGES = {
    'FORBIDDEN': 'Forbidden',
    'REQUEST_TOO_LARGE': '请求体过大，请减小输入数据大小或分批处理',
    'INVALID_MODEL': '不支持的模型: {}',
    'INVALID_REQUEST_FORMAT': '无效的请求数据格式',
    'INTERNAL_ERROR': '服务器内部错误',
    'RESPONSE_ERROR': '响应数据传输错误，请重试'
}