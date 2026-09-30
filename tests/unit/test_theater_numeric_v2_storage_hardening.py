"""Numeric v2 storage hardening: portable delete manifests, share-violation retries, quarantine privacy."""

from __future__ import annotations

import json
import shutil

import pytest

from services.theater import numeric_v2_maintenance, numeric_v2_store
from services.theater.numeric_v2_registry import NumericV2PackageRegistry
from services.theater.numeric_v2_runtime import NumericV2Engine, NumericV2Runtime
from tests.unit.test_theater_numeric_v2_runtime import _binding, _branch_story, _opening


async def _prepared_interrupted_delete(theater_root):
    story = _branch_story()
    story_id = story["meta"]["story_id"]
    registry = NumericV2PackageRegistry(theater_root / "numeric_v2" / "packages")
    registry.import_package(story)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), theater_root)
    stored = await runtime.start_session(
        session_id="runtime_delete_migrated",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    transaction_dir, manifest_path, manifest = numeric_v2_maintenance._prepare_delete_transaction(
        theater_root, registry, story_id,
    )
    # Simulate the crash after the destructive phase started.
    await numeric_v2_store.delete_numeric_v2_sessions(theater_root, story_id=story_id)
    registry.delete_package(story_id)
    return story_id, stored.session.session_id, manifest_path, manifest


def _migrate(old_root, new_root):
    shutil.copytree(old_root, new_root)
    shutil.rmtree(old_root)


@pytest.mark.asyncio
async def test_delete_manifest_is_portable_across_storage_root_migration(tmp_path):
    old_root = tmp_path / "old" / "theater"
    new_root = tmp_path / "new" / "theater"
    story_id, session_id, manifest_path, manifest = await _prepared_interrupted_delete(old_root)
    assert not any(
        str(value).startswith(str(tmp_path))
        for value in manifest.values()
        if isinstance(value, str)
    )

    _migrate(old_root, new_root)
    numeric_v2_maintenance.recover_numeric_v2_delete_transactions(new_root)

    assert (new_root / "numeric_v2" / "packages" / f"{story_id}.json").is_file()
    assert (new_root / "numeric_v2" / "sessions" / f"{session_id}.json").is_file()
    assert not old_root.exists()
    assert not list((new_root / "numeric_v2" / "delete_transactions").iterdir())


@pytest.mark.asyncio
async def test_legacy_absolute_manifest_is_mapped_onto_current_root(tmp_path):
    old_root = tmp_path / "old" / "theater"
    new_root = tmp_path / "new" / "theater"
    story_id, session_id, manifest_path, manifest = await _prepared_interrupted_delete(old_root)
    legacy = dict(manifest)
    for key in numeric_v2_maintenance._MANIFEST_PATH_KEYS:
        legacy[key] = str(old_root / manifest[key])
    manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

    _migrate(old_root, new_root)
    numeric_v2_maintenance.recover_numeric_v2_delete_transactions(new_root)

    assert (new_root / "numeric_v2" / "packages" / f"{story_id}.json").is_file()
    assert (new_root / "numeric_v2" / "sessions" / f"{session_id}.json").is_file()
    assert not old_root.exists()


@pytest.mark.asyncio
async def test_unmappable_legacy_manifest_is_kept_and_never_restored_elsewhere(tmp_path):
    old_root = tmp_path / "old" / "theater"
    new_root = tmp_path / "new" / "theater"
    story_id, session_id, manifest_path, manifest = await _prepared_interrupted_delete(old_root)
    legacy = dict(manifest)
    legacy["session_root"] = str(tmp_path / "elsewhere" / "sessions")
    manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

    _migrate(old_root, new_root)
    numeric_v2_maintenance.recover_numeric_v2_delete_transactions(new_root)

    assert not (tmp_path / "elsewhere").exists()
    assert not (new_root / "numeric_v2" / "packages" / f"{story_id}.json").exists()
    assert len(list((new_root / "numeric_v2" / "delete_transactions").iterdir())) == 1


def test_manifest_path_rejects_parent_traversal(tmp_path):
    with pytest.raises(numeric_v2_maintenance._UnresolvableManifestPathError):
        numeric_v2_maintenance._manifest_path({"session_root": "../escape"}, "session_root", tmp_path)
    with pytest.raises(numeric_v2_maintenance._UnresolvableManifestPathError):
        numeric_v2_maintenance._manifest_path(
            {"session_root": str(tmp_path / ".." / "escape")}, "session_root", tmp_path,
        )

