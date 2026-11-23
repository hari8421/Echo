import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from echo_agent.cli import main


class CLITests(unittest.TestCase):
    def test_init_list_and_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--home", tmp, "init"])
            self.assertEqual(code, 0)
            self.assertIn("Echo workspace ready", out.getvalue())

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--home", tmp, "list"])
            self.assertEqual(code, 0)
            self.assertIn("No documents yet", out.getvalue())

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    ["--home", tmp, "remember", "User", "likes", "concise", "notes", "--kind", "preference"]
                )
            self.assertEqual(code, 0)
            self.assertIn("Saved memory", out.getvalue())

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--home", tmp, "memories"])
            self.assertEqual(code, 0)
            self.assertIn("likes concise notes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
