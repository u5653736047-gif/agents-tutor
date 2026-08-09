"""Security and behavior contract for the read-only workspace filesystem."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.filesystem import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
    WorkspaceFileSystem,
)


def _error_code(error: pytest.ExceptionInfo[WorkspaceFileError]) -> WorkspaceFileErrorCode:
    return error.value.code


def test_workspace_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        WorkspaceFileSystem(tmp_path / "missing")

    regular_file = tmp_path / "lesson.txt"
    regular_file.write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match="existing directory"):
        WorkspaceFileSystem(regular_file)


def test_workspace_info_exposes_the_authorized_absolute_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "course-project"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()

    result = WorkspaceFileSystem(
        workspace,
        additional_roots=[shared],
    ).workspace_info()

    assert result == {
        "ok": True,
        "workspace_name": "course-project",
        "root": str(workspace.resolve()),
        "additional_roots": [str(shared.resolve())],
        "access": "read_only",
        "path_format": "relative_or_absolute",
    }


@pytest.mark.parametrize(
    "requested_path",
    ["../outside.txt", "docs/../../outside.txt"],
)
def test_parent_traversal_is_rejected(tmp_path: Path, requested_path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    filesystem = WorkspaceFileSystem(workspace)

    with pytest.raises(WorkspaceFileError) as error:
        filesystem.read_file(requested_path)

    assert _error_code(error) is WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE


def test_absolute_paths_are_allowed_inside_the_primary_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lesson = workspace / "lesson.txt"
    lesson.write_text("content", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace)

    result = filesystem.read_file(str(lesson))

    assert result["content"] == "1: content"
    assert result["path"] == str(lesson.resolve())


def test_absolute_paths_require_an_authorized_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace)

    with pytest.raises(WorkspaceFileError) as error:
        filesystem.read_file(str(outside))

    assert _error_code(error) is WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE


def test_additional_workspace_root_accepts_absolute_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    shared_file = shared / "notes.md"
    shared_file.write_text("shared notes", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace, additional_roots=[shared])

    read = filesystem.read_file(str(shared_file))
    listed = filesystem.list_files(str(shared))

    assert read["content"] == "1: shared notes"
    assert read["path"] == str(shared_file.resolve())
    assert listed["path"] == str(shared.resolve())
    assert listed["entries"] == [
        {"path": str(shared_file.resolve()), "type": "file", "size": 12}
    ]


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    filesystem = WorkspaceFileSystem(workspace)

    with pytest.raises(WorkspaceFileError) as file_error:
        filesystem.read_file("link.txt")

    assert _error_code(file_error) is WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        ".ssh/id_ed25519",
        ".aws/credentials",
        "credentials.pem",
        ".git-credentials",
    ],
)
def test_sensitive_files_are_rejected(tmp_path: Path, relative_path: str) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace)

    with pytest.raises(WorkspaceFileError) as error:
        filesystem.read_file(relative_path)

    assert _error_code(error) is WorkspaceFileErrorCode.SENSITIVE_FILE


def test_env_template_is_readable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env.example").write_text("API_KEY=replace-me\n", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace)

    result = filesystem.read_file(".env.example")

    assert result["ok"] is True
    assert result["content"] == "1: API_KEY=replace-me"


def test_binary_files_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "image.bin").write_bytes(b"header\x00payload")
    filesystem = WorkspaceFileSystem(workspace)

    with pytest.raises(WorkspaceFileError) as error:
        filesystem.read_file("image.bin")

    assert _error_code(error) is WorkspaceFileErrorCode.BINARY_FILE


def test_oversized_files_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.txt").write_text("123456", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace, max_file_bytes=5)

    with pytest.raises(WorkspaceFileError) as error:
        filesystem.read_file("large.txt")

    assert _error_code(error) is WorkspaceFileErrorCode.FILE_TOO_LARGE


def test_read_file_returns_numbered_paginated_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lesson.txt").write_text(
        "alpha\nbeta\ngamma\ndelta\n",
        encoding="utf-8",
    )
    filesystem = WorkspaceFileSystem(workspace)

    result = filesystem.read_file("lesson.txt", line_offset=2, n_lines=2)

    assert result == {
        "ok": True,
        "path": "lesson.txt",
        "content": "2: beta\n3: gamma",
        "line_offset": 2,
        "lines_returned": 2,
        "total_lines": 4,
        "truncated": True,
        "next_line_offset": 4,
    }


def test_read_file_enforces_output_byte_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lesson.txt").write_text("abcdefghij\nsecond\n", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace, max_read_bytes=8)

    result = filesystem.read_file("lesson.txt", n_lines=2)

    assert result["content"] == "1: ab…"
    assert result["lines_returned"] == 1
    assert result["truncated"] is True
    assert result["next_line_offset"] == 2


def test_list_files_filters_sensitive_ignored_and_escaping_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lesson.txt").write_text("lesson", encoding="utf-8")
    (workspace / ".env").write_text("secret", encoding="utf-8")
    for ignored_name in (".git", ".tools"):
        ignored = workspace / ignored_name
        ignored.mkdir()
        (ignored / "config").write_text("token", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "outside-link.txt")
    except OSError:
        pass
    filesystem = WorkspaceFileSystem(workspace)

    result = filesystem.list_files(".")

    assert result["entries"] == [{"path": "lesson.txt", "type": "file", "size": 6}]
    filtered_count = result["filtered_count"]
    assert isinstance(filtered_count, int)
    assert filtered_count >= 3
    assert all(".env" not in entry["path"] for entry in result["entries"])


def test_glob_files_is_bounded_and_uses_workspace_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.md"):
        (docs / name).write_text(name, encoding="utf-8")
    (docs / ".env").write_text("secret", encoding="utf-8")
    filesystem = WorkspaceFileSystem(workspace)

    result = filesystem.glob_files("**/*.txt", max_results=1)

    assert result["matches"] == ["docs/a.txt"]
    assert result["truncated"] is True
    with pytest.raises(WorkspaceFileError) as error:
        filesystem.glob_files("../*.txt")
    assert _error_code(error) is WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE


def test_grep_files_returns_literal_line_matches_and_skips_binary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    (docs / "one.txt").write_text("Alpha topic\nother\n", encoding="utf-8")
    (docs / "two.md").write_text("alpha topic again\n", encoding="utf-8")
    (docs / "binary.txt").write_bytes(b"alpha\x00topic")
    filesystem = WorkspaceFileSystem(workspace)

    result = filesystem.grep_files(
        "alpha topic",
        file_pattern="**/*",
        case_sensitive=False,
    )

    assert result["matches"] == [
        {"path": "docs/one.txt", "line_number": 1, "line": "Alpha topic"},
        {
            "path": "docs/two.md",
            "line_number": 1,
            "line": "alpha topic again",
        },
    ]
    assert result["files_scanned"] == 2
    filtered_count = result["filtered_count"]
    assert isinstance(filtered_count, int)
    assert filtered_count >= 1
