import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.iflow.iflow_client import IFlowClientWrapper
from iflow_sdk import AssistantMessage, TaskFinishMessage


class IFlowService:
    """
    iFlow 服务类
    提供文件系统访问功能
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self.client_wrapper = IFlowClientWrapper(self.config)
    
    async def send_message(self, message: str) -> str:
        """
        发送消息到 iFlow 并获取响应
        """
        async with await self.client_wrapper.get_client() as client:
            await client.send_message(message)

            response_parts: List[str] = []
            async for msg in client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    text = getattr(getattr(msg, "chunk", None), "text", None)
                    if text:
                        response_parts.append(text)
                elif isinstance(msg, TaskFinishMessage):
                    break

            return "".join(response_parts)
        return ""
    
    async def read_file(self, file_path: str) -> Optional[str]:
        """
        读取文件内容
        """
        message = f"读取文件 {file_path} 的内容"
        response = await self.send_message(message)
        return response
    
    async def write_file(self, file_path: str, content: str) -> bool:
        """
        写入文件内容
        """
        message = f"在 {file_path} 文件中写入以下内容:\n{content}"
        response = await self.send_message(message)
        return "成功" in response
    
    async def list_directory(self, directory_path: str) -> List[str]:
        """
        列出目录内容
        """
        message = f"列出目录 {directory_path} 下的所有文件和子目录"
        response = await self.send_message(message)
        
        # 解析响应，提取文件和目录列表
        # 这里需要根据实际响应格式进行解析
        files = []
        lines = response.split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('\t'):
                files.append(line)
        
        return files
    
    async def create_directory(self, directory_path: str) -> bool:
        """
        创建目录
        """
        message = f"创建目录 {directory_path}"
        response = await self.send_message(message)
        return "成功" in response
    
    async def delete_file(self, file_path: str) -> bool:
        """
        删除文件
        """
        message = f"删除文件 {file_path}"
        response = await self.send_message(message)
        return "成功" in response
    
    async def get_file_info(self, file_path: str) -> Dict[str, Any]:
        """
        获取文件信息
        """
        message = f"获取文件 {file_path} 的信息，包括大小、修改时间等"
        response = await self.send_message(message)
        
        # 解析响应，提取文件信息
        info = {}
        lines = response.split('\n')
        for line in lines:
            line = line.strip()
            if ':' in line:
                key, value = line.split(':', 1)
                info[key.strip()] = value.strip()
        
        return info
    
    async def __aenter__(self):
        """
        异步上下文管理器进入
        """
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        异步上下文管理器退出
        """
        pass
