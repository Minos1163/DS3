#!/usr/bin/env python3
from analyze_log import main

# Lightweight wrapper: moved from repository root
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


if __name__ == '__main__':
    main()
