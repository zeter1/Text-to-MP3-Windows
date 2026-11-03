"""Engine manager tests on synthetic source/state only; no installs into real runtime."""
from pathlib import Path
import hashlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix='tts-manager-', dir=SOURCE)
ROOT = Path(MIRROR.name)
(ROOT / 'tts_app').mkdir()
for file in (SOURCE / 'tts_app').glob('*.py'):
    shutil.copyfile(file, ROOT / 'tts_app' / file.name)
sys.path.insert(0, str(ROOT))
from tts_app import engine_installation as owner
from tts_app.runtime import STORAGE_BASE_DIR
assert STORAGE_BASE_DIR.resolve() == ROOT.resolve()


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.fixture.name)
        self.engines = self.root / 'Движки речи'
        self.engines.mkdir()
        self.model = self.engines / 'silero.pt'
        self.edge = self.engines / 'edge'
        self.patches = [patch.object(owner, name, value) for name, value in {
            'ENGINE_ROOT': self.engines, 'MODEL_PATH': self.model, 'EDGE_RUNTIME': self.edge,
            'OPTIONAL': self.engines / 'optional', 'BACKUPS': self.root / '_maintenance_backups' / 'removed_engines',
        }.items()]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.fixture.cleanup()

    def test_install_existing_never_reads_network_or_replaces_bytes(self):
        self.model.write_bytes(b'existing model sentinel')
        self.edge.mkdir()
        (self.edge / 'private.txt').write_bytes(b'existing edge sentinel')
        with patch.object(owner.urllib.request, 'urlopen', side_effect=AssertionError('network')), \
             patch.object(owner, 'pip_install', side_effect=AssertionError('pip')):
            owner.install_engine('silero')
            owner.install_engine('edge')
        self.assertEqual(self.model.read_bytes(), b'existing model sentinel')
        self.assertEqual((self.edge / 'private.txt').read_bytes(), b'existing edge sentinel')

    def test_remove_preserves_recovery_and_unrelated_files(self):
        self.model.write_bytes(b'existing model sentinel')
        self.edge.mkdir()
        (self.edge / 'keep.txt').write_bytes(b'keep')
        owner.remove_engine('silero')
        self.assertFalse(self.model.exists())
        backups = list(owner.BACKUPS.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'existing model sentinel')
        self.assertEqual((self.edge / 'keep.txt').read_bytes(), b'keep')
        self.assertEqual(owner.remove_engine('silero'), 'Уже отсутствует')

    def test_unknown_engine_rejected_before_file_changes(self):
        for engine in ('sapi', '../outside', 'global'):
            with self.assertRaises(ValueError): owner.remove_engine(engine)
            with self.assertRaises(ValueError): owner.install_engine(engine)
        self.assertEqual(list(self.engines.iterdir()), [])

    def test_staged_install_does_not_touch_global_environment(self):
        def fake_pip(packages, destination):
            self.assertEqual(packages, ['edge-tts==7.3.1'])
            self.assertTrue(destination.resolve().is_relative_to(self.engines))
            (destination / 'edge_tts').mkdir(parents=True)
            (destination / 'edge_tts' / '__init__.py').write_text('# fixture')
        with patch.object(owner, 'pip_install', side_effect=fake_pip):
            owner.install_engine('edge')
        self.assertTrue((self.edge / 'edge_tts' / '__init__.py').is_file())
        self.assertEqual(list(self.engines.iterdir()), [self.edge])

    def test_failed_install_never_publishes_partial_engine(self):
        def partial(_packages, destination):
            destination.mkdir()
            (destination / 'partial').write_bytes(b'partial')
            raise RuntimeError('synthetic download failure')
        with patch.object(owner, 'pip_install', side_effect=partial):
            with self.assertRaises(RuntimeError): owner.install_engine('edge')
        self.assertFalse(self.edge.exists())
        self.assertEqual(list(self.engines.iterdir()), [])


if __name__ == '__main__':
    try: unittest.main()
    finally: MIRROR.cleanup()
