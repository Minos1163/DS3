from iflow_sdk import IFlowClient, IFlowOptions, PermissionMode
from pathlib import Path
from typing import Optional, List


class IFlowClientWrapper:
    """
    iFlow 客户端包装器
    提供安全的文件系统访问功能
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self._client = None
        self._options = self._build_options()
    
    def _build_options(self) -> IFlowOptions:
        """
        构建 IFlowOptions 配置
        """
        iflow_config = self.config.get("iflow", {})
        
        # 必需参数
        file_access = bool(iflow_config.get("file_access", True))
        
        # 重要参数
        file_allowed_dirs = iflow_config.get("file_allowed_dirs", None)
        if file_allowed_dirs is None:
            # 默认允许当前工作目录
            file_allowed_dirs = [str(Path.cwd())]
        
        # 可选参数
        file_read_only = bool(iflow_config.get("file_read_only", False))
        file_max_size = int(iflow_config.get("file_max_size", 10 * 1024 * 1024))  # 默认 10MB
        cwd = iflow_config.get("cwd", str(Path.cwd()))
        
        # 构建选项
        options = IFlowOptions(
            file_access=file_access,
            file_allowed_dirs=file_allowed_dirs,
            file_read_only=file_read_only,
            file_max_size=file_max_size,
            cwd=cwd,
            permission_mode=PermissionMode.AUTO  # 自动批准
        )
        
        return options
    
    async def get_client(self) -> IFlowClient:
        """
        获取 IFlowClient 实例
        """
        if self._client is None:
            self._client = IFlowClient(self._options)
        return self._client
    
    async def __aenter__(self):
        """
        异步上下文管理器进入
        """
        return await self.get_client()
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        异步上下文管理器退出
        """
        pass
