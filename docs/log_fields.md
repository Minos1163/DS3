# Logs Field Checklist for Risk Gate & Trades

- action: ENTER|EXIT|HOLD
- enter: boolean (risk gate recommends entering)
- exit: boolean (risk gate recommends exiting)
- score: float, risk score from evaluation
- details: nested dict with trend/momentum/volatility/drawdown/atr hints
- state: raw state snapshot sent to risk gate
- raw: full raw risk gate response
- timestamp_utc: ISO timestamp in UTC
- asset: traded asset symbol
- amount: trade amount
- price: trade price (if applicable)
- order_id: generated order identifier (if ENTER)
- status: trade status (ordered/closed/no_action)

This checklist ensures future log exports capture all essential signals for comparing risk strategies.
