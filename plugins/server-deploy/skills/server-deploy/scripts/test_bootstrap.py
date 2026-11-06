"""Offline bootstrap invariants; no production connections or credentials."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import paramiko

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).with_name("bootstrap_ssh.py"))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_target_defaults_and_custom_port(self):
        self.assertEqual(bootstrap.target({"SERVER": "example.test"}), ("example.test", 22, "root"))
        self.assertEqual(bootstrap.target({"SERVER": "ssh://deploy@example.test:2222"}), ("example.test", 2222, "deploy"))
        self.assertEqual(bootstrap.target({"SERVER": "example.test", "SSH_USER": "ops", "SSH_PORT": "2200"}), ("example.test", 2200, "ops"))

    def test_unsafe_or_malformed_target_rejected(self):
        for value in ("", "ssh://user:secret@example.test", "https://example.test", "ssh://example.test/path", "example.test:99999"):
            with self.subTest(value=value), self.assertRaises((bootstrap.BootstrapError, ValueError)):
                bootstrap.target({"SERVER": value})

    def test_unknown_host_does_not_save(self):
        key = paramiko.RSAKey.generate(1024)
        client = Mock()
        with tempfile.TemporaryDirectory() as directory:
            known = Path(directory) / "known_hosts"
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.HostPolicy(known, None, False).missing_host_key(client, "test", key)
            client.save_host_keys.assert_not_called()
            self.assertFalse(known.exists())

    def test_fingerprint_mismatch_never_trusts(self):
        client = Mock()
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.HostPolicy(Path("unused"), "SHA256:wrong", True).missing_host_key(client, "test", paramiko.RSAKey.generate(1024))
        client.save_host_keys.assert_not_called()

    def test_verified_fingerprint_is_saved(self):
        key = paramiko.RSAKey.generate(1024)
        with tempfile.TemporaryDirectory() as directory:
            known = Path(directory) / "known_hosts"
            client = paramiko.SSHClient()
            bootstrap.HostPolicy(known, bootstrap.fingerprint(key), False).missing_host_key(client, "test", key)
            loaded = paramiko.HostKeys(str(known))
            self.assertTrue(loaded.check("test", key))
            self.assertFalse(loaded.check("test", paramiko.RSAKey.generate(1024)))

    def test_real_keygen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "test_ed25519"
            first = bootstrap.ensure_key(key, "test-project")
            digest = bootstrap.hashlib.sha256(key.read_bytes()).digest()
            self.assertEqual(first, bootstrap.ensure_key(key, "test-project"))
            self.assertEqual(digest, bootstrap.hashlib.sha256(key.read_bytes()).digest())
            Path(str(key) + ".pub").unlink()
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.ensure_key(key, "test-project")
            self.assertEqual(digest, bootstrap.hashlib.sha256(key.read_bytes()).digest())

    def test_password_never_in_process_arguments(self):
        # Local commands generate keys only; password transport is a Python API call.
        with patch.object(bootstrap.paramiko, "SSHClient") as factory:
            client = factory.return_value
            client.get_transport.return_value.get_remote_server_key.return_value = paramiko.RSAKey.generate(1024)
            client.save_host_keys.side_effect = lambda filename: Path(filename).touch()
            with tempfile.TemporaryDirectory() as directory:
                bootstrap.connect("test", 22, "root", Path(directory)/"known", None, False, password="secret-fixture")
            self.assertEqual(client.connect.call_args.kwargs["password"], "secret-fixture")
            self.assertFalse(client.connect.call_args.kwargs["allow_agent"])
            self.assertFalse(client.connect.call_args.kwargs["look_for_keys"])


if __name__ == "__main__":
    unittest.main()
