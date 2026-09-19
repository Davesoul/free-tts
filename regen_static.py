import re

src = open('D:/Coding/free-tts/server.py', encoding='utf-8').read()
m = re.search(r'def _build_static\(\):.*?\n    return r"""(.*)"""', src, re.S)
if not m:
    raise SystemExit('ERROR: _build_static not found')

html = m.group(1)
open('D:/Coding/free-tts/static/index.html', 'w', encoding='utf-8').write(html)

c = open('D:/Coding/free-tts/static/index.html').read()
print('static/index.html regenerated:', len(c), 'bytes')
checks = {
    'reader.readAsDataURL': 'reader.readAsDataURL' in c,
    'id="recordBtn"': 'id="recordBtn"' in c,
    'else{voice': 'else{voice' in c,
    'vs.map (bad)': 'vs.map' in c,
}
for k, v in checks.items():
    print(f'  {k}: {v}')
