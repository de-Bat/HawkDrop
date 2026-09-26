import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hawkdrop.config import Config, load_config, server_settings
from hawkdrop.db import Database
from hawkdrop.server import ServerContext, _next_check_delay


class ServerSettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = server_settings({}, {}, env={})
        self.assertEqual((s.host, s.port, s.token, s.check_every, s.base_path), ("127.0.0.1", 8765, None, None, ""))

    def test_precedence_cli_env_config(self):
        cfg = {"host": "10.0.0.1", "port": 1000, "check_every": 12}
        env = {"HAWKDROP_PORT": "2000", "HAWKDROP_CHECK_EVERY": "3"}
        s = server_settings({"port": 3000}, cfg, env=env)
        self.assertEqual(s.host, "10.0.0.1")   # config
        self.assertEqual(s.port, 3000)         # CLI beats env
        self.assertEqual(s.check_every, 3.0)   # env beats config

    def test_token_file_and_base_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "token"
            f.write_text("from-file\n")
            s = server_settings({}, {}, env={"HAWKDROP_TOKEN_FILE": str(f), "HAWKDROP_BASE_PATH": "hawkdrop/"})
            self.assertEqual(s.token, "from-file")
            self.assertEqual(s.base_path, "/hawkdrop")
            # an explicit token wins over the file
            self.assertEqual(server_settings({"token": "cli"}, {"token_file": str(f)}, env={}).token, "cli")

    def test_invalid_values(self):
        with self.assertRaises(SystemExit):
            server_settings({}, {}, env={"HAWKDROP_PORT": "eighty"})
        with self.assertRaises(SystemExit):
            server_settings({}, {}, env={"HAWKDROP_TOKEN_FILE": "/nonexistent/token"})
        self.assertIsNone(server_settings({}, {}, env={"HAWKDROP_CHECK_EVERY": "0"}).check_every)

    def test_config_file_server_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text('[server]\nport = 9000\n[destination]\ncode = "EU"\n')
            cfg = load_config(p)
            self.assertEqual(server_settings({}, cfg.server, env={}).port, 9000)
            self.assertEqual(cfg.destination["code"], "EU")


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "h.db"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_wal_and_concurrent_connection(self):
        mode = self.db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        other = Database(self.path)
        other.add_item("From another process")
        self.assertEqual(len(self.db.list_items()), 1)
        other.close()

    def test_backup(self):
        self.db.add_item("Kept")
        target = Path(self.tmp.name) / "backups" / "b.db"
        self.db.backup(target)
        copy = Database(target)
        self.assertEqual([i.name for i in copy.list_items()], ["Kept"])
        copy.close()

    def test_scheduled_check_survives_restarts(self):
        ctx = ServerContext(self.path, Config(), offline_fx=True)
        self.assertEqual(_next_check_delay(ctx, timedelta(hours=6)), 60.0)  # never ran
        self.db.set_kv("last_auto_check", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        self.assertAlmostEqual(_next_check_delay(ctx, timedelta(hours=6)), 4 * 3600, delta=5)
        self.db.set_kv("last_auto_check", (datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        self.assertEqual(_next_check_delay(ctx, timedelta(hours=6)), 30.0)  # overdue



class BackupCommandTest(unittest.TestCase):
    def test_backup_into_new_directory(self):
        import contextlib
        import io

        from hawkdrop.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--db", db, "--offline", "track", "Thing"])
                main(["--db", db, "--offline", "backup", os.path.join(tmp, "backups")])
            files = list(Path(tmp, "backups").glob("hawkdrop-*.db"))
            self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
