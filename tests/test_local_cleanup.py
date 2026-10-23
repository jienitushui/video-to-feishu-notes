import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vfnlib.local_cleanup import apply_cleanup, canonical_version_dir, plan_cleanup


class LocalCleanupTests(unittest.TestCase):
    def test_preview_and_apply_keep_current_and_unknown_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for version in (1, 2, 3):
                path = canonical_version_dir(root, 'rec1', version)
                path.mkdir(parents=True)
                (path / 'marker.txt').write_text(str(version), encoding='utf-8')
            legacy = root / 'rec1-v2'
            legacy.mkdir()
            plan = plan_cleanup(root, 'rec1', 3, '已完成')
            self.assertEqual(plan['candidates'], [str(root / 'rec1' / 'v1'), str(root / 'rec1' / 'v2')])
            self.assertIn(str(root / 'rec1' / 'v3'), plan['current'])
            self.assertIn(str(legacy), plan['skipped'])
            result = apply_cleanup(plan)
            self.assertTrue(result['applied'])
            self.assertFalse((root / 'rec1' / 'v1').exists())
            self.assertFalse((root / 'rec1' / 'v2').exists())
            self.assertTrue((root / 'rec1' / 'v3').exists())
            self.assertTrue(legacy.exists())

    def test_incomplete_task_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = canonical_version_dir(root, 'rec1', 1)
            path.mkdir(parents=True)
            plan = plan_cleanup(root, 'rec1', 2, '处理中')
            result = apply_cleanup(plan)
            self.assertFalse(result['applied'])
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
