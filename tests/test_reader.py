import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ReaderTests(unittest.TestCase):
    def test_reader_uses_new_installation_paths_without_database_or_devices(self):
        source = Path(__file__).resolve().parents[1]/'leitor/leitor.py'
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'STOCKFLOW_ROOT':directory}),patch('signal.signal'),contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location('isolated_reader',source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Test constructor only: never call start(), setup_database_pool() or monitor_input_events().
            with patch.object(module.StockflowQRService,'setup_logging'):
                service = module.StockflowQRService()
            self.assertEqual(Path(service.config_file),Path(directory)/'config/printers.json')
            self.assertEqual(Path(service.job_file),Path(directory)/'logs/leitor/jobs.json')
            self.assertEqual(service.db_config['pool_size'],1)
            self.assertTrue(service.db_config['use_pure'])
            self.assertEqual(service.keycode_to_char('KEY_A'),'a')
