#!/usr/bin/env python3
"""Install user-owned command/extension links; leave registration disabled."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import toggle

ROOT = Path(__file__).resolve().parent
UUID = 'copilot-overlay@croze'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true', help='remove only links owned by this checkout; run copilot-toggle off first')
    args = parser.parse_args()
    if toggle.capability_enabled():
        raise RuntimeError("disable the lane with copilot-toggle off before installing or uninstalling")
    home = Path.home()
    links = {home/'bin'/name: ROOT/file for name,file in (
        ('copilot-toggle','toggle.py'),('copilot-tui','tui.py'),('copilot-open','open_window.py'))}
    links[home/'.local/share/gnome-shell/extensions'/UUID] = ROOT/'overlay'/UUID
    for dest,source in links.items():
        if dest.is_symlink() and dest.resolve() == source.resolve():
            continue
        if dest.exists() or dest.is_symlink():
            raise RuntimeError(f'refusing to replace unrelated path: {dest}')
    if args.uninstall:
        for dest in links:
            if dest.is_symlink():dest.unlink()
        print('Owned command and extension links removed; personal configuration/state retained.')
        return
    # Prepare the first managed OFF block without replacing existing instructions.
    doctrine = Path(toggle.doctrine_path())
    rendered = toggle._render_doctrine(str(doctrine), False, True)
    config = Path(toggle.claude_json_path())
    if config.exists():
        data = json.loads(config.read_text())
        if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
            raise ValueError('Claude configuration must contain an object of MCP servers')
    mode = stat.S_IMODE(doctrine.stat().st_mode) if doctrine.exists() else 0o600
    subprocess.run(['glib-compile-schemas',str(ROOT/'overlay'/UUID/'schemas')],check=True)
    if not config.exists():toggle._atomic_write(str(config), "{}\n", 0o600)
    toggle._atomic_write(str(doctrine), rendered, mode)
    for dest,source in links.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_symlink():dest.symlink_to(source)
    print('Installed. Add ~/bin to PATH. Enable the GNOME extension after login, then run copilot-toggle on.')
    print('No MCP registration or desktop extension activation was changed by this installer.')

if __name__ == '__main__':
    try:main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'install: {exc}',file=sys.stderr);sys.exit(1)
