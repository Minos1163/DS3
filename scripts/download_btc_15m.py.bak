import os
import time
import requests
import csv
from datetime import datetime, timedelta

# 下载 Binance BTCUSDT 15m 历史 K 线（过去 120 天），保存为 data/BTCUSDT_15m_120d.csv
API = 'https://api.binance.com/api/v3/klines'
SYMBOL = 'BTCUSDT'
INTERVAL = '15m'
DAYS = 120
OUT_FILE = 'data/BTCUSDT_15m_120d.csv'

def ms(dt):
    return int(dt.timestamp() * 1000)

def download():
    os.makedirs('data', exist_ok=True)
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=DAYS)
    start_ms = ms(start_dt)
    end_ms = ms(end_dt)

    all_rows = []
    limit = 1000
    cur_start = start_ms
    print(f"Downloading {SYMBOL} {INTERVAL} from {start_dt} to {end_dt}")
    while True:
        params = {
            'symbol': SYMBOL,
            'interval': INTERVAL,
            'startTime': cur_start,
            'endTime': end_ms,
            'limit': limit
        }
        resp = requests.get(API, params=params, timeout=30)
        if resp.status_code != 200:
            print('HTTP', resp.status_code, resp.text)
            break
        data = resp.json()
        if not data:
            break
        for item in data:
            # item: [openTime, open, high, low, close, volume, closeTime, ...]
            row = {
                'timestamp': datetime.utcfromtimestamp(item[0]/1000).strftime('%Y-%m-%d %H:%M:%S'),
                'open': float(item[1]),
                'high': float(item[2]),
                'low': float(item[3]),
                'close': float(item[4]),
                'volume': float(item[5])
            }
            all_rows.append(row)
        # Binance returns up to `limit` rows; advance start to last returned openTime + interval
        last_open = data[-1][0]
        # advance by one interval (15 minutes)
        cur_start = last_open + 15 * 60 * 1000
        # avoid infinite loop
        if cur_start >= end_ms:
            break
        # rate limit safety
        time.sleep(0.2)

    # dedupe and sort
    if all_rows:
        # write to csv
        seen = set()
        rows = []
        for r in all_rows:
            if r['timestamp'] in seen:
                continue
            seen.add(r['timestamp'])
            rows.append(r)
        rows.sort(key=lambda x: x['timestamp'])
        with open(OUT_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['timestamp','open','high','low','close','volume'])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Saved {len(rows)} rows to {OUT_FILE}")
    else:
        print('No data downloaded')

if __name__ == '__main__':
    download()
