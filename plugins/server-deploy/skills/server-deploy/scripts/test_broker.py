"""Regression tests for the restricted broker's command parser (no server access)."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

if os.name == 'nt':
    for name in ('pwd', 'grp', 'fcntl'):
        sys.modules[name] = types.ModuleType(name)
spec = importlib.util.spec_from_file_location('sdp_core', Path(__file__).resolve().parents[1] / 'server' / 'sdp_core.py')
broker = importlib.util.module_from_spec(spec)
sys.modules['sdp_core'] = broker
spec.loader.exec_module(broker)


class ParserTests(unittest.TestCase):
    def test_project_can_use_only_ipv4_when_ipv6_port_is_owned_elsewhere(self):
        project = types.SimpleNamespace(name='crm-qurlish')
        route = {'routes': [{'path': '/', 'port': 20000}], 'ipv6': False}
        with patch.object(broker, 'has_ipv6', return_value=True):
            self.assertNotIn('[::]', broker.render_site(project, 'api.example.test', route, False))
            route.pop('ipv6')
            self.assertIn('[::]', broker.render_site(project, 'api.example.test', route, False))

    def test_exec_keeps_subcommand_and_program_separate(self):
        args = broker.build_parser().parse_args(['exec', 'api', '--', 'alembic', 'current'])
        self.assertEqual(args.cmd, 'exec')
        self.assertEqual(args.command, ['alembic', 'current'])

    def test_run_keeps_subcommand_and_program_separate(self):
        args = broker.build_parser().parse_args(['run', 'api', '--', 'python', '-m', 'app.bootstrap'])
        self.assertEqual(args.cmd, 'run')
        self.assertEqual(args.command, ['python', '-m', 'app.bootstrap'])


if __name__ == '__main__':
    unittest.main()
