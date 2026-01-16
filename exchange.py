"""
Binance USDT futures exchange integration.
"""
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
from typing import Dict, Any, Optional
import time


logger = logging.getLogger(__name__)


class BinanceExchange:
    """Binance USDT futures exchange client."""
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        """
        Initialize Binance exchange client.
        
        Args:
            api_key: Binance API key
            api_secret: Binance API secret
            testnet: Use testnet if True
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        if testnet:
            self.client = Client(
                api_key=api_key,
                api_secret=api_secret,
                testnet=True
            )
        else:
            self.client = Client(
                api_key=api_key,
                api_secret=api_secret
            )
    
    def get_market_data(self, symbol: str) -> Dict[str, Any]:
        """
        Get current market data for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')
            
        Returns:
            Dictionary with market data
        """
        try:
            ticker = self.client.futures_ticker(symbol=symbol)
            klines = self.client.futures_klines(
                symbol=symbol,
                interval=Client.KLINE_INTERVAL_1HOUR,
                limit=100
            )
            
            return {
                'symbol': symbol,
                'price': float(ticker['lastPrice']),
                'price_change_percent': float(ticker['priceChangePercent']),
                'volume': float(ticker['volume']),
                'high': float(ticker['highPrice']),
                'low': float(ticker['lowPrice']),
                'klines': klines
            }
        except BinanceAPIException as e:
            logger.error(f"Binance API error getting market data for {symbol}: {e}")
            return {}
    
    def get_account_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        try:
            account = self.client.futures_account()
            balance = {
                'total_balance': float(account['totalWalletBalance']),
                'available_balance': float(account['availableBalance']),
                'total_unrealized_profit': float(account['totalUnrealizedProfit'])
            }
            return balance
        except BinanceAPIException as e:
            logger.error(f"Error getting account balance: {e}")
            return {}
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get current position for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Position information or None
        """
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for position in positions:
                if float(position['positionAmt']) != 0:
                    return {
                        'symbol': position['symbol'],
                        'position_amt': float(position['positionAmt']),
                        'entry_price': float(position['entryPrice']),
                        'unrealized_profit': float(position['unRealizedProfit']),
                        'leverage': int(position['leverage'])
                    }
            return None
        except BinanceAPIException as e:
            logger.error(f"Error getting position for {symbol}: {e}")
            return None
    
    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = 'MARKET',
        price: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Place an order on Binance futures.
        
        Args:
            symbol: Trading symbol
            side: 'BUY' or 'SELL'
            quantity: Order quantity
            order_type: Order type (MARKET, LIMIT, etc.)
            price: Order price (for LIMIT orders)
            
        Returns:
            Order response or None
        """
        try:
            if order_type == 'MARKET':
                order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=order_type,
                    quantity=quantity
                )
            else:
                order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=order_type,
                    quantity=quantity,
                    price=price,
                    timeInForce='GTC'
                )
            
            return order
        except BinanceAPIException as e:
            logger.error(f"Error placing order for {symbol}: {e}")
            return None
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Set leverage for a symbol.
        
        Args:
            symbol: Trading symbol
            leverage: Leverage value (1-125)
            
        Returns:
            True if successful
        """
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return True
        except BinanceAPIException as e:
            logger.error(f"Error setting leverage for {symbol}: {e}")
            return False
