import importlib.util
from pathlib import Path
import unittest

class CoordinateProbeImportTest(unittest.TestCase):
    def test_import_has_no_cli_or_measurement_side_effects(self):
        path=Path(__file__).with_name('coordinate-memory-report.py')
        spec=importlib.util.spec_from_file_location('coordinate_probe',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))
