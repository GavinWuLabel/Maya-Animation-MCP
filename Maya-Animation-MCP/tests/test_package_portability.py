"""Package/path tests without Maya, sockets or real application settings."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from maya_side.standalone_service import parse_options
class PortabilityTests(unittest.TestCase):
    def test_default_paths_are_package_relative(self):
        with patch.dict(os.environ,{},clear=True):a=parse_options([])
        self.assertEqual(Path(a.output_root),ROOT/'output')
        self.assertEqual(Path(a.work_root),ROOT/'runtime'/'background')
        self.assertFalse(a.allow_python)
    def test_explicit_paths_allow_spaces_unicode(self):
        with tempfile.TemporaryDirectory(prefix='Maya 测试 ')as temp:
            a=parse_options(['--output-root',temp,'--work-root',temp,'--connection-file',str(Path(temp)/'connection.json'),'--allow-python'])
            self.assertEqual(Path(a.output_root),Path(temp));self.assertTrue(a.allow_python)
    def test_output_environment_override(self):
        with tempfile.TemporaryDirectory()as temp,patch.dict(os.environ,{'MAYA_MCP_OUTPUT_ROOT':temp}):
            self.assertEqual(parse_options([]).output_root,temp)
    def test_invalid_port_rejected(self):
        with self.assertRaises(SystemExit):parse_options(['--port','65536'])
    def test_generated_snippets_do_not_change_settings(self):
        spec=importlib.util.spec_from_file_location('configure_package',str(ROOT/'scripts'/'configure.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='Maya 包 ')as temp:
            module.ROOT=Path(temp);module.main()
            self.assertIn('background_connection.json',(Path(temp)/'codex-mcp-background.toml').read_text(encoding='utf8'))
            self.assertIn('start_in_maya.py',(Path(temp)/'START_MAYA_BRIDGE.mel').read_text(encoding='utf8'))
            self.assertEqual(len(list(Path(temp).iterdir())),3)
if __name__=='__main__':unittest.main()
