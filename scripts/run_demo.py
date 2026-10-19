"""Use existing component dependencies read-only, without .pth or old QA fixtures."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'src'))
for name in ('.venv-demo','.venv-web','.venv-mcp','.venv-agent','.venv-build'):
    site=ROOT/name/'Lib/site-packages'
    if site.is_dir(): sys.path.append(str(site))
    for child in ('win32','win32/lib'):
        if (site/child).is_dir(): sys.path.append(str(site/child))

if __name__=='__main__':
    from campuspulse_recovery.demo.__main__ import main
    main()
