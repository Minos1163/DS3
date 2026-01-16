"""
Test script for DS3 trading bot components.
"""
import sys
from unittest.mock import Mock, patch
from deepseek_ai import DeepSeekAI
from exchange import BinanceExchange
from technical_analysis import TechnicalAnalysis
from risk_manager import RiskManager


def test_technical_analysis():
    """Test technical analysis calculations."""
    print("Testing Technical Analysis...")
    
    # Create mock kline data
    klines = []
    base_price = 50000
    for i in range(100):
        klines.append([
            1000000000 + i * 60000,  # timestamp
            str(base_price + i * 10),  # open
            str(base_price + i * 10 + 50),  # high
            str(base_price + i * 10 - 50),  # low
            str(base_price + i * 10 + 20),  # close
            '100',  # volume
            1000000000 + (i + 1) * 60000,  # close_time
            '5000000',  # quote_volume
            '1000',  # trades
            '50',  # taker_buy_base
            '2500000',  # taker_buy_quote
            '0'  # ignore
        ])
    
    ta = TechnicalAnalysis()
    indicators = ta.calculate_indicators(klines)
    
    assert 'rsi' in indicators
    assert 'macd' in indicators
    assert 'ema_20' in indicators
    assert 'ema_50' in indicators
    
    print(f"  ✓ RSI: {indicators['rsi']}")
    print(f"  ✓ MACD: {indicators['macd']}")
    print(f"  ✓ EMA 20: {indicators['ema_20']}")
    print(f"  ✓ EMA 50: {indicators['ema_50']}")
    
    trend = ta.get_trend(indicators)
    print(f"  ✓ Trend: {trend}")
    
    print("✅ Technical Analysis tests passed!\n")


def test_risk_manager():
    """Test risk management calculations."""
    print("Testing Risk Manager...")
    
    rm = RiskManager(risk_per_trade=0.02, max_position_size=0.1)
    
    # Test position size calculation
    position_size = rm.calculate_position_size(
        account_balance=10000,
        entry_price=50000,
        stop_loss=49000,
        confidence=80
    )
    
    assert position_size > 0
    print(f"  ✓ Position size: {position_size}")
    
    # Test stop loss calculation
    stop_loss = rm.calculate_stop_loss(entry_price=50000, side='BUY')
    assert stop_loss < 50000
    print(f"  ✓ Stop loss (BUY): ${stop_loss}")
    
    # Test take profit calculation
    take_profit = rm.calculate_take_profit(
        entry_price=50000,
        stop_loss=49000,
        side='BUY',
        risk_reward_ratio=2.0
    )
    assert take_profit > 50000
    print(f"  ✓ Take profit (BUY): ${take_profit}")
    
    # Test trade entry decision
    should_enter = rm.should_enter_trade(
        confidence=75,
        account_balance=10000,
        current_positions=0
    )
    assert should_enter is True
    print(f"  ✓ Should enter trade: {should_enter}")
    
    print("✅ Risk Manager tests passed!\n")


def test_deepseek_integration():
    """Test DeepSeek AI integration structure."""
    print("Testing DeepSeek AI Integration...")
    
    # Mock the OpenAI client
    with patch('deepseek_ai.OpenAI') as mock_openai:
        mock_client = Mock()
        mock_openai.return_value = mock_client
        
        # Create DeepSeek instance
        deepseek = DeepSeekAI(api_key="test_key", base_url="https://test.api")
        
        assert deepseek.model == "deepseek-chat"
        print("  ✓ DeepSeek client initialized")
        
        # Test prompt creation
        market_data = {
            'symbol': 'BTCUSDT',
            'price': 50000,
            'price_change_percent': 2.5,
            'volume': 1000000,
            'high': 51000,
            'low': 49000,
            'rsi': 55,
            'macd': 100,
            'macd_signal': 90,
            'ema_20': 49800,
            'ema_50': 49500
        }
        
        prompt = deepseek._create_analysis_prompt(market_data)
        assert 'BTCUSDT' in prompt
        assert '50000' in prompt
        print("  ✓ Analysis prompt created")
    
    print("✅ DeepSeek AI Integration tests passed!\n")


def test_exchange_integration():
    """Test exchange integration structure."""
    print("Testing Exchange Integration...")
    
    # Mock the Binance client
    with patch('exchange.Client') as mock_client_class:
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        
        # Create exchange instance
        exchange = BinanceExchange(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True
        )
        
        assert exchange.testnet is True
        print("  ✓ Exchange client initialized")
        
        # Test market data structure
        mock_client.futures_ticker.return_value = {
            'lastPrice': '50000.00',
            'priceChangePercent': '2.5',
            'volume': '1000000',
            'highPrice': '51000.00',
            'lowPrice': '49000.00'
        }
        mock_client.futures_klines.return_value = []
        
        market_data = exchange.get_market_data('BTCUSDT')
        assert 'symbol' in market_data
        assert 'price' in market_data
        print("  ✓ Market data structure validated")
    
    print("✅ Exchange Integration tests passed!\n")


def main():
    """Run all tests."""
    print("=" * 60)
    print("DS3 Trading Bot - Component Tests")
    print("=" * 60 + "\n")
    
    try:
        test_technical_analysis()
        test_risk_manager()
        test_deepseek_integration()
        test_exchange_integration()
        
        print("=" * 60)
        print("✅ All tests passed successfully!")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
