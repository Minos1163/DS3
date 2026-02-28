---
name: meme-5m-rotation-executor
description: Execute a practical 5m short-term MEME trading framework that combines grid trading in ranges, breakout trend-following, sector rotation, and strict risk controls. Use when the user asks for crypto short-term plans, MEME coin entry/exit rules, grid parameters, trend confirmation, intraday risk limits, or review routines. In this workspace, treat this as the default framework for trading-related requests unless the user explicitly asks for a different strategy.
---

# Meme 5m Rotation Executor

## Mandatory Behavior

- Use this framework first for any trading-related question.
- Output actionable execution rules, not vague directional opinions.
- Always include: market mode, trigger, entry, stop, take-profit, position sizing, and risk guardrails.
- If inputs are missing, assume conservative defaults and state assumptions in one line.

## Execution Workflow

1. Run pre-trade preparation (10 minutes before session)
2. Classify market mode every 30 minutes
3. Select entry system: grid mode or trend mode
4. Apply position and exposure limits
5. Execute MEME rotation switch rules
6. Apply extreme-market response rules
7. Enforce daily risk stop rules
8. Run end-of-day review and parameter tuning

## Operational Rules

### 1) Pre-Trade Preparation

- Scan MEME candidates and keep only 3 to 5 active symbols.
- Selection filters:
  - Top-20 by 24h volume
  - Clear 5m volume expansion
  - Sufficient 5m ATR
  - Avoid obvious distribution patterns
- Rule: do not trade illiquid symbols and do not pre-position cold coins.

### 2) Market Mode Classification (Every 30 Minutes)

- Range mode:
  - EMA9 and EMA21 intertwined
  - No sustained volume expansion
  - Frequent upper/lower wicks
  - Primary tactic: grid
- Trend ignition mode:
  - EMA9 above EMA21 with clear slope
  - 2 to 3 consecutive high-volume bullish candles
  - Breakout of prior high structure
  - Primary tactic: trend-following
- Extreme sentiment mode:
  - 3 or more oversized bullish candles
  - RSI above 80
  - FOMO surge
  - Tactic: fast in/fast out and reduce exposure

### 3) Entry Systems

- Grid execution (range mode):
  - Use only after confirming range boundaries
  - Grid spacing = ATR(5m) x 0.8
  - 8 to 15 grid levels
  - Position per grid = 1% to 2%
  - Stop condition:
    - Break below range low -> pause all grid orders
    - Volume breakout above range high -> switch to trend mode
- Trend execution (trend mode):
  - Required signals:
    - High-volume breakout above prior high
    - Pullback holds EMA9
    - Volume does not decay materially
  - Entry:
    - Enter on breakout-pullback confirmation
    - Or on second high-volume continuation
  - Stop:
    - Break below EMA21
    - Or high-volume stall
  - Take-profit:
    - Scale out 30%
    - Trail the rest using higher lows

### 4) Position Management

- Hold at most 2 MEME positions at once.
- Max single-symbol exposure <= 20%.
- After 2 consecutive stop-outs, cut size by 50%.
- After 3 consecutive wins, do not increase size.

### 5) Rotation Mechanism

- Monitor:
  - Leader gain > 15%
  - Leader shifts to low-volume sideways
  - Second-tier symbols suddenly expand in volume
- Actions:
  - Reduce leader position
  - Rotate into early-stage laggard catch-up
  - Do not chase extended candles
- Rotation mnemonic:
  - Leader explodes -> watch catch-up names
  - Leader stalls -> reduce and transfer risk
  - Leader drops with volume -> de-risk market-wide

### 6) Extreme Market Response

- Flash dump (wick down): no bottom-fishing, wait for second confirmation, cut size by half.
- FOMO spike: do not chase the third large green candle, wait for pullback.
- Market panic: flat exposure and observe for 30 minutes.

### 7) Daily Risk System

- Max daily drawdown: 5%, then force stop trading.
- 3 consecutive losing days: switch to reduced-size mode.
- Keep at least 1 no-trade day per week.

### 8) Daily Review Loop

- Answer daily:
  - Which trades followed the system?
  - Which trades were emotion-driven?
  - Which setup had highest win rate?
  - Which entry got harvested most?
- Tune continuously:
  - Grid spacing
  - Trend confirmation strictness
  - Rotation timing speed

## Response Template

Use this compact output layout for execution requests:

```text
[Market Mode]
- Current mode: <range|trend|extreme>
- Evidence: <EMA/volume/structure>

[Execution Plan]
- Symbols: <3-5 active candidates>
- Entry trigger: <exact trigger>
- Position sizing: <percent>
- Stop: <exact invalidation>
- Take-profit: <scale/trailing rule>

[Rotation and Risk]
- Rotation condition: <leader->laggard logic>
- Daily risk status: <drawdown/stop state>
- Next review checkpoint: <time>
```

## Reference

- Read `references/playbook.md` when full rule details are needed verbatim.
