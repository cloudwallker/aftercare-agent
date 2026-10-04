"""Exercise the release gate against real Git indexes, outside the Docker test image."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release.py"
INFOGRAPHIC = "docs/images/cartoon-infographic.png"


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


def stage(repository, name, data):
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    subprocess.run(["git", "add", "--", name], cwd=repository, check=True, capture_output=True)
    return path


def check(repository):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )


def test_reviewed_infographic_is_accepted(repository):
    stage(repository, INFOGRAPHIC, (ROOT / INFOGRAPHIC).read_bytes())

    result = check(repository)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Scanned 1 indexed files; 0 findings." in result.stdout


def test_changed_infographic_requires_another_review(repository):
    stage(repository, INFOGRAPHIC, (ROOT / INFOGRAPHIC).read_bytes() + b"changed")

    result = check(repository)

    assert result.returncode == 1
    assert f"{INFOGRAPHIC}:0: reviewed-binary-content-changed" in result.stdout


def test_renamed_infographic_has_no_review_exception(repository):
    stage(repository, "docs/images/unreviewed.png", (ROOT / INFOGRAPHIC).read_bytes())

    result = check(repository)

    assert result.returncode == 1
    assert "docs/images/unreviewed.png:0: oversized-file" in result.stdout


def test_unknown_binary_requires_review(repository):
    stage(repository, "docs/images/unknown.png", b"\x89PNG\r\n\x1a\n\xff")

    result = check(repository)

    assert result.returncode == 1
    assert "docs/images/unknown.png:0: non-text-file-review-required" in result.stdout


def test_oversized_text_is_still_rejected(repository):
    stage(repository, "docs/large.txt", b"a" * 2_000_001)

    result = check(repository)

    assert result.returncode == 1
    assert "docs/large.txt:0: oversized-file" in result.stdout


def test_forbidden_file_is_still_rejected(repository):
    stage(repository, ".env", b"MODEL_MODE=mock\n")

    result = check(repository)

    assert result.returncode == 1
    assert ".env:0: forbidden-file" in result.stdout


def test_secret_detection_remains_active_and_redacted(repository):
    synthetic_token = "ghp_" + "A" * 30
    stage(repository, "config.txt", synthetic_token.encode())

    result = check(repository)

    assert result.returncode == 1
    assert "config.txt:1: provider-token" in result.stdout
    assert synthetic_token not in result.stdout + result.stderr


def test_reads_staged_content_instead_of_worktree(repository):
    image = stage(repository, INFOGRAPHIC, (ROOT / INFOGRAPHIC).read_bytes())
    image.write_bytes(b"unreviewed worktree content")

    result = check(repository)

    assert result.returncode == 0, result.stdout + result.stderr


def test_clean_worktree_does_not_hide_changed_staged_image(repository):
    image = stage(repository, INFOGRAPHIC, b"unreviewed staged content")
    image.write_bytes((ROOT / INFOGRAPHIC).read_bytes())

    result = check(repository)

    assert result.returncode == 1
    assert f"{INFOGRAPHIC}:0: reviewed-binary-content-changed" in result.stdout


def test_empty_index_is_rejected(repository):
    result = check(repository)

    assert result.returncode == 1
    assert "Scanned 0 indexed files; 0 findings." in result.stdout
