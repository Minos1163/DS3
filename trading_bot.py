"""
Main trading bot that orchestrates AI analysis and trading execution.
"""
import os
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from dotenv import load_dotenv

from deepseek_ai import DeepSeekAI
from exchange import BinanceExchange
from technical_analysis import TechnicalAnalysis
from risk_manager import RiskManager


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TradingBot:
    """AI-powered USDT futures trading bot using DeepSeek."""
    
    def __init__(self):
        """Initialize trading bot."""
        # Load environment variables
        load_dotenv()
        
        # Initialize components
        self.deepseek = DeepSeekAI(
            api_key=os.getenv('DEEPSEEK_API_KEY'),
            base_url=os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
        )
        
        self.exchange = BinanceExchange(
            api_key=os.getenv('BINANCE_API_KEY'),
            api_secret=os.getenv('BINANCE_API_SECRET'),
            testnet=os.getenv('USE_TESTNET', 'True').lower() == 'true'
        )
        
        self.technical_analysis = TechnicalAnalysis()
        self.risk_manager = RiskManager(
            risk_per_trade=float(os.getenv('RISK_PER_TRADE', '0.02')),
            max_position_size=float(os.getenv('MAX_POSITION_SIZE', '0.01'))
        )
        
        # Trading parameters
        self.symbol = os.getenv('TRADING_SYMBOL', 'BTCUSDT')
        self.position_size = float(os.getenv('POSITION_SIZE', '0.001'))
        
        logger.info(f"Trading bot initialized for {self.symbol}")
    
    def run(self, interval: int = 300):
        """
        Run the trading bot in a loop.
        
        Args:
            interval: Sleep interval between iterations (seconds)
        """
        logger.info("Starting trading bot...")
        
        while True:
            try:
                self.execute_trading_cycle()
                logger.info(f"Sleeping for {interval} seconds...")
                time.sleep(interval)
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in trading cycle: {e}", exc_info=True)
                time.sleep(interval)
    
    def execute_trading_cycle(self):
        """Execute one trading cycle: analyze, decide, and trade."""
        logger.info("=" * 50)
        logger.info(f"Starting trading cycle at {datetime.now()}")
        
        # 1. Fetch market data
        market_data = self.exchange.get_market_data(self.symbol)
        if not market_data:
            logger.warning("Failed to fetch market data")
            return
        
        logger.info(f"Current price: ${market_data['price']:.2f}")
        
        # 2. Calculate technical indicators
        indicators = self.technical_analysis.calculate_indicators(
            market_data.get('klines', [])
        )
        
        if indicators:
            market_data.update(indicators)
            trend = self.technical_analysis.get_trend(indicators)
            logger.info(f"Technical trend: {trend}")
            logger.info(f"RSI: {indicators.get('rsi', 'N/A')}, "
                       f"MACD: {indicators.get('macd', 'N/A')}")
        
        # 3. Get AI analysis
        logger.info("Requesting AI analysis from DeepSeek...")
        ai_decision = self.deepseek.analyze_market(market_data)
        
        logger.info(f"AI Decision: {ai_decision.get('action', 'UNKNOWN')}")
        logger.info(f"Confidence: {ai_decision.get('confidence', 0)}%")
        logger.info(f"Reasoning: {ai_decision.get('reasoning', 'N/A')}")
        
        # 4. Check account balance
        balance = self.exchange.get_account_balance()
        if balance:
            logger.info(f"Account balance: ${balance.get('total_balance', 0):.2f}")
        
        # 5. Check current position
        current_position = self.exchange.get_position(self.symbol)
        if current_position:
            logger.info(f"Current position: {current_position}")
        
        # 6. Execute trading decision
        self.execute_decision(ai_decision, market_data, balance, current_position)
    
    def execute_decision(
        self,
        ai_decision: Dict[str, Any],
        market_data: Dict[str, Any],
        balance: Dict[str, Any],
        current_position: Optional[Dict[str, Any]]
    ):
        """
        Execute trading decision based on AI analysis.
        
        Args:
            ai_decision: AI trading decision
            market_data: Current market data
            balance: Account balance
            current_position: Current position info
        """
        action = ai_decision.get('action', 'HOLD')
        confidence = ai_decision.get('confidence', 0)
        
        # Skip if confidence is too low
        if not self.risk_manager.should_enter_trade(
            confidence=confidence,
            account_balance=balance.get('available_balance', 0),
            current_positions=1 if current_position else 0
        ):
            logger.info("Trade rejected by risk manager")
            return
        
        # Handle HOLD action
        if action == 'HOLD':
            logger.info("AI recommends HOLD - no action taken")
            return
        
        # Close existing position if direction changes
        if current_position:
            current_side = 'BUY' if current_position['position_amt'] > 0 else 'SELL'
            if (action == 'BUY' and current_side == 'SELL') or \
               (action == 'SELL' and current_side == 'BUY'):
                logger.info("Closing opposite position...")
                self.close_position(current_position)
        
        # Open new position
        if action in ['BUY', 'SELL']:
            entry_price = market_data['price']
            stop_loss = ai_decision.get('stop_loss') or \
                       self.risk_manager.calculate_stop_loss(entry_price, action)
            
            position_size = self.risk_manager.calculate_position_size(
                account_balance=balance.get('available_balance', 0),
                entry_price=entry_price,
                stop_loss=stop_loss,
                confidence=confidence
            )
            
            if position_size > 0:
                logger.info(f"Opening {action} position...")
                logger.info(f"Entry: ${entry_price:.2f}, Stop Loss: ${stop_loss:.2f}")
                logger.info(f"Position size: {position_size}")
                
                order = self.exchange.place_order(
                    symbol=self.symbol,
                    side=action,
                    quantity=position_size,
                    order_type='MARKET'
                )
                
                if order:
                    logger.info(f"Order executed: {order.get('orderId', 'N/A')}")
                else:
                    logger.error("Failed to execute order")
    
    def close_position(self, position: Dict[str, Any]):
        """
        Close an existing position.
        
        Args:
            position: Position information
        """
        symbol = position['symbol']
        position_amt = position['position_amt']
        side = 'SELL' if position_amt > 0 else 'BUY'
        quantity = abs(position_amt)
        
        logger.info(f"Closing position: {side} {quantity} {symbol}")
        
        order = self.exchange.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type='MARKET'
        )
        
        if order:
            logger.info(f"Position closed: {order.get('orderId', 'N/A')}")
        else:
            logger.error("Failed to close position")


def main():
    """Main entry point."""
    try:
        bot = TradingBot()
        bot.run(interval=300)  # Run every 5 minutes
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)


if __name__ == '__main__':
    main()
