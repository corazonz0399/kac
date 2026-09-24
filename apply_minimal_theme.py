import re

with open('/home/ubuntu/report_generos/templates/dashboard.html') as f:
    content = f.read()

original = content

# 1. Inject theme script setelah <title>
THEME_SCRIPT = '<script>!function(){var t=localStorage.getItem(\'rg-theme\')||\'dark\';document.documentElement.setAttribute(\'data-theme\',t)}()</script>'
title_match = re.search(r'(<title>[^<]*</title>)\s*\n?', content)
if title_match:
    title_end = title_match.end()
    after = content[title_end:title_end+100]
    if 'rg-theme' not in after:
        content = content[:title_end] + '\n' + THEME_SCRIPT + content[title_end:]
        print("✅ Injected theme script")

# 2. Ganti localStorage key
content = content.replace("localStorage.getItem('theme')", "localStorage.getItem('rg-theme')")
content = content.replace('localStorage.getItem("theme")', 'localStorage.getItem("rg-theme")')
content = content.replace("localStorage.setItem('theme'", "localStorage.setItem('rg-theme'")
content = content.replace('localStorage.setItem("theme"', 'localStorage.setItem("rg-theme"')

# 3. Standarisasi toggleTheme (kalo ada)
TOGGLE_FN = '''function toggleTheme(){var c=document.documentElement.getAttribute('data-theme'),n=c==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',n);localStorage.setItem('rg-theme',n);}'''
if 'function toggleTheme' in content:
    content = re.sub(
        r'function\s+toggleTheme\s*\(\s*\)\s*\{[^}]*\}',
        TOGGLE_FN,
        content
    )
    print("✅ Standardized toggleTheme")

if content != original:
    with open('/home/ubuntu/report_generos/templates/dashboard.html', 'w') as f:
        f.write(content)
    print("✅ Written!")
else:
    print("⚠️ No changes")
