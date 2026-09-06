import os
import tempfile

# テストは必ず一時DBを使う（開発用 data/mss2.db を汚さない）。
# app.* を import する前に設定する必要があるため conftest の import 時点で行う。
_tmpdir = tempfile.mkdtemp(prefix="mss2test-")
os.environ["MSS2_DB_URL"] = f"sqlite:///{os.path.join(_tmpdir, 'test.db')}"
