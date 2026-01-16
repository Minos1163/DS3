"""
Technical analysis module for calculating indicators.
"""
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from typing import List, Dict, Any


class TechnicalAnalysis:
    """Calculate technical indicators for trading analysis."""
    
    @staticmethod
    def calculate_indicators(klines: List) -> Dict[str, Any]:
        """
        Calculate technical indicators from kline data.
        
        Args:
            klines: List of kline data from exchange
            
        Returns:
            Dictionary with calculated indicators
        """
        if not klines or len(klines) < 50:
            return {}
        
        # Convert klines to DataFrame
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 
            'volume', 'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        
        # Convert to numeric
        df['close'] = pd.to_numeric(df['close'])
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        df['volume'] = pd.to_numeric(df['volume'])
        
        # Calculate RSI
        rsi = RSIIndicator(df['close'], window=14)
        current_rsi = float(rsi.rsi().iloc[-1])
        
        # Calculate MACD
        macd = MACD(df['close'])
        current_macd = float(macd.macd().iloc[-1])
        current_signal = float(macd.macd_signal().iloc[-1])
        
        # Calculate EMAs
        ema_20 = EMAIndicator(df['close'], window=20)
        ema_50 = EMAIndicator(df['close'], window=50)
        current_ema_20 = float(ema_20.ema_indicator().iloc[-1])
        current_ema_50 = float(ema_50.ema_indicator().iloc[-1])
        
        return {
            'rsi': round(current_rsi, 2),
            'macd': round(current_macd, 2),
            'macd_signal': round(current_signal, 2),
            'ema_20': round(current_ema_20, 2),
            'ema_50': round(current_ema_50, 2)
        }
    
    @staticmethod
    def get_trend(indicators: Dict[str, Any]) -> str:
        """
        Determine market trend based on indicators.
        
        Args:
            indicators: Dictionary with technical indicators
            
        Returns:
            Trend string: 'BULLISH', 'BEARISH', or 'NEUTRAL'
        """
        if not indicators:
            return 'NEUTRAL'
        
        bullish_signals = 0
        bearish_signals = 0
        
        # RSI analysis
        rsi = indicators.get('rsi', 50)
        if rsi > 70:
            bearish_signals += 1
        elif rsi < 30:
            bullish_signals += 1
        
        # MACD analysis
        macd = indicators.get('macd', 0)
        signal = indicators.get('macd_signal', 0)
        if macd > signal:
            bullish_signals += 1
        elif macd < signal:
            bearish_signals += 1
        
        # EMA analysis
        ema_20 = indicators.get('ema_20', 0)
        ema_50 = indicators.get('ema_50', 0)
        if ema_20 > ema_50:
            bullish_signals += 1
        elif ema_20 < ema_50:
            bearish_signals += 1
        
        if bullish_signals > bearish_signals:
            return 'BULLISH'
        elif bearish_signals > bullish_signals:
            return 'BEARISH'
        else:
            return 'NEUTRAL'
