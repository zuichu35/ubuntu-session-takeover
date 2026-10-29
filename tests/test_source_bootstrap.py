"""Isolated bootstrap tests: no network, compiler, privileged operation or live service."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpstreamFetch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache'
        repo = self.cache / 'project'
        repo.mkdir(parents=True)
        self.fetch = load('fetch-upstreams')
        subprocess.run(['git', 'init', '--quiet', str(repo)], check=True)
        (repo / 'source').write_text('pinned source\n')
        self.fetch.git(repo, 'add', 'source')
        self.fetch.git(repo, '-c', 'user.name=Bootstrap Test', '-c', 'user.email=test@example.invalid',
                       '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'fixture')
        self.revision = self.fetch.git(repo, 'rev-parse', 'HEAD')
        self.fetch.UPSTREAMS = (('project', 'https://example.invalid/project.git', self.revision),)
        self.vendor = self.root / 'vendor'

    def test_pinned_objects_ignore_dirty_cache_and_preserve_dirty_destination(self):
        (self.cache / 'project/source').write_text('uncommitted cache edit')
        self.fetch.fetch(self.vendor, self.cache)
        source = self.vendor / 'project/source'
        self.assertEqual(source.read_text(), 'pinned source\n')
        source.write_text('local patch')
        result = self.fetch.fetch(self.vendor, self.cache)
        self.assertTrue(result[0]['dirty'])
        self.assertEqual(source.read_text(), 'local patch')

    def test_wrong_origin_is_not_replaced(self):
        self.fetch.fetch(self.vendor, self.cache)
        repo = self.vendor / 'project'
        self.fetch.git(repo, 'remote', 'set-url', 'origin', 'https://example.invalid/other.git')
        with self.assertRaisesRegex(RuntimeError, 'another origin'):
            self.fetch.fetch(self.vendor, self.cache)
        self.assertEqual((repo / 'source').read_text(), 'pinned source\n')

    def test_wrong_revision_is_not_reset(self):
        self.fetch.fetch(self.vendor, self.cache)
        repo = self.vendor / 'project'
        (repo / 'source').write_text('second commit')
        self.fetch.git(repo, 'add', 'source')
        self.fetch.git(repo, '-c', 'user.name=Bootstrap Test', '-c', 'user.email=test@example.invalid',
                       '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'changed')
        with self.assertRaisesRegex(RuntimeError, 'another revision'):
            self.fetch.fetch(self.vendor, self.cache)
        self.assertEqual((repo / 'source').read_text(), 'second commit')

    def test_check_missing_does_not_create_vendor(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing upstream'):
            self.fetch.fetch(self.vendor, check=True)
        self.assertFalse(self.vendor.exists())

    def test_symlink_vendor_is_rejected(self):
        self.vendor.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.fetch.fetch(self.vendor, self.cache)
        self.assertEqual((self.cache / 'project/source').read_text(), 'pinned source\n')


class BuildBoundaries(unittest.TestCase):
    def test_install_manifest_rejects_escape_and_symlink(self):
        builder = load('build-grd')
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'prefix'
            prefix.mkdir()
            builder.validate_install_targets({'a': [str(prefix / 'a'), str(prefix / 'b')]}, prefix)
            for destination in ('/usr/lib/sysusers.d/grd.conf', str(prefix / '../escape'), 'relative'):
                with self.assertRaises(RuntimeError):
                    builder.validate_install_targets({'a': destination}, prefix)
            (prefix / 'link').symlink_to(Path(folder), target_is_directory=True)
            with self.assertRaises(RuntimeError):
                builder.validate_install_targets({'a': str(prefix / 'link/escape')}, prefix)
            alias = Path(folder) / 'alias'
            alias.symlink_to(prefix, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                builder.validate_install_targets({'a': str(alias / 'data')}, alias)

    def test_plan_modes_need_no_inputs_and_write_nothing(self):
        for name in ('fetch-upstreams', 'record-native-abi', 'build-grd',
                     'build-greeter-candidate', 'build-greeter-fix', 'package-native'):
            with self.subTest(script=name), tempfile.TemporaryDirectory() as folder:
                module = load(name)
                module.ROOT = Path(folder)
                with patch('sys.argv', [name, '--plan']), contextlib.redirect_stdout(io.StringIO()) as output:
                    module.main()
                self.assertIsInstance(json.loads(output.getvalue()), (dict, list))
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_abi_snapshot_reads_public_inputs_and_missing_input_fails(self):
        abi = load('record-native-abi')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'system-file'
            source.write_bytes(b'public fixture')
            before = source.stat()
            self.assertEqual(len(abi.snapshot([source])[str(source)]), 64)
            self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
            with self.assertRaises(FileNotFoundError):
                abi.snapshot([source, Path(folder) / 'missing'])


if __name__ == '__main__':
    unittest.main()
