"""A green scenario list alone cannot publish a target validation receipt."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_compatibility import profile_fixture, verified_facts

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ci_target', ROOT / 'scripts/ci-target.py')
CI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CI)


class ReceiptPublication(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'receipt.json'
        self.profile, digest = profile_fixture()
        self.facts = verified_facts(self.profile, digest)
        self.receipt = self.facts.pop('receipt')

    def test_matching_evidence_is_inspected_after_writing(self):
        def inspect():
            self.assertTrue(self.path.is_file())
            return {'status': 'verified'}
        CI.write_verified_receipt(self.path, self.receipt, self.profile, self.facts, inspect)
        self.assertTrue(self.path.is_file())

    def test_new_package_tuple_with_passing_cases_cannot_publish(self):
        self.facts['target']['packages']['gnome-shell'] = '50.1-unvalidated'
        self.facts['manifest']['built_for'] = copy.deepcopy(self.facts['target'])
        self.receipt['target'] = copy.deepcopy(self.facts['target'])
        inspect = Mock()
        with self.assertRaisesRegex(RuntimeError, 'Exact target package tuple'):
            CI.write_verified_receipt(self.path, self.receipt, self.profile, self.facts, inspect)
        self.assertFalse(self.path.exists())
        inspect.assert_not_called()

    def test_post_write_failure_removes_the_receipt(self):
        for inspect in (Mock(return_value={'status': 'incompatible', 'reasons': ['runtime changed']}),
                        Mock(side_effect=OSError('inspection failed'))):
            with self.subTest(inspect=inspect), self.assertRaises((RuntimeError, OSError)):
                CI.write_verified_receipt(self.path, self.receipt, self.profile, self.facts, inspect)
            self.assertFalse(self.path.exists())
