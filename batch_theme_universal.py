import re, os, glob

TEMPLATES_DIR = "/home/ubuntu/report_generos/templates"
EXCLUDE = ["error.html"]

THEME_SCRIPT = '<script>!function(){var t=localStorage.getItem(\'rg-theme\')||\'dark\';document.documentElement.setAttribute(\'data-theme\',t)}()</script>'

TOGGLE_FN = '''function toggleTheme(){var c=document.documentElement.getAttribute('data-theme'),n=c==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',n);localStorage.setItem('rg-theme',n);}'''

files = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.html")))
print(f"Found {len(files)} HTML files")

for fpath in files:
    fname = os.path.basename(fpath)
    if fname in EXCLUDE or "backup" in fname:
        print(f"  SKIP {fname}")
        continue

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    
    original = content
    
    # 1. Inject THEME_SCRIPT setelah <title>
    title_match = re.search(r'(<title>[^<]*</title>)\s*\n?', content)
    if title_match:
        title_end = title_match.end()
        after_title = content[title_end:title_end+200]
        if 'rg-theme' not in after_title and 'localStorage' not in after_title[:60]:
            content = content[:title_end] + '\n' + THEME_SCRIPT + content[title_end:]
            print(f"  [{fname}] inject theme script after <title>")
        else:
            print(f"  [{fname}] theme script already present")
    else:
        print(f"  [{fname}] WARNING: no <title>")
    
    # 2. Hapus inline CSS variable blocks
    def remove_variable_blocks(text):
        def _replace_style(m):
            sc = m.group(1)
            # Hapus :root { ... }
            sc = re.sub(r':root(?:,\s*\[data-theme="dark"\])?\s*\{[^}]*\}', '', sc)
            # Hapus [data-theme="light"] { ... }
            sc = re.sub(r'\[data-theme="light"\]\s*\{[^}]*\}', '', sc)
            # Hapus [data-theme="light"] .topbar { ... }
            sc = re.sub(r'\[data-theme="light"\]\s*\.topbar\s*\{[^}]*\}', '', sc)
            # Hapus .topbar hardcoded background
            sc = re.sub(r'\.topbar\s*\{[^}]*background[^}]*\}', '', sc)
            # Collapse empty lines
            sc = re.sub(r'\n{3,}', '\n\n', sc)
            sc_stripped = sc.strip()
            if not sc_stripped or sc_stripped == '/* Theme - match dashboard */':
                return ''
            return f'<style>{sc}</style>'
        return re.sub(r'<style>(.*?)</style>', _replace_style, text, flags=re.DOTALL)
    
    content = remove_variable_blocks(content)
    
    # 3. Ganti localStorage key: 'theme' -> 'rg-theme'
    content = content.replace("localStorage.getItem('theme')", "localStorage.getItem('rg-theme')")
    content = content.replace('localStorage.getItem("theme")', 'localStorage.getItem("rg-theme")')
    content = content.replace("localStorage.setItem('theme'", "localStorage.setItem('rg-theme'")
    content = content.replace('localStorage.setItem("theme"', 'localStorage.setItem("rg-theme"')
    
    # 4. Standardisasi toggleTheme
    if 'function toggleTheme' in content:
        content = re.sub(
            r'function\s+toggleTheme\s*\(\s*\)\s*\{[^}]*\}',
            TOGGLE_FN,
            content
        )
        print(f"  [{fname}] standardized toggleTheme()")
    
    # 5. Bersihin orphan comments
    content = content.replace('/* Theme - match dashboard */', '')
    content = content.replace('/* Topbar - match dashboard hardcoded dark */\n', '')
    
    # 6. Hapus baris kosong berlebihan
    content = re.sub(r'\n{4,}', '\n\n\n', content)
    
    if content != original:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  [{fname}] ✅ WRITTEN")
    else:
        print(f"  [{fname}] ⏭️ unchanged")

print("\n=== SELESAI ===")
