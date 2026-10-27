"""Updater failure boundaries, without a Docker daemon or a live database."""
import importlib.machinery
import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "auto-update"
loader = importlib.machinery.SourceFileLoader("geologs_auto_update", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
updater = importlib.util.module_from_spec(spec)
loader.exec_module(updater)


class AutoUpdateTest(unittest.TestCase):
    def test_update_channel_defaults_to_stable_and_accepts_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".env").write_text("")
            with patch.object(updater, "ROOT", root):
                self.assertEqual(updater.update_channel(), "stable")
            (root / ".env").write_text("GEOLOGS_UPDATE_CHANNEL=edge\n")
            with patch.object(updater, "ROOT", root):
                self.assertEqual(updater.update_channel(), "edge")

    def test_unknown_update_channel_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".env").write_text("GEOLOGS_UPDATE_CHANNEL=nightly\n")
            with patch.object(updater, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "stable or edge"):
                    updater.update_channel()

    def test_backup_is_validated_before_it_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".env").write_text("GEOLOGS_TOKEN=secret\n")

            def command(*args, input_file=None, output_file=None):
                if "pg_dump" in args:
                    output_file.write(b"x" * 2048)
                elif "pg_restore" in args:
                    return "TABLE DATA public point_observations"
                return ""

            with patch.object(updater, "ROOT", root), patch.object(updater, "BACKUPS", root / "backups"), \
                 patch.object(updater, "run", side_effect=command):
                saved = updater.backup()
            self.assertEqual(saved.stat().st_size, 2048)
            self.assertEqual(saved.with_suffix(".env").read_text(), "GEOLOGS_TOKEN=secret\n")

    def test_incomplete_backup_prevents_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".env").write_text("GEOLOGS_TOKEN=secret\n")

            def command(*args, input_file=None, output_file=None):
                if "pg_dump" in args:
                    output_file.write(b"short")
                return ""

            with patch.object(updater, "ROOT", root), patch.object(updater, "BACKUPS", root / "backups"), \
                 patch.object(updater, "run", side_effect=command):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    updater.backup()
            self.assertEqual(list((root / "backups").glob("*.dump")), [])

    def test_failed_health_check_restores_previous_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state = root / "update-status"
            state.mkdir()
            (root / ".env").write_text("GEOLOGS_TOKEN=secret\n")
            previous = "GEOLOGS_AUTO_IMAGE=ghcr.io/karunstha/geologs@sha256:" + "a" * 64 + "\n"
            (state / "active.env").write_text(previous)

            def command(*args, **kwargs):
                if "imagetools" in args:
                    return "sha256:" + "b" * 64
                if "image" in args and "inspect" in args:
                    return "a" * 40 if "revision" in args[-1] else "v0.2.0"
                return ""

            with patch.object(updater, "ROOT", root), patch.object(updater, "STATE", state), \
                 patch.object(updater, "ACTIVE", state / "active.env"), patch.object(updater, "run", side_effect=command), \
                 patch.object(updater, "backup", return_value=root / "backups" / "saved.dump"), \
                 patch.object(updater, "ready", side_effect=RuntimeError("unhealthy")), \
                 patch.object(updater, "compose") as compose:
                with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                    updater.main()
            self.assertEqual((state / "active.env").read_text(), previous)
            self.assertEqual(compose.call_count, 2)
            self.assertTrue(compose.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
