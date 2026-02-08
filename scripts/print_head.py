import io

p = "d:/AIDCA/AIBOT/src/main.py"
with io.open(p, "r", encoding="utf-8") as f:
    for i, line_text in enumerate(f, 1):
        if i <= 200:
            print(f"{i:04d}: {line_text.rstrip()!r}")
        else:
            break
