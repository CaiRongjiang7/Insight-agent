from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
from pathlib import Path


def _load_module(module_path: Path):
    relative = module_path.relative_to(Path.cwd())
    module_name = "sandbox_" + "_".join(
        part.replace(".", "_").replace(":", "_") for part in relative.with_suffix("").parts
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load test module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    root = Path.cwd()
    tests_dir = root / "tests"
    if not tests_dir.exists():
        print("No tests directory found.")
        return 0

    failures: list[str] = []
    total = 0
    for module_path in sorted(tests_dir.rglob("test_*.py")):
        module = _load_module(module_path)
        for name, value in inspect.getmembers(module):
            if not name.startswith("test_") or not callable(value):
                continue
            total += 1
            try:
                value()
                print(f"PASS {module_path.relative_to(root)}::{name}")
            except Exception:  # noqa: BLE001
                failures.append(f"{module_path.relative_to(root)}::{name}")
                print(f"FAIL {module_path.relative_to(root)}::{name}")
                traceback.print_exc()

    print(f"Executed {total} tests with {len(failures)} failures.")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
