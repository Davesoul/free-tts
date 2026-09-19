import re

src = open('server.py', encoding='utf-8').read()
m = re.search(r'def _build_static\(\):.*?\n    return r"""(.*)"""', src, re.S)
html = '<!doctype html>\n<html lang="en">\n' + m.group(1) + '\n</html>'
open('static/index.html', 'w', encoding='utf-8').write(html)

c = open('static/index.html').read()
print('written', len(c), 'bytes')
print('reader.readAsDataURL:', 'reader.readAsDataURL' in c)
print('recordBtn:', 'id="recordBtn"' in c)
print('else{voice:', 'else{voice' in c)
print('vs.map (bad):', 'vs.map' in c)
