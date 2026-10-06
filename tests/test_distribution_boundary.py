from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DistributionBoundaryTests(unittest.TestCase):
    def test_release_tree_has_product_entrypoints_without_workspace_materials(self):
        for name in ('AGENTS.md', 'CLAUDE.md', 'README.md', 'CONTRIBUTING.md'):
            self.assertTrue((ROOT / name).is_file(), name)
        for name in ('development', 'docs/roadmap.md', 'docs/initialization.md',
                     'docs/publication.md', 'technical-task-modularization.md',
                     'technical-task-agent-routing-transition.md'):
            self.assertFalse((ROOT / name).exists(), name)
        contract = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('flow1c-controller', contract)
        self.assertIn('interview-preparation', contract)
        self.assertIn('CONTRIBUTING.md', contract)
        self.assertTrue((ROOT / 'CLAUDE.md').read_text(encoding='utf-8').startswith('@AGENTS.md\n'))

    def test_static_documentation_links_resolve_within_product(self):
        files = [ROOT / name for name in ('README.md', 'AGENTS.md', 'CLAUDE.md', 'CONTRIBUTING.md')]
        files += list((ROOT / 'docs').glob('*.md')) + list((ROOT / 'wiki').glob('*.md'))
        for file in files:
            for link in re.findall(r'\]\(([^ )]+)(?:\s[^)]*)?\)', file.read_text(encoding='utf-8')):
                if link.startswith(('http:', 'https:', 'mailto:', '#')):
                    continue
                target = (file.parent / link.split('#', 1)[0]).resolve()
                with self.subTest(file=str(file.relative_to(ROOT)), link=link):
                    self.assertTrue(target.is_relative_to(ROOT), 'Documentation escapes the checkout')
                    self.assertTrue(target.exists(), 'Broken documentation link')


if __name__ == '__main__':
    unittest.main()
