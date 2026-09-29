"""Numeric v2 启动核查和可恢复剧本删除事务。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from .numeric_v2_archive import (
    PUBLIC_ARCHIVE_QUARANTINE_DIRNAME,
    NumericV2ArchiveError,
    NumericV2ArchiveStore,
)
from .numeric_v2_registry import NumericV2PackageRegistry, NumericV2PackageError, NumericV2PackageNotFoundError
from .numeric_v2_runtime import NumericV2RuntimeError
from .numeric_v2_store import (
    NumericV2SessionStore,
    NumericV2StoreError,
    _read_numeric_v2_session_summary,
    _read_story_session_slots,
    _write_story_session_slots,
    _delete_numeric_v2_sessions_unlocked,
    _is_story_session_index_content_error,
    numeric_v2_session_files_guard,
    list_numeric_v2_public_archives,
    list_numeric_v2_sessions,
)

from .numeric_v2_storage_transaction import run_storage_mutation


QUARANTINE_FILE_LIMIT = 6
# 公开冷档案隔离区（PUBLIC_ARCHIVE_QUARANTINE_DIRNAME）独立于 Session 隔离区，
# 不被 QUARANTINE_FILE_LIMIT 裁剪删除；只随显式删除/遗忘在可回滚事务内清理。
DELETE_TRANSACTION_SCHEMA = "neko.script.delete_transaction.numeric.v2"
# 损坏的 story_sessions.json 是可重建的派生缓存；移入独立目录保存，不参与裁剪删除。
INDEX_QUARANTINE_DIRNAME = "quarantine_indexes"
# 这些状态只需清理事务目录，绝不重放备份。
_SETTLED_DELETE_TRANSACTION_STATES = frozenset({"committed", "rolled_back", "superseded"})

_MAINTENANCE_LOCK = threading.Lock()
_MAINTAINED_ROOTS: set[str] = set()
logger = logging.getLogger(__name__)


def _atomic_write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=".manifest-",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _manifest_path(payload: Mapping[str, Any], key: str) -> Path | None:
    raw = str(payload.get(key) or "").strip()
    return Path(raw) if raw else None


def _restore_missing_file(backup: Path, target: Path) -> None:
    # Only undo this transaction's unlink: a file present again at the target is
    # either untouched or newer (re-import, new round) and must not be overwritten.
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)


def _restore_delete_transaction(transaction_dir: Path, payload: Mapping[str, Any]) -> None:
    """Best-effort undo of a story delete; every step runs, the first failure is raised last."""

    failures: list[BaseException] = []

    def attempt(step: Callable[[], None]) -> None:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below
            failures.append(exc)

    package_backup = transaction_dir / "package.json"
    package_target = _manifest_path(payload, "package_target")
    if package_backup.is_file() and package_target is not None:
        attempt(lambda: _restore_missing_file(package_backup, package_target))

    for root_key, backup_dirname in (
        ("session_root", "sessions"),
        ("public_archive_root", "public_archives"),
        ("receipt_root", "end_receipts"),
        ("public_archive_quarantine_root", PUBLIC_ARCHIVE_QUARANTINE_DIRNAME),
    ):
        target_root = _manifest_path(payload, root_key)
        backup_root = transaction_dir / backup_dirname
        if not backup_root.is_dir() or target_root is None:
            continue
        for backup in sorted(backup_root.glob("*.json")):
            attempt(
                lambda backup=backup, target_root=target_root: _restore_missing_file(
                    backup, target_root / backup.name,
                )
            )

    index_target = _manifest_path(payload, "index_target")
    story_id = str(payload.get("story_id") or "").strip()
    raw_slots = payload.get("index_story_slots")
    if index_target is not None and story_id and isinstance(raw_slots, dict) and raw_slots:
        def restore_index_slots() -> None:
            stories = _read_story_session_slots_or_quarantine(index_target)
            story_slots = stories.setdefault(story_id, {})
            for character_id, session_id in raw_slots.items():
                if str(character_id).strip() and str(session_id).strip():
                    # A slot written after the delete belongs to a newer session.
                    story_slots.setdefault(str(character_id), str(session_id))
            _write_story_session_slots(index_target, stories)

        attempt(restore_index_slots)

    if failures:
        raise failures[0]


def _read_story_session_slots_or_quarantine(index_path: Path) -> dict[str, dict[str, str]]:
    """Read the derived story-session index, moving a corrupt one aside for a rebuild."""

    try:
        return _read_story_session_slots(index_path)
    except NumericV2StoreError as exc:
        if not _is_story_session_index_content_error(exc):
            # Temporarily unreadable is not corrupt: fail closed, never move it.
            raise
        logger.warning(
            "Numeric v2 story-session index %s is corrupt (%s); quarantining and rebuilding it",
            index_path,
            exc,
        )
        _quarantine_session(
            index_path,
            index_path.parent / INDEX_QUARANTINE_DIRNAME,
            "corrupt",
        )
        return {}


def recover_numeric_v2_delete_transactions(theater_root: Path) -> None:
    root = Path(theater_root) / "numeric_v2" / "delete_transactions"
    if not root.is_dir():
        return
    for transaction_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = transaction_dir / "manifest.json"
        if not manifest_path.is_file():
            # destructive 阶段只会在 prepared manifest 落盘后开始；这里仅是
            # 备份阶段中断留下的临时目录，可以直接清理。
            shutil.rmtree(transaction_dir, ignore_errors=True)
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != DELETE_TRANSACTION_SCHEMA:
            continue
        state = payload.get("state")
        if state == "prepared":
            _restore_delete_transaction(transaction_dir, payload)
        elif state not in _SETTLED_DELETE_TRANSACTION_STATES:
            # Unknown state: keep the backup rather than guess.
            logger.warning(
                "Numeric v2 delete transaction %s has unknown state %r; leaving it in place",
                transaction_dir,
                state,
            )
            continue
        shutil.rmtree(transaction_dir, ignore_errors=True)


def _supersede_pending_delete_transactions(
    theater_root: Path, story_id: str, current_dir: Path,
) -> None:
    # A later committed delete of the same story supersedes an earlier one whose
    # rollback failed; replaying that stale backup would resurrect the story.
    root = Path(theater_root) / "numeric_v2" / "delete_transactions"
    try:
        transaction_dirs = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        logger.warning("Numeric v2 cannot scan delete transactions", exc_info=True)
        return
    for transaction_dir in transaction_dirs:
        if transaction_dir == current_dir:
            continue
        manifest_path = transaction_dir / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != DELETE_TRANSACTION_SCHEMA
                or payload.get("state") != "prepared"
                or payload.get("story_id") != story_id
            ):
                continue
            payload["state"] = "superseded"
            _atomic_write_manifest(manifest_path, payload)
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning(
                "Numeric v2 cannot supersede delete transaction %s", transaction_dir, exc_info=True,
            )


def _prepare_delete_transaction(
    theater_root: Path,
    registry: NumericV2PackageRegistry,
    story_id: str,
) -> tuple[Path, Path, dict[str, Any]]:
    package_target = registry.package_path(story_id)
    session_root = Path(theater_root) / "numeric_v2" / "sessions"
    public_archive_root = Path(theater_root) / "numeric_v2" / "public_archives"
    archive_store = NumericV2ArchiveStore(theater_root)
    index_target = Path(theater_root) / "numeric_v2" / "story_sessions.json"
    transaction_dir = (
        Path(theater_root)
        / "numeric_v2"
        / "delete_transactions"
        / f"{story_id}-{uuid.uuid4().hex}"
    )
    try:
        transaction_dir.mkdir(parents=True)
        shutil.copy2(package_target, transaction_dir / "package.json")
        session_backup_root = transaction_dir / "sessions"
        story_session_ids: set[str] = set()
        for summary in list_numeric_v2_sessions(
            theater_root,
            story_id=story_id,
            raise_on_io_error=True,
        ):
            story_session_ids.add(str(summary.get("session_id") or ""))
            session_backup_root.mkdir(parents=True, exist_ok=True)
            source = Path(summary["path"])
            shutil.copy2(source, session_backup_root / source.name)
        public_archive_backup_root = transaction_dir / "public_archives"
        for summary in list_numeric_v2_public_archives(
            theater_root,
            story_id=story_id,
            raise_on_io_error=True,
        ):
            public_archive_backup_root.mkdir(parents=True, exist_ok=True)
            source = Path(summary["path"])
            shutil.copy2(source, public_archive_backup_root / source.name)
        receipt_backup_root = transaction_dir / "end_receipts"
        for source in archive_store.receipt_paths_for_scope(story_id=story_id):
            if not source.is_file():
                continue
            receipt_backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, receipt_backup_root / source.name)
        index_stories = _read_story_session_slots(index_target)
        story_session_ids.update(str(value) for value in index_stories.get(story_id, {}).values())
        # Only quarantined archives attributable to this story are erased here: a
        # package delete is not a request to erase data whose owner is unknown.
        quarantined_archives = archive_store.quarantined_public_archive_paths(
            story_id=story_id,
            session_ids=story_session_ids,
        )
        quarantine_backup_root = transaction_dir / PUBLIC_ARCHIVE_QUARANTINE_DIRNAME
        for source in quarantined_archives:
            quarantine_backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, quarantine_backup_root / source.name)
        manifest = {
            "schema": DELETE_TRANSACTION_SCHEMA,
            "state": "prepared",
            "story_id": story_id,
            "package_target": str(package_target),
            "session_root": str(session_root),
            "public_archive_root": str(public_archive_root),
            "receipt_root": str(archive_store.root),
            "public_archive_quarantine_root": str(archive_store.public_archive_quarantine_root),
            "quarantined_archive_files": [path.name for path in quarantined_archives],
            "index_target": str(index_target),
            "index_existed": index_target.is_file(),
            "index_story_slots": index_stories.get(story_id, {}),
        }
        manifest_path = transaction_dir / "manifest.json"
        _atomic_write_manifest(manifest_path, manifest)
        return transaction_dir, manifest_path, manifest
    except BaseException:
        shutil.rmtree(transaction_dir, ignore_errors=True)
        raise


async def delete_numeric_v2_story_transactionally(
    theater_root: Path,
    registry: NumericV2PackageRegistry,
    story_id: str,
    *,
    write_transaction=nullcontext,
) -> int:
    async with numeric_v2_session_files_guard(theater_root):
        return await run_storage_mutation(
            write_transaction, _delete_story_files, theater_root, registry, story_id,
        )


def _delete_story_files(theater_root: Path, registry: NumericV2PackageRegistry, story_id: str) -> int:
    # Backup, deletion and rollback share one cloud fence and one worker thread.
    try:
        transaction_dir, manifest_path, manifest = _prepare_delete_transaction(
            theater_root, registry, story_id,
        )
    except (OSError, NumericV2ArchiveError) as exc:
        raise NumericV2StoreError("numeric_story_delete_backup_failed") from exc
    try:
        deleted =_delete_numeric_v2_sessions_unlocked(theater_root, story_id=story_id)
        NumericV2ArchiveStore(theater_root).delete_receipts(story_id=story_id)
        archive_store = NumericV2ArchiveStore(theater_root)
        archive_store.delete_public_archives(story_id=story_id, character_id="")
        for name in manifest["quarantined_archive_files"]:
            # Backed up in the prepared transaction above, so a rollback restores it.
            (archive_store.public_archive_quarantine_root / name).unlink(missing_ok=True)
        registry.delete_package(story_id)
        manifest["state"] = "committed"
        _atomic_write_manifest(manifest_path, manifest)
    except BaseException:
        try:
            _restore_delete_transaction(transaction_dir, manifest)
        except Exception as rollback_exc:
            raise NumericV2StoreError("numeric_story_delete_rollback_failed") from rollback_exc
        # rmtree may silently leave the manifest behind (e.g. a Windows share
        # violation); settle it first so startup recovery never replays it.
        try:
            manifest["state"] = "rolled_back"
            _atomic_write_manifest(manifest_path, manifest)
        except OSError:
            logger.warning("Numeric v2 cannot mark delete rollback settled", exc_info=True)
        shutil.rmtree(transaction_dir, ignore_errors=True)
        raise
    _supersede_pending_delete_transactions(theater_root, story_id, transaction_dir)
    shutil.rmtree(transaction_dir, ignore_errors=True)
    return len(deleted)


def _quarantine_session(path: Path, quarantine_root: Path, reason: str) -> None:
    quarantine_root.mkdir(parents=True, exist_ok=True)
    target = quarantine_root / (
        f"{reason}-{int(time.time() * 1000)}-{uuid.uuid4().hex}-{path.name}"
    )
    os.replace(path, target)


def _trim_quarantine(quarantine_root: Path) -> None:
    if not quarantine_root.is_dir():
        return
    files = sorted(
        (path for path in quarantine_root.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in files[QUARANTINE_FILE_LIMIT:]:
        stale.unlink()


def _trim_quarantine_safely(quarantine_root: Path) -> None:
    try:
        _trim_quarantine(quarantine_root)
    except OSError:
        logger.warning("Numeric v2 隔离区裁剪失败", exc_info=True)


def _caused_by_os_error(exc: BaseException) -> bool:
    """识别被业务异常包装的暂时性文件系统错误。"""  # noqa: DOCSTRING_CJK

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError):
            return True
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return False


def audit_numeric_v2_storage(
    theater_root: Path,
    registry: NumericV2PackageRegistry,
    *,
    character_ids_by_name: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """启动/维护时全盘复验；日常恢复路径不扫描 Session 目录。"""  # noqa: DOCSTRING_CJK

    session_root = Path(theater_root) / "numeric_v2" / "sessions"
    quarantine_root = Path(theater_root) / "numeric_v2" / "quarantine"
    index_path = Path(theater_root) / "numeric_v2" / "story_sessions.json"
    known_characters = {
        str(name).strip(): str(character_id).strip()
        for name, character_id in (character_ids_by_name or {}).items()
        if str(name).strip() and str(character_id).strip()
    }
    known_character_ids = set(known_characters.values())
    if not session_root.is_dir():
        if index_path.is_file():
            _write_story_session_slots(index_path, {})
        _trim_quarantine_safely(quarantine_root)
        return {"valid": 0, "quarantined": 0}

    valid: list[tuple[Path, dict[str, str], int, int, str]] = []
    quarantined = 0
    engine_cache: dict[str, Any] = {}
    unloadable_stories: set[str] = set()
    for path in sorted(session_root.glob("*.json")):
        try:
            summary = _read_numeric_v2_session_summary(
                path,
                raise_on_io_error=True,
            )
            if summary is None or summary["session_id"] != path.stem:
                raise NumericV2StoreError("numeric_session_summary_invalid")
            story_id = summary["story_id"]
            if story_id in unloadable_stories:
                continue
            if story_id not in engine_cache:
                package_path = registry.package_path(story_id)
                try:
                    package_path.stat()
                except FileNotFoundError:
                    # 已删除剧本留下的孤儿 Session 属于可确定的数据失效，不应当作暂时性 I/O 故障。
                    raise NumericV2StoreError(
                        "numeric_session_story_missing"
                    ) from None
                try:
                    engine_cache[story_id] = registry.load_engine(story_id)
                except NumericV2PackageNotFoundError:
                    raise
                except NumericV2PackageError as exc:
                    if _caused_by_os_error(exc):
                        raise
                    # An unusable package says nothing about the validity of its saves.
                    unloadable_stories.add(story_id)
                    continue
            store = NumericV2SessionStore(theater_root, engine_cache[story_id])
            stored = store._read(path)
            try:
                store._validate_chain(stored)
            except NumericV2RuntimeError as exc:
                if str(exc) not in {
                    "story_package_revision_mismatch",
                    "story_package_hash_mismatch",
                }:
                    raise
                # 合法旧 Session 不能因剧本升级被隔离；保留它供用户结束或删除，
                # 日常恢复仍会走严格重放并拒绝继续旧版本剧情。
                store._validate_lifecycle_chain(stored)
            effective_character_id = summary["character_id"] or known_characters.get(
                summary["catgirl_name"],
                "",
            )
            if known_characters and effective_character_id not in known_character_ids:
                raise NumericV2StoreError("numeric_session_character_missing")
            if not effective_character_id:
                raise NumericV2StoreError("numeric_session_character_unresolved")
            valid.append(
                (
                    path,
                    summary,
                    stored.session.revision,
                    path.stat().st_mtime_ns,
                    effective_character_id,
                )
            )
        except (NumericV2StoreError, NumericV2RuntimeError, NumericV2PackageError, NumericV2PackageNotFoundError, OSError) as exc:
            if _caused_by_os_error(exc):
                # 权限、挂载或设备故障可能只是暂时状态；本轮中止，绝不移动仍可能有效的数据。
                raise NumericV2StoreError(
                    "numeric_session_audit_read_failed"
                ) from exc
            try:
                _quarantine_session(path, quarantine_root, "invalid")
                quarantined += 1
            except OSError:
                logger.warning(
                    "Numeric v2 无法隔离异常 Session %s: %s",
                    path,
                    exc,
                    exc_info=True,
                )

    # 索引是派生缓存：内容损坏时隔离后按 Session 文件重建；暂时性 I/O 故障仍中止本轮。
    old_index = _read_story_session_slots_or_quarantine(index_path)
    slots: dict[
        tuple[str, str],
        list[tuple[Path, dict[str, str], int, int, str]],
    ] = {}
    for item in valid:
        slots.setdefault((item[1]["story_id"], item[4]), []).append(item)

    rebuilt: dict[str, dict[str, str]] = {
        story_id: dict(old_index[story_id])
        for story_id in unloadable_stories if story_id in old_index
    }
    for (story_id, character_id), candidates in slots.items():
        indexed_id = old_index.get(story_id, {}).get(character_id, "")
        selected = next(
            (item for item in candidates if item[1]["session_id"] == indexed_id),
            None,
        ) or max(
            candidates,
            key=lambda item: (
                item[1]["status"] != "ended",
                item[2],
                item[3],
                item[1]["session_id"],
            ),
        )
        rebuilt.setdefault(story_id, {})[character_id] = selected[1]["session_id"]
        for duplicate in candidates:
            if duplicate[0] == selected[0]:
                continue
            try:
                _quarantine_session(duplicate[0], quarantine_root, "duplicate")
                quarantined += 1
            except OSError:
                logger.warning(
                    "Numeric v2 无法隔离重复 Session: %s",
                    duplicate[0],
                    exc_info=True,
                )

    _write_story_session_slots(index_path, rebuilt)
    _trim_quarantine_safely(quarantine_root)
    return {"valid": sum(len(slots) for story_id, slots in rebuilt.items() if story_id not in unloadable_stories), "quarantined": quarantined}


def maintain_numeric_v2_storage_once(
    theater_root: Path,
    registry: NumericV2PackageRegistry,
    *,
    character_ids_by_name: Mapping[str, str],
    assert_writable: Callable[[], None] | None = None,
    write_transaction=nullcontext,
) -> dict[str, int] | None:
    """每个运行根仅在冷启动初始化时执行一次恢复和全盘核查。"""  # noqa: DOCSTRING_CJK

    key = str(Path(theater_root).resolve())
    with _MAINTENANCE_LOCK:
        if key in _MAINTAINED_ROOTS:
            return None
        with write_transaction():
            # 冷启动恢复、默认包安装和索引重建都会写盘，必须服从与云存档相同的写栅栏。
            if assert_writable is not None:
                assert_writable()
            recover_numeric_v2_delete_transactions(theater_root)
            registry.ensure_default_packages()
            result = audit_numeric_v2_storage(
                theater_root,
                registry,
                character_ids_by_name=character_ids_by_name,
            )
            active_session_ids = {
                item["session_id"]
                for item in list_numeric_v2_sessions(theater_root)
            }
            archive_store = NumericV2ArchiveStore(theater_root)
            result.update(archive_store.cleanup_receipts(active_session_ids))
            # 损坏的公开冷档案会让角色改名/删除的严格快照对所有角色失败；
            # 与坏档 Session 一样移入隔离区，但使用独立目录，不参与数量裁剪删除。
            archives_quarantined = archive_store.quarantine_invalid_public_archives(
                Path(theater_root) / "numeric_v2" / PUBLIC_ARCHIVE_QUARANTINE_DIRNAME
            )
            if archives_quarantined:
                logger.warning("Numeric v2 已隔离 %d 份无法解析的公开冷档案", archives_quarantined)
                result["archives_quarantined"] = archives_quarantined
            _MAINTAINED_ROOTS.add(key)
            return result


__all__ = [
    "INDEX_QUARANTINE_DIRNAME",
    "PUBLIC_ARCHIVE_QUARANTINE_DIRNAME",
    "QUARANTINE_FILE_LIMIT",
    "audit_numeric_v2_storage",
    "delete_numeric_v2_story_transactionally",
    "maintain_numeric_v2_storage_once",
    "recover_numeric_v2_delete_transactions",
]
