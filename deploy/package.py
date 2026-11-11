"""Create a source-only deployment archive; never include secrets or local data."""
from pathlib import Path
import hashlib
import tarfile

root = Path(__file__).resolve().parents[1]
paths = ["app", "agent", "static", "docs", "deploy", "tests", "requirements.txt", "requirements-dev.txt",
         "Dockerfile", ".dockerignore", "compose.yaml", "README.md", "AGENTS.md", ".gitignore"]
for required in ("app/main.py", "agent/worker.py", "agent/notebook.py"):
    if not (root / required).is_file():
        raise SystemExit(f"Missing required source: {required}")

archive = root / "work" / "deploy.tar.gz"
archive.parent.mkdir(exist_ok=True)


def include(info):
    path = Path(info.name)
    if "__pycache__" in path.parts or path.suffix in (".pyc", ".log") or path.name.startswith("credentials"):
        return None
    return info


with tarfile.open(archive, "w:gz") as tar:
    for name in paths:
        if (root / name).exists():
            tar.add(root / name, arcname=name, filter=include)
print(f"Archive: {archive}")
print(f"SHA256: {hashlib.sha256(archive.read_bytes()).hexdigest()}")
