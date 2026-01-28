"""
持仓数据管理器
负责获取和管理持仓信息
"""
from typing import Dict, Any, Optional


class PositionDataManager:
    """持仓数据管理器"""
    
    def __init__(self, client):
        """
        初始化持仓数据管理器
        
        Args:
            client: Binance API客户端
        """
        self.client = client
    
    def get_current_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取当前持仓
        
        Returns:
            {
                'side': 'LONG' 或 'SHORT',
                'amount': 0.001,
                'entry_price': 115000.0,
                'mark_price': 115050.0,
                'leverage': 10,
                'margin': 115.0,
                'unrealized_pnl': 5.0,
                'pnl_percent': 0.43,
                'liquidation_price': 105000.0
            }
        """
        try:
            position = self.client.get_position(symbol)
            if not position:
                return None
            
            # 解析持仓数据
            amount = float(position['positionAmt'])
            if amount == 0:
                return None
            
            side = 'LONG' if amount > 0 else 'SHORT'
            entry_price = float(position['entryPrice'])
            mark_price = float(position['markPrice'])
            leverage = int(position['leverage'])
            unrealized_pnl = float(position['unRealizedProfit'])
            
            # 计算盈亏百分比
            if entry_price > 0:
                if side == 'LONG':
                    pnl_percent = ((mark_price - entry_price) / entry_price) * 100
                else:
                    pnl_percent = ((entry_price - mark_price) / entry_price) * 100
            else:
                pnl_percent = 0.0
            
            # 保证金
            margin = abs(amount * entry_price / leverage) if leverage > 0 else 0
            
            return {
                'side': side,
                'amount': abs(amount),
                'entry_price': entry_price,
                'mark_price': mark_price,
                'leverage': leverage,
                'margin': margin,
                'unrealized_pnl': unrealized_pnl,
                'pnl_percent': pnl_percent,
                'liquidation_price': float(position.get('liquidationPrice', 0)),
                'notional': abs(amount * mark_price)  # 名义价值
            }
        except Exception as e:
            print(f"⚠️ 获取持仓失败 {symbol}: {e}")
            return None
    
    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有持仓
        
        Returns:
            {
                'BTCUSDT': {...},
                'ETHUSDT': {...},
                ...
            }
        """
        try:
            positions = self.client.get_all_positions()
            result = {}
            
            for pos in positions:
                symbol = pos['symbol']
                amount = float(pos['positionAmt'])
                if amount != 0:
                    result[symbol] = self.get_current_position(symbol)
            
            return result
        except Exception as e:
            print(f"⚠️ 获取所有持仓失败: {e}")
            return {}
    
    def has_position(self, symbol: str) -> bool:
        """检查是否有持仓"""
        position = self.get_current_position(symbol)
        return position is not None
