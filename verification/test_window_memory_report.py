import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('window_memory_report',Path(__file__).with_name('window-memory-report.py'))
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)

class PeakRssUnitsTest(unittest.TestCase):
    def test_darwin_bytes_and_linux_kib_normalize_to_same_bytes(self):
        self.assertEqual(1048576,report.peak_rss_bytes("    1048576  maximum resident set size\n",'Darwin'))
        self.assertEqual(1048576,report.peak_rss_bytes("Maximum resident set size (kbytes): 1024\n",'Linux'))
    def test_missing_zero_and_wrong_host_results_are_rejected(self):
        for text,host in [("exit status 0",'Darwin'),("Maximum resident set size (kbytes): 0",'Linux'),("123 maximum resident set size",'Windows')]:
            with self.assertRaises(ValueError):report.peak_rss_bytes(text,host)
