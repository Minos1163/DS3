import os
from pathlib import Path

paths = [
    r"D:\AIDCA\AIBOT\nul",
    r"\\?\D:\\AIDCA\\AIBOT\\nul",
]

for p in paths:
    try:
        print("Trying", p)
        if os.path.exists(p):
            os.unlink(p)
            print("Unlinked", p)
        else:
            # attempt Path.unlink which may raise
            Path(p).unlink(missing_ok=True)
            print("Path.unlink called for", p)
    except Exception as e:
        print("Failed to remove", p, "->", type(e).__name__, e)

print("Done")
