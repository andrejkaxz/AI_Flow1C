from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProductIdentityTests(unittest.TestCase):
    def test_cli_runs_outside_checkout_without_initializing_user_state(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts/flow1c.py'), '--help'],
                cwd=folder, capture_output=True, text=True, encoding='utf-8', timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Flow1C', result.stdout)
            self.assertIn('agent-begin', result.stdout)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_controller_and_stage_contracts_use_product_namespace(self):
        config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))
        self.assertEqual(config['default_agent'], 'flow1c-controller')
        self.assertEqual(config['permission']['flow1c_*'], 'allow')
        stages = json.loads((ROOT / 'config/stages.json').read_text(encoding='utf-8'))
        self.assertIn('workflow-review', stages['operations'])
        for name, stage in stages['operations'].items():
            with self.subTest(operation=name):
                self.assertTrue(stage['skill'].startswith('flow1c-'))
                self.assertTrue((ROOT / '.agents/skills' / stage['skill'] / 'SKILL.md').is_file())
                self.assertTrue(all(tool.startswith('flow1c_') for tool in stage['allowed_tools']))

    def test_product_configuration_does_not_contain_machine_paths(self):
        config = json.loads((ROOT / '.flow1c.json').read_text(encoding='utf-8'))
        self.assertEqual(config['project']['name'], 'Flow1C')
        self.assertEqual(config['gitea']['token_env'], 'FLOW1C_GITEA_TOKEN')
        self.assertNotIn('local', config)
        self.assertNotIn('C:\\Users\\', json.dumps(config))


if __name__ == '__main__':
    unittest.main()
