# Run generated OOS validation tasks
$ErrorActionPreference = 'Stop'
Write-Host 'Running: SOLUSDT_15m_60d 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_60d.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: SOLUSDT_15m_120d 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_120d.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: SOLUSDT_15m_120d 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_120d.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: SOLUSDT_15m_60d 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_60d.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_MELANIAUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_MELANIAUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_MELANIAUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_MELANIAUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_MELANIAUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_MELANIAUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_MELANIAUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_MELANIAUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: SOLUSDT_15m_30d 0.02 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_30d.csv" --stop_loss 0.02 --position 0.4 
Write-Host 'Running: SOLUSDT_15m_30d 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_30d.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: tmp_resampled_CHILLGUYUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_CHILLGUYUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_CHILLGUYUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_CHILLGUYUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_DOGEUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_DOGEUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_DOGEUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_DOGEUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_PNUTUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_PNUTUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_PNUTUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_PNUTUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_MEMEUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_MEMEUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_MEMEUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_MEMEUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_BOMEUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_BOMEUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_BOMEUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_BOMEUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_SOLUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_SOLUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_SOLUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_SOLUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_PENGUUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_PENGUUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_PENGUUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_PENGUUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_ADAUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_ADAUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_ADAUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_ADAUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: BTCUSDT_15m_120d 0.02 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\BTCUSDT_15m_120d.csv" --stop_loss 0.02 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_PEOPLEUSDT_5m_30d_15m_15m 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_PEOPLEUSDT_5m_30d_15m_15m.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: tmp_resampled_PEOPLEUSDT_5m_30d_15m 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_PEOPLEUSDT_5m_30d_15m.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_TURBOUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_TURBOUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_TURBOUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_TURBOUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_NOTUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_NOTUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_NOTUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_NOTUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_REZUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_REZUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_REZUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_REZUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_BANANAS31USDT_5m_30d_15m 0.02 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_BANANAS31USDT_5m_30d_15m.csv" --stop_loss 0.02 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_BANANAS31USDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_BANANAS31USDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_ARBUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_ARBUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_ARBUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_ARBUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_ACTUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_ACTUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_ACTUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_ACTUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_BTCUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_BTCUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_BTCUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_BTCUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_AVAXUSDT_5m_30d_15m 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_AVAXUSDT_5m_30d_15m.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_AVAXUSDT_5m_30d_15m_15m 0.025 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_AVAXUSDT_5m_30d_15m_15m.csv" --stop_loss 0.025 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_WIFUSDT_5m_30d_15m_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_WIFUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_WIFUSDT_5m_30d_15m 0.015 0.4'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_WIFUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.4 
Write-Host 'Running: tmp_resampled_tmp_resampled_TRXUSDT_5m_30d_15m_15m 0.02 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_TRXUSDT_5m_30d_15m_15m.csv" --stop_loss 0.02 --position 0.3 
Write-Host 'Running: tmp_resampled_TRXUSDT_5m_30d_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_TRXUSDT_5m_30d_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_ALTUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_ALTUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_ALTUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_ALTUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_ETHUSDT_5m_30d_15m_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_ETHUSDT_5m_30d_15m_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_ETHUSDT_5m_30d_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_ETHUSDT_5m_30d_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_BRETTUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_BRETTUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_BRETTUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_BRETTUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_GOATUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_GOATUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_GOATUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_GOATUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_SOLUSDT_5m_15d_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_SOLUSDT_5m_15d_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_SOLUSDT_5m_15d_15m_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_SOLUSDT_5m_15d_15m_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_SPXUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_SPXUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_SPXUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_SPXUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_OPUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_OPUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_OPUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_OPUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_DEGENUSDT_5m_30d_15m 0.02 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_DEGENUSDT_5m_30d_15m.csv" --stop_loss 0.02 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_DEGENUSDT_5m_30d_15m_15m 0.02 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_DEGENUSDT_5m_30d_15m_15m.csv" --stop_loss 0.02 --position 0.3 
Write-Host 'Running: tmp_resampled_TRUMPUSDT_5m_30d_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_TRUMPUSDT_5m_30d_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_TRUMPUSDT_5m_30d_15m_15m 0.025 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_TRUMPUSDT_5m_30d_15m_15m.csv" --stop_loss 0.025 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_DOGSUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_DOGSUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_DOGSUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_DOGSUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_BNBUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_BNBUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_BNBUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_BNBUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_TONUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_TONUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_TONUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_TONUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_XRPUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_XRPUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_XRPUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_XRPUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_MEWUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_MEWUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_MEWUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_MEWUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_BANUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_BANUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_BANUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_BANUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_HIPPOUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_HIPPOUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_HIPPOUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_HIPPOUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_KOMAUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_KOMAUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_KOMAUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_KOMAUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: SOLUSDT_15m_15d 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\SOLUSDT_15m_15d.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_TSTUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_TSTUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_TSTUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_TSTUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_FARTCOINUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_FARTCOINUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_FARTCOINUSDT_5m_30d_15m 0.02 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_FARTCOINUSDT_5m_30d_15m.csv" --stop_loss 0.02 --position 0.3 
Write-Host 'Running: tmp_resampled_tmp_resampled_PIPPINUSDT_5m_30d_15m_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_tmp_resampled_PIPPINUSDT_5m_30d_15m_15m.csv" --stop_loss 0.015 --position 0.3 
Write-Host 'Running: tmp_resampled_PIPPINUSDT_5m_30d_15m 0.015 0.3'
& .venv\Scripts\python.exe scripts\validate_single_best.py --file "data\tmp_resampled_PIPPINUSDT_5m_30d_15m.csv" --stop_loss 0.015 --position 0.3 
