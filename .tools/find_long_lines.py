from pathlib import Path
p=Path('src/api/binance_client.py')
for i,l in enumerate(p.read_text().splitlines(),1):
    if len(l)>79:
        print(i,len(l),l)
