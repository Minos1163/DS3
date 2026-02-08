import argparse

from src.data.klines_downloader import download_public_klines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols like ETHUSDT SOLUSDT")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    for sym in args.symbols:
        out_file = f"data/{sym}_{args.interval}_{args.days}d.csv"
        df = download_public_klines(sym, args.interval, args.days, out_file)
        if df is None:
            print(f"No data downloaded for {sym}")
        else:
            print(f"Saved {len(df)} rows to {out_file}")


if __name__ == "__main__":
    main()
