import json
import subprocess
import sys

cmd = [r".venv\Scripts\ruff.exe", "check", "--line-length", "120", "--output-format", "json", "."]
try:
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
except Exception as e:
    print('ERROR running ruff:', e)
    sys.exit(1)

if p.returncode == 0 and not p.stdout.strip():
    print('No issues')
    sys.exit(0)

try:
    data = json.loads(p.stdout)
except Exception as e:
    print('Failed to parse JSON output from ruff:', e)
    print('Raw output:\n', p.stdout[:10000])
    sys.exit(1)

counts = {}
for item in data:
    fn = item.get('filename')
    counts[fn] = counts.get(fn, 0) + 1

sorted_files = sorted(counts.items(), key=lambda x: x[1], reverse=True)
print('Top files by ruff issue count:')
for i, (fn, cnt) in enumerate(sorted_files[:20], 1):
    print(f'{i:2d}. {fn} — {cnt} issues')

# Also print total issues
print('\nTotal issues:', len(data))
