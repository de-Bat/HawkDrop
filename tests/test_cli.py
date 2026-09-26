import contextlib
import io
import os
import tempfile
import unittest

from hawkdrop.cli import main


class CliSmokeTest(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--db", self.db, "--offline", *args])
        return out.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "cli.db")
        os.environ["HAWKDROP_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_workflow(self):
        self.assertIn("Tracking #1", self.run_cli("track", "AirPods Pro 3", "--category", "electronics"))
        self.run_cli("price", "AirPods", "ksp", "999", "--shipping", "0")
        self.run_cli("price", "AirPods", "amazon_us", "249")
        out = self.run_cli("compare", "AirPods")
        self.assertIn("KSP", out)
        self.assertIn("Amazon.com", out)
        self.assertIn("Recommendation", self.run_cli("advise", "AirPods"))
        self.assertIn('"action"', self.run_cli("advise", "AirPods", "--json"))

    def test_demo_and_events(self):
        self.assertIn("Recommendation", self.run_cli("demo"))
        self.assertIn("Black Friday", self.run_cli("events", "--days", "365"))
        self.assertIn("ksp", self.run_cli("stores"))


if __name__ == "__main__":
    unittest.main()
