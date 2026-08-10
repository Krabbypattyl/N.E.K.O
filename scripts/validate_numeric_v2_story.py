#!/usr/bin/env python3
"""供 InkAI 调用的 Numeric v2 复验与安全安装 CLI。"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.theater.numeric_v2 import NumericV2CompileError, NumericV2Compiler  # noqa: E402
from services.theater.numeric_v2_registry import (  # noqa: E402
    NumericV2PackageError,
    NumericV2PackageExistsError,
    NumericV2PackageRegistry,
)


def _package_root() -> Path:
    """复用 N.E.K.O 自己的存储策略，不在 InkAI 复制平台路径规则。"""

    from utils.config_manager import ConfigManager

    return Path(ConfigManager().app_docs_dir) / "theater" / "numeric_v2" / "packages"


def main() -> int:
    if len(sys.argv) not in {2, 3} or (len(sys.argv) == 3 and sys.argv[2] != "--install"):
        print(json.dumps({"success": False, "error": {"code": "invalid_arguments"}}))
        return 2
    source = Path(sys.argv[1])
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        compiler = NumericV2Compiler()
        compiled = compiler.compile(payload)
        if len(sys.argv) == 3:
            data = NumericV2PackageRegistry(_package_root(), compiler).import_package(compiled.story)
        else:
            meta = compiled.story["meta"]
            data = {
                "story_id": meta["story_id"],
                "title": meta["title"],
                "schema": compiled.story["schema"],
                "package_hash": compiled.package_hash,
                "warnings": [asdict(item) for item in compiled.warnings],
            }
        print(json.dumps({"success": True, "data": data}, ensure_ascii=False))
        return 0
    except NumericV2CompileError as exc:
        print(json.dumps({
            "success": False,
            "error": {
                "code": "numeric_v2_compile_failed",
                "details": {"issues": [asdict(item) for item in exc.issues]},
            },
        }, ensure_ascii=False))
        return 3
    except NumericV2PackageExistsError:
        print(json.dumps({"success": False, "error": {"code": "numeric_v2_story_exists"}}))
        return 4
    except (NumericV2PackageError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "success": False,
            "error": {"code": str(exc) or "numeric_v2_validation_failed"},
        }, ensure_ascii=False))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
