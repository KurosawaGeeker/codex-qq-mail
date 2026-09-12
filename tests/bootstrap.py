"""Offline test setup. Values here are synthetic and are never delivery targets.

Import before any runtime module, including when executing a single test file.
The temporary default state and Codex home prevent accidental access to a user's
installed configuration. Individual tests inject fake transport boundaries.
"""

import atexit
import os
from pathlib import Path
import sys
import tempfile

SOURCE_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SOURCE_DIR))
_TEST_STATE = tempfile.TemporaryDirectory(prefix="codex-qq-mail-tests-")
atexit.register(_TEST_STATE.cleanup)
os.environ["CODEX_QQ_MAIL_ACCOUNT"] = "configured-user@qq.com"
os.environ["CODEX_QQ_MAIL_STATE_DIR"] = _TEST_STATE.name
os.environ["CODEX_HOME"] = str(Path(_TEST_STATE.name) / "codex-home")
os.environ.pop("CODEX_THREAD_ID", None)
