import json
import logging
from typing import Optional, Dict, Any

# 配置日志
logger = logging.getLogger(__name__)

class ConfigManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = {}
        self.load_config()
    
    def load_config(self):
        """从配置文件加载配置"""
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
        
        logger.warning(f"⚠️ 未找到模型 {model} 的配置")
        return None
    
    def get_auth_tokens(self) -> list:
        """获取认证令牌列表"""
        return self.config.get("proxy_config", {}).get("auth_tokens", [])
    
    def get_config(self) -> Dict[str, Any]:
        """获取完整的配置数据"""
        return self.config