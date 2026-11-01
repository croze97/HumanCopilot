"""Standalone installation never replaces unrelated files or enables the lane."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.env={**os.environ,'HOME':str(self.home),'CODEX_HOME':str(self.home/'.codex'),
            'HC_CONTROL_FILE':str(self.home/'control.json'),'HC_SPOOL':str(self.home/'spool'),
            'HC_RECEIPT_KEY_FILE':str(self.home/'receipt-key')}
    def run_tool(self,file,*args,ok=True):
        p=subprocess.run([sys.executable,str(ROOT/file),*args],env=self.env,capture_output=True,text=True,timeout=60)
        self.assertEqual(p.returncode==0,ok,p.stderr)
        return p
    def test_install_toggle_and_uninstall_preserve_user_content(self):
        instructions=self.home/'.claude/CLAUDE.md'
        instructions.parent.mkdir();instructions.write_text('# Personal instructions\n')
        self.run_tool('install.py')
        self.assertTrue((self.home/'bin/copilot-toggle').is_symlink())
        self.assertTrue(instructions.read_text().startswith('# Personal instructions\n'))
        self.assertEqual(json.loads((self.home/'.claude.json').read_text()), {})
        self.run_tool('toggle.py','on')
        config=json.loads((self.home/'.claude.json').read_text())
        self.assertIn('human-copilot',config['mcpServers'])
        self.assertTrue(json.loads((self.home/'control.json').read_text())['enabled'])
        self.run_tool('install.py',ok=False)
        self.run_tool('toggle.py','off')
        self.assertFalse(json.loads((self.home/'control.json').read_text())['enabled'])
        self.run_tool('install.py','--uninstall')
        self.assertFalse((self.home/'bin/copilot-toggle').exists())
        self.assertTrue(instructions.read_text().startswith('# Personal instructions\n'))
    def test_unrelated_destination_refuses_before_any_configuration_write(self):
        target=self.home/'bin/copilot-tui';target.parent.mkdir();target.write_text('keep me')
        self.run_tool('install.py',ok=False)
        self.assertEqual(target.read_text(),'keep me')
        self.assertFalse((self.home/'.claude').exists())

if __name__=='__main__':unittest.main()
