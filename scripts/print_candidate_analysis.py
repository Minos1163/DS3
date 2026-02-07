import pandas as pd

import glob
import os

# pick latest candidate file
pattern = 'logs/dca_candidate_logs_*.csv'
files = glob.glob(pattern)
if not files:
    raise SystemExit('no candidate log files found')
path = max(files, key=os.path.getctime)
print('analyzing:', path)
df = pd.read_csv(path, parse_dates=['timestamp'])
# normalize boolean columns (they may be strings)
for c in ['became_candidate','selected','opened']:
    if df[c].dtype == object:
        df[c]=df[c].map({'True':True,'False':False}).fillna(df[c])

# compute base/applied decisions
df['base_decision']=df['score']>=df['base_threshold']
df['applied_decision']=df['score']>=df['applied_threshold']

# rows where decisions differ
blocked = df[(df['base_decision']==True) & (df['applied_decision']==False)]
allowed = df[(df['base_decision']==False) & (df['applied_decision']==True)]
selected_or_opened = df[(df['selected']==True)|(df['opened']==True)]

print('file:', path)
print('total rows:', len(df))
print('selected/opened rows:', len(selected_or_opened))
print('blocked by multiplier (base True -> applied False):', len(blocked))
print('allowed by multiplier (base False -> applied True):', len(allowed))

print('\nExamples (blocked) head 10:')
print(blocked.head(10).to_csv(index=False))

print('\nExamples (allowed) head 10:')
print(allowed.head(10).to_csv(index=False))

print('\nSelected/opened sample 20:')
print(selected_or_opened.head(20).to_csv(index=False))

# aggregate by regime how many blocked/allowed
print('\nBlocked by regime:')
if len(blocked):
    print(blocked.groupby('regime').size().to_string())
else:
    print('0')
print('\nAllowed by regime:')
if len(allowed):
    print(allowed.groupby('regime').size().to_string())
else:
    print('0')
