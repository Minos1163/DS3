"""
环境变量管理器
负责加载和管理环境变量
"""

import os
from typing import Optional, Tuple

from dotenv import load_dotenv


class EnvManager:
    """环境变量管理器"""

    @staticmethod
    def load_env_file(file_path: str = ".env") -> bool:
        """
        加载环境变量文件

        Args:
            file_path: .env文件路径

        Returns:
            是否成功加载
        """
        if os.path.exists(file_path):
            load_dotenv(file_path)
            return True
        else:
            print(f"⚠️ 环境变量文件不存在: {file_path}")
            return False

    @staticmethod
    def get_api_credentials() -> Tuple[Optional[str], Optional[str]]:
        """
        获取API凭证

        Returns:
            (api_key, api_secret)
        """
        api_key = os.getenv("BINANCE_API_KEY")
        api_secret = os.getenv("BINANCE_SECRET")

        if not api_key or not api_secret:
            print("⚠️ API凭证未配置")

        return api_key, api_secret

    @staticmethod
    def get_deepseek_key() -> Optional[str]:
        """获取DeepSeek API密钥"""
        return os.getenv("DEEPSEEK_API_KEY")

    @staticmethod
    def require_env(key: str, error_msg: Optional[str] = None) -> str:
        """
        获取必需的环境变量，不存在则抛出异常

        Args:
            key: 环境变量名
            error_msg: 错误消息

        Returns:
            环境变量值

        Raises:
            ValueError: 环境变量不存在
        """
        value = os.getenv(key)
        if not value:
            msg = error_msg or f"环境变量 {key} 未设置"
            raise ValueError(msg)
        return value
