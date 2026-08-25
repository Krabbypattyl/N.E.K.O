"""Numeric v2 Story Package 的独立安全注册表。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .numeric_v2 import NumericV2CompileError, NumericV2Compiler


_STORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_PACKAGE_ROOT = Path(__file__).with_name("default_numeric_v2_packages")
_DEFAULT_PACKAGES_INITIALIZED_MARKER = ".defaults_initialized"


class NumericV2PackageError(ValueError):
    """Numeric v2 包无法复验或写入。"""  # noqa: DOCSTRING_CJK


class NumericV2PackageExistsError(NumericV2PackageError):
    """目标 story_id 已存在，默认不允许覆盖。"""  # noqa: DOCSTRING_CJK


class NumericV2PackageNotFoundError(NumericV2PackageError):
    """指定 Numeric v2 包不存在。"""  # noqa: DOCSTRING_CJK


class NumericV2PackageRegistry:
    """只管理 ``numeric_v2/packages``，不读取 v1 包或 Session。"""  # noqa: DOCSTRING_CJK

    def __init__(self, root: Path, compiler: NumericV2Compiler | None = None):
        self.root = Path(root)
        self.compiler = compiler or NumericV2Compiler()

    def ensure_default_packages(self) -> None:
        """首次使用 Numeric v2 时安装仓库内置剧本，绝不覆盖用户剧本。"""  # noqa: DOCSTRING_CJK

        marker = self.root / _DEFAULT_PACKAGES_INITIALIZED_MARKER
        if marker.is_file():
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            bundled_sources = (
                sorted(_DEFAULT_PACKAGE_ROOT.glob("*.json"))
                if _DEFAULT_PACKAGE_ROOT.is_dir()
                else []
            )
            # 当前发行物没有内置包时不写完成标记，后续版本加入默认剧本后仍能自动补装。
            if not bundled_sources:
                return
            if not any(self.root.glob("*.json")):
                for source in bundled_sources:
                    payload = json.loads(source.read_text(encoding="utf-8"))
                    compiled = self.compiler.compile(payload)
                    target = self.package_path(compiled.story_id)
                    if target.exists():
                        continue
                    try:
                        self.import_package(compiled.story)
                    except NumericV2PackageExistsError:
                        # 多进程首次启动可能同时安装同一默认包；另一进程已写入即视为初始化成功。
                        pass
            marker.touch(exist_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError, NumericV2CompileError) as exc:
            raise NumericV2PackageError("numeric_v2_default_package_invalid") from exc

    def delete_package(self, story_id: str) -> None:
        """删除一个已安装剧本；Session 由调用方在同一业务动作中级联清理。"""  # noqa: DOCSTRING_CJK

        target = self.package_path(story_id)
        try:
            target.unlink()
        except FileNotFoundError as exc:
            raise NumericV2PackageNotFoundError("numeric_story_not_found") from exc
        except OSError as exc:
            raise NumericV2PackageError("numeric_story_delete_failed") from exc

    def package_path(self, story_id: str) -> Path:
        if not isinstance(story_id, str) or not _STORY_ID_RE.fullmatch(story_id):
            raise NumericV2PackageError("invalid_numeric_v2_story_id")
        return self.root / f"{story_id}.json"

    def validate_package(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        compiled = self.compiler.compile(payload)
        meta = compiled.story["meta"]
        return {
            "story_id": meta["story_id"],
            "title": meta["title"],
            "author": meta["author"],
            "revision": meta["revision"],
            "language": meta["language"],
            "schema": compiled.story["schema"],
            "package_hash": compiled.package_hash,
            "warnings": [warning.__dict__ for warning in compiled.warnings],
            "intro": dict(compiled.story["intro"]),
            "metric_count": len(compiled.story["metric_schema"]),
        }

    def list_packages(self) -> list[dict[str, Any]]:
        """只列出能够重新通过当前 v2 合同的包。"""  # noqa: DOCSTRING_CJK

        if not self.root.is_dir():
            return []
        result = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                result.append(self.validate_package(payload))
            except (OSError, UnicodeError, json.JSONDecodeError, NumericV2CompileError):
                continue
        return result

    def load_engine(self, story_id: str):
        """从 v2 私有目录加载确定性 Engine，不兼容旧包。"""  # noqa: DOCSTRING_CJK

        from .numeric_v2_runtime import NumericV2Engine

        path = self.package_path(story_id)
        if not path.is_file():
            raise NumericV2PackageNotFoundError("numeric_story_not_found")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NumericV2Engine.from_mapping(payload)
        except NumericV2CompileError as exc:
            raise NumericV2PackageError("numeric_v2_contract_invalid") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NumericV2PackageError("numeric_v2_package_read_failed") from exc

    def import_package(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            compiled = self.compiler.compile(payload)
        except NumericV2CompileError as exc:
            raise NumericV2PackageError("numeric_v2_contract_invalid") from exc
        target = self.package_path(compiled.story_id)
        temporary_path: Path | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise NumericV2PackageExistsError("numeric_v2_story_exists")
            with tempfile.NamedTemporaryFile(
                dir=self.root,
                prefix=f".{target.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(compiled.json_bytes)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            try:
                os.link(temporary_path, target)
            except FileExistsError as exc:
                raise NumericV2PackageExistsError("numeric_v2_story_exists") from exc
            except OSError:
                # Some user-selected filesystems do not support hard links.
                # Fall back to exclusive creation without ever replacing a
                # target that another process may have created concurrently.
                try:
                    target_fd = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError as exc:
                    raise NumericV2PackageExistsError("numeric_v2_story_exists") from exc
                try:
                    with os.fdopen(target_fd, "wb") as target_file:
                        target_file.write(compiled.json_bytes)
                        target_file.flush()
                        os.fsync(target_file.fileno())
                except Exception:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    raise
        except NumericV2PackageError:
            raise
        except OSError as exc:
            raise NumericV2PackageError("numeric_v2_import_failed") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return self.validate_package(compiled.story)


__all__ = [
    "NumericV2PackageError",
    "NumericV2PackageExistsError",
    "NumericV2PackageNotFoundError",
    "NumericV2PackageRegistry",
]
