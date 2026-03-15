"""Deprecated local launcher.

Use ``python src/main.py`` as the only supported startup command.
This wrapper remains only to forward old invocations to the canonical entry.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def main() -> None:
    print("DEPRECATED: use `python src/main.py`; forwarding to canonical entrypoint.")
    from src.main import main as canonical_main
    canonical_main()


if __name__ == "__main__":
    main()
