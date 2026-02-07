import io
p='d:/AIDCA/AIBOT/src/main.py'
with io.open(p,'r',encoding='utf-8') as f:
    for i,l in enumerate(f,1):
        if i<=200:
            print(f"{i:04d}: {l.rstrip()!r}")
        else:
            break
