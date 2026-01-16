"""
Risk management module for position sizing and stop loss.
"""
from typing import Dict, Any, Optional


class RiskManager:
    """Manage trading risk and position sizing."""
    
    def __init__(self, risk_per_trade: float = 0.02, max_position_size: float = 0.1):
        """
        Initialize risk manager.
        
        Args:
            risk_per_trade: Risk percentage per trade (0.02 = 2%)
            max_position_size: Maximum position size as fraction of balance
        """
        self.risk_per_trade = risk_per_trade
        self.max_position_size = max_position_size
    
    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
        confidence: float
    ) -> float:
        """
        Calculate position size based on risk parameters.
        
        Args:
            account_balance: Total account balance
            entry_price: Intended entry price
            stop_loss: Stop loss price
            confidence: AI confidence level (0-100)
            
        Returns:
            Position size in base currency
        """
        if entry_price <= 0 or stop_loss <= 0 or account_balance <= 0:
            return 0.0
        
        # Calculate risk amount
        risk_amount = account_balance * self.risk_per_trade
        
        # Calculate price difference
        price_diff = abs(entry_price - stop_loss)
        if price_diff == 0:
            return 0.0
        
        # Calculate base position size
        position_size = risk_amount / price_diff
        
        # Adjust based on confidence
        confidence_factor = confidence / 100.0
        adjusted_position_size = position_size * confidence_factor
        
        # Apply maximum position size limit
        max_allowed = account_balance * self.max_position_size / entry_price
        final_position_size = min(adjusted_position_size, max_allowed)
        
        return round(final_position_size, 6)
    
    def calculate_stop_loss(
        self,
        entry_price: float,
        side: str,
        atr: Optional[float] = None,
        multiplier: float = 2.0
    ) -> float:
        """
        Calculate stop loss price.
        
        Args:
            entry_price: Entry price
            side: 'BUY' or 'SELL'
            atr: Average True Range (optional)
            multiplier: ATR multiplier for stop loss
            
        Returns:
            Stop loss price
        """
        if atr is None:
            # Use percentage-based stop loss
            stop_distance = entry_price * 0.02  # 2% stop loss
        else:
            stop_distance = atr * multiplier
        
        if side == 'BUY':
            return round(entry_price - stop_distance, 2)
        else:  # SELL
            return round(entry_price + stop_distance, 2)
    
    def calculate_take_profit(
        self,
        entry_price: float,
        stop_loss: float,
        side: str,
        risk_reward_ratio: float = 2.0
    ) -> float:
        """
        Calculate take profit price based on risk/reward ratio.
        
        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            side: 'BUY' or 'SELL'
            risk_reward_ratio: Risk/reward ratio (default 2:1)
            
        Returns:
            Take profit price
        """
        risk = abs(entry_price - stop_loss)
        reward = risk * risk_reward_ratio
        
        if side == 'BUY':
            return round(entry_price + reward, 2)
        else:  # SELL
            return round(entry_price - reward, 2)
    
    def should_enter_trade(
        self,
        confidence: float,
        account_balance: float,
        current_positions: int = 0,
        min_confidence: float = 60.0
    ) -> bool:
        """
        Determine if a trade should be entered.
        
        Args:
            confidence: AI confidence level
            account_balance: Current account balance
            current_positions: Number of current open positions
            min_confidence: Minimum confidence threshold
            
        Returns:
            True if trade should be entered
        """
        if confidence < min_confidence:
            return False
        
        if account_balance <= 0:
            return False
        
        # Limit number of concurrent positions
        if current_positions >= 3:
            return False
        
        return True
