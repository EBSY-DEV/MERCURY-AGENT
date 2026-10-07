"""Check that the built wheel includes every dashboard asset from the source tree."""

import sys
from pathlib import Path
from zipfile import ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    web = root / "mercury" / "web"
    expected = {
        path.relative_to(root).as_posix()
        for path in web.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if not expected:
        raise SystemExit("No dashboard assets found in the source tree")
    for wheel in sys.argv[1:]:
        with ZipFile(wheel) as archive:
            missing = expected - set(archive.namelist())
        if missing:
            raise SystemExit(f"{wheel} is missing dashboard assets:\n" + "\n".join(sorted(missing)))
        print(f"{Path(wheel).name}: all {len(expected)} dashboard assets are included")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scripts/check_wheel_assets.py WHEEL [WHEEL ...]")
    main()
