"""
DeepSeek AI integration module for trading decisions.
"""
import os
from openai import OpenAI
from typing import Dict, Any, Optional
import json


class DeepSeekAI:
    """DeepSeek AI client for trading decisions."""
    
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        """
        Initialize DeepSeek AI client.
        
        Args:
            api_key: DeepSeek API key
            base_url: DeepSeek API base URL
        """
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = "deepseek-chat"
    
    def analyze_market(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data using DeepSeek AI.
        
        Args:
            market_data: Dictionary containing market information
            
        Returns:
            Dictionary with trading decision and reasoning
        """
        prompt = self._create_analysis_prompt(market_data)
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert cryptocurrency futures trader. Analyze the provided market data and provide trading recommendations in JSON format."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            return result
            
        except Exception as e:
            print(f"Error calling DeepSeek API: {e}")
            return {
                "action": "HOLD",
                "confidence": 0,
                "reasoning": f"Error: {str(e)}"
            }
    
    def _create_analysis_prompt(self, market_data: Dict[str, Any]) -> str:
        """Create analysis prompt from market data."""
        prompt = f"""
Analyze the following USDT-margined futures market data and provide a trading decision:

Symbol: {market_data.get('symbol', 'N/A')}
Current Price: ${market_data.get('price', 'N/A')}
24h Change: {market_data.get('price_change_percent', 'N/A')}%
24h Volume: ${market_data.get('volume', 'N/A')}
24h High: ${market_data.get('high', 'N/A')}
24h Low: ${market_data.get('low', 'N/A')}

Technical Indicators:
- RSI: {market_data.get('rsi', 'N/A')}
- MACD: {market_data.get('macd', 'N/A')}
- Signal: {market_data.get('macd_signal', 'N/A')}
- EMA 20: ${market_data.get('ema_20', 'N/A')}
- EMA 50: ${market_data.get('ema_50', 'N/A')}

Based on this data, provide your analysis in the following JSON format:
{{
    "action": "BUY" or "SELL" or "HOLD",
    "confidence": 0-100,
    "entry_price": recommended entry price (if applicable),
    "stop_loss": recommended stop loss price (if applicable),
    "take_profit": recommended take profit price (if applicable),
    "reasoning": "detailed explanation of your decision"
}}
"""
        return prompt
