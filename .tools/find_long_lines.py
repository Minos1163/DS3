from pathlib import Path

p = Path("src/api/binance_client.py")
for i, line_text in enumerate(p.read_text().splitlines(), 1):
    if len(line_text) > 79:
        print(i, len(line_text), line_text)
