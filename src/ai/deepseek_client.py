"""
DeepSeek AI客户端
调用DeepSeek API进行交易决策
"""

from openai import OpenAI


import os
import warnings
from typing import Any, Dict, Optional


class DeepSeekClient:
    """DeepSeek AI客户端"""

    def __init__(self, api_key: Optional[str] = None, model: str = "deepseek-reasoner"):
        """
        初始化DeepSeek客户端

        Args:
            api_key: DeepSeek API密钥
            model: 模型名称
        """
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY 未设置")

        self.model = model
        self.base_url = "https://api.deepseek.com"
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers={"User-Agent": "AI-Trading-Bot"},
        )

        # 抑制urllib3警告
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    def analyze_and_decide(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """
        调用AI分析并获取决策

        Args:
            prompt: 提示词
            **kwargs: 其他参数

        Returns:
            {
            'reasoning_content': 'AI推理过程',
                'content': '决策内容（JSON字符串）',
                'raw_response': 完整响应对象
            }
        """
        try:
            # 调用API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个专业的加密货币量化交易AI助手。",
                    },
                    {"role": "user", "content": prompt},
                ],
                stream=False,
                **kwargs,
            )

            # 提取内容
            reasoning_content = None
            message = response.choices[0].message
            content = message.content

            # 如果使用推理模型，尝试提取推理内容
            if hasattr(message, "reasoning_content"):
                reasoning_content = getattr(message, "reasoning_content", None)
            elif hasattr(response, "reasoning_content"):
                reasoning_content = getattr(response, "reasoning_content", None)

            # 尝试从 response.choices[0] 获取
            if not reasoning_content and hasattr(response.choices[0], "reasoning_content"):
                reasoning_content = getattr(response.choices[0], "reasoning_content", None)

            # 打印推理过程（如果有）
            if reasoning_content:
                print("\n🧠 AI推理过程:")
                print(reasoning_content)
                print()

            return {
                "reasoning_content": reasoning_content,
                "content": content,
                "raw_response": response,
                "usage": {
                    "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                    "total_tokens": getattr(response.usage, "total_tokens", 0),
                },
            }

        except Exception as e:
            print(f"❌ DeepSeek API调用失败: {e}")
            raise

    def get_reasoning(self, response: Dict[str, Any]) -> str:
        """
        获取AI推理过程

        Returns:
            推理内容字符串
        """
        return response.get("reasoning_content", "")

    def get_decision_content(self, response: Dict[str, Any]) -> str:
        """
        获取AI决策内容

        Returns:
            决策内容字符串（通常是JSON）
        """
        return response.get("content", "")

    def calculate_cost(self, response: Dict[str, Any]) -> float:
        """
        计算API调用成本

        Returns:
            成本（USDT）
        """
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        # DeepSeek定价（示例，请查看实际定价）
        # 假设: $0.001/1K tokens
        cost = (prompt_tokens + completion_tokens) / 1000 * 0.001
        return cost
