# PARAM_TUNING_SKILL.md

**AI Parameter Optimization Framework -- Crypto Fund Flow Strategy**

Version: 1.0\
Generated: 2026-02-25T12:44:14.740448 UTC

------------------------------------------------------------------------

# 🎯 Objective

This document defines how an AI agent should systematically tune
parameters for the multi-timeframe (15m + 5m) fund-flow trading system.

Primary goals:

-   Improve win rate stability
-   Improve Sharpe ratio
-   Reduce drawdown
-   Reduce weight oscillation
-   Avoid overfitting

This AI does NOT modify execution logic.\
It only adjusts parameters and evaluates performance impact.

------------------------------------------------------------------------

# 🧠 System Overview

FinalScore = 0.6 × Score_15m + 0.4 × Score_5m

Two main optimization domains:

1.  Default Weights (TREND / RANGE)
2.  Risk & Threshold Parameters

------------------------------------------------------------------------

# 📊 Tunable Parameter Categories

## 1️⃣ Default Weights

### TREND Regime

-   trend_cvd
-   trend_cvd_momentum
-   trend_oi_delta
-   trend_funding
-   trend_depth_ratio
-   trend_imbalance
-   trend_liquidity_delta
-   trend_micro_delta

Constraints:

-   All ∈ \[0,1\]
-   Sum must equal 1
-   Momentum (cvd_momentum + oi_delta) ≤ 0.45 recommended

------------------------------------------------------------------------

### RANGE Regime

-   range_cvd
-   range_cvd_momentum
-   range_oi_delta
-   range_funding
-   range_depth_ratio
-   range_imbalance
-   range_liquidity_delta
-   range_micro_delta

Constraints:

-   imbalance ≥ 0.20 recommended
-   micro_delta ≥ 0.12 recommended
-   trend continuation factors reduced

------------------------------------------------------------------------

## 2️⃣ Microstructure Thresholds

-   trap_guard
-   phantom_threshold
-   spread_z\_gate
-   extreme_vol_cooldown threshold

------------------------------------------------------------------------

## 3️⃣ Confidence Adjustment Parameters

-   confidence_floor
-   confidence_decay_on_risk
-   consistency_bonus_factor

------------------------------------------------------------------------

# 🔁 Optimization Methodology

## Step 1: Validate Data Integrity

Reject test runs where:

-   stale_seconds \> 30
-   missing_fields not empty
-   insufficient z-score history

------------------------------------------------------------------------

## Step 2: Define Optimization Target

Primary metrics:

-   Sharpe ratio (target \> 1.5)
-   Profit factor (\>1.4)
-   Max drawdown (\<25% of equity)
-   Win rate (55--70% sustainable)

Secondary:

-   Weight volatility
-   Confidence volatility

------------------------------------------------------------------------

## Step 3: Optimization Strategy

### Phase A -- Coarse Grid Search

Adjust:

-   Major weight groups in ±10% range
-   Keep relative structure intact

Evaluate 3--7 day rolling performance.

------------------------------------------------------------------------

### Phase B -- Sensitivity Testing

Perturb each parameter independently:

Δ ±5%

Observe:

-   Stability
-   Metric sensitivity
-   Drawdown change

Reject parameters causing instability.

------------------------------------------------------------------------

### Phase C -- Risk Stress Testing

Simulate:

-   High spread regime
-   Low liquidity regime
-   False breakout regime

Ensure:

-   Risk flags reduce confidence
-   Momentum weight decreases under trap

------------------------------------------------------------------------

# 🧩 Regime-Specific Optimization Rules

## TREND Optimization

If win rate \< 55%:

-   Reduce cvd_momentum
-   Increase depth_ratio
-   Increase liquidity_delta

If trend continuation underperforms:

-   Increase oi_delta weight
-   Increase funding weight slightly

------------------------------------------------------------------------

## RANGE Optimization

If overtrading detected:

-   Increase imbalance weight
-   Increase trap sensitivity
-   Reduce cvd weight

If missed reversals:

-   Increase micro_delta
-   Slightly increase phantom weight

------------------------------------------------------------------------

# 📈 Stability Controls

Parameter tuning must:

-   Preserve weight sum = 1
-   Avoid \>15% single-iteration changes
-   Apply smoothing over updates
-   Maintain regime logic consistency

------------------------------------------------------------------------

# 🛑 Hard Constraints

The AI must NEVER:

-   Change leverage logic
-   Modify execution thresholds directly
-   Disable risk guards
-   Remove fallback logic
-   Override flow_confirm logic

------------------------------------------------------------------------

# 🔬 Evaluation Protocol

Each parameter set must be evaluated over:

-   Trending sample
-   Ranging sample
-   Volatility spike sample

Use rolling window validation.

Reject if:

-   Performance degrades \>10% in any regime
-   Drawdown increases \>20%

------------------------------------------------------------------------

# 🧠 Adaptive Learning Rule

If 3 consecutive weeks:

-   Same regime underperforms → Apply controlled reweighting (max 8%
    adjustment total)

If performance recovers: → Freeze weights for minimum 7 days

------------------------------------------------------------------------

# 📌 Final Output Requirements

Parameter tuning AI must output:

1.  Parameter changes made
2.  Before/after metrics
3.  Risk impact assessment
4.  Stability impact assessment
5.  Recommendation: Accept / Reject

------------------------------------------------------------------------

# 🎯 Final Goal

Maximize structural edge while preserving:

-   Interpretability
-   Regime awareness
-   Risk discipline
-   Stability

------------------------------------------------------------------------

END OF PARAM_TUNING_SKILL.md
