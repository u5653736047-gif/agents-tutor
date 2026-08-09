"""Read-only filesystem access constrained to user-authorized workspace roots."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path, PurePosixPath

DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_READ_BYTES = 100 * 1024
DEFAULT_MAX_READ_LINES = 1_000
DEFAULT_MAX_LINE_CHARS = 2_000
DEFAULT_MAX_RESULTS = 100
DEFAULT_MAX_SCAN_FILES = 5_000
DEFAULT_MAX_SEARCH_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_SEARCH_FILES = 500
_TRUNCATION_MARKER = "…"
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        ".tools",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        "venv",
        "node_modules",
        "__pycache__",
    }
)
_SENSITIVE_DIRECTORY_NAMES = frozenset({".ssh", ".aws", ".gcp", ".azure"})
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".envrc",
        ".git-credentials",
        ".netrc",
        "_netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "service-account.json",
        "service_account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})
_ACTIVE_WORKSPACE: ContextVar[WorkspaceFileSystem | None]


class WorkspaceFileErrorCode(str, Enum):
    """Stable, non-sensitive failure categories returned by file tools."""

    INVALID_PATH = "invalid_path"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    NOT_A_DIRECTORY = "not_a_directory"
    SENSITIVE_FILE = "sensitive_file"
    BINARY_FILE = "binary_file"
    INVALID_ENCODING = "invalid_encoding"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_REQUEST = "invalid_request"
    ACCESS_DENIED = "access_denied"


_SAFE_ERROR_MESSAGES: dict[WorkspaceFileErrorCode, str] = {
    WorkspaceFileErrorCode.INVALID_PATH: "文件路径无效。",
    WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE: "只能访问当前会话已授权的工作区目录。",
    WorkspaceFileErrorCode.NOT_FOUND: "文件或目录不存在。",
    WorkspaceFileErrorCode.NOT_A_FILE: "指定路径不是文件。",
    WorkspaceFileErrorCode.NOT_A_DIRECTORY: "指定路径不是目录。",
    WorkspaceFileErrorCode.SENSITIVE_FILE: "该路径属于受保护的敏感文件。",
    WorkspaceFileErrorCode.BINARY_FILE: "该文件是二进制文件，不能作为文本读取。",
    WorkspaceFileErrorCode.INVALID_ENCODING: "该文件不是有效的 UTF-8 文本。",
    WorkspaceFileErrorCode.FILE_TOO_LARGE: "文件超过允许读取的大小上限。",
    WorkspaceFileErrorCode.INVALID_REQUEST: "文件工具参数无效。",
    WorkspaceFileErrorCode.ACCESS_DENIED: "无法安全访问该路径。",
}


class WorkspaceFileError(RuntimeError):
    """A filesystem failure whose public message is safe for the model and UI."""

    def __init__(self, code: WorkspaceFileErrorCode) -> None:
        self.code = code
        super().__init__(_SAFE_ERROR_MESSAGES[code])

    def as_result(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_code": self.code.value,
            "message": str(self),
        }


class WorkspaceFileSystem:
    """Expose bounded read/search operations within a canonical workspace root."""

    def __init__(
        self,
        root: str | Path,
        *,
        additional_roots: Sequence[str | Path] = (),
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        max_read_lines: int = DEFAULT_MAX_READ_LINES,
        max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
        max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
        max_search_bytes: int = DEFAULT_MAX_SEARCH_BYTES,
        max_search_files: int = DEFAULT_MAX_SEARCH_FILES,
    ) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_dir():
            raise ValueError("workspace root must be an existing directory")
        self.root = candidate.resolve(strict=True)
        resolved_additional: list[Path] = []
        for raw_root in additional_roots:
            additional = Path(raw_root).expanduser()
            if not additional.is_dir():
                raise ValueError("workspace root must be an existing directory")
            resolved = additional.resolve(strict=True)
            if resolved != self.root and resolved not in resolved_additional:
                resolved_additional.append(resolved)
        self.additional_roots = tuple(resolved_additional)
        self.roots = (self.root, *self.additional_roots)
        self.max_file_bytes = _positive(max_file_bytes, "max_file_bytes")
        self.max_read_bytes = _at_least(max_read_bytes, 8, "max_read_bytes")
        self.max_read_lines = _positive(max_read_lines, "max_read_lines")
        self.max_line_chars = _positive(max_line_chars, "max_line_chars")
        self.max_scan_files = _positive(max_scan_files, "max_scan_files")
        self.max_search_bytes = _positive(max_search_bytes, "max_search_bytes")
        self.max_search_files = _positive(max_search_files, "max_search_files")

    def workspace_info(self) -> dict[str, object]:
        """Return the exact roots that the user authorized for this session."""
        return {
            "ok": True,
            "workspace_name": self.root.name or "workspace",
            "root": str(self.root),
            "additional_roots": [str(root) for root in self.additional_roots],
            "access": "read_only",
            "path_format": "relative_or_absolute",
        }

    def resolve_directory(self, path: str = ".") -> Path:
        """Resolve an existing directory inside one authorized root.

        This is the capability boundary used by approved process tools for
        their working directory.  It intentionally returns no broader disk
        access primitive: relative traversal, protected paths and link/junction
        escapes are checked by the same resolver as the read-only tools.
        """
        resolved, _display, _root, _absolute = self._resolve_existing(
            path,
            expected="directory",
        )
        return resolved

    def read_file(
        self,
        path: str,
        *,
        line_offset: int = 1,
        n_lines: int = 200,
    ) -> dict[str, object]:
        """Read a UTF-8 text file with line numbering and strict output bounds."""
        if line_offset < 1 or not 1 <= n_lines <= self.max_read_lines:
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_REQUEST)
        resolved, display_path, _root, _absolute_display = self._resolve_existing(
            path,
            expected="file",
        )
        data = self._read_bytes(resolved)
        text = self._decode_text(data)
        lines = text.splitlines()
        start = min(line_offset - 1, len(lines))
        requested_lines = lines[start : start + n_lines]
        rendered: list[str] = []
        used_bytes = 0
        content_was_truncated = False

        for index, raw_line in enumerate(requested_lines, start=line_offset):
            line, char_truncated = _truncate_chars(raw_line, self.max_line_chars)
            prefix = f"{index}: "
            separator_bytes = 1 if rendered else 0
            remaining = self.max_read_bytes - used_bytes - separator_bytes
            candidate = f"{prefix}{line}"
            if len(candidate.encode("utf-8")) <= remaining:
                rendered.append(candidate)
                used_bytes += separator_bytes + len(candidate.encode("utf-8"))
                content_was_truncated = content_was_truncated or char_truncated
                continue
            fitted = _fit_line_to_bytes(prefix, line, remaining)
            if fitted is not None:
                rendered.append(fitted)
                used_bytes += separator_bytes + len(fitted.encode("utf-8"))
            content_was_truncated = True
            break

        returned = len(rendered)
        next_offset = line_offset + returned if start + returned < len(lines) else None
        return {
            "ok": True,
            "path": display_path,
            "content": "\n".join(rendered),
            "line_offset": line_offset,
            "lines_returned": returned,
            "total_lines": len(lines),
            "truncated": content_was_truncated or next_offset is not None,
            "next_line_offset": next_offset,
        }

    def list_files(
        self,
        path: str = ".",
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> dict[str, object]:
        """List immediate safe children without exposing protected entries."""
        _validate_result_limit(max_results)
        directory, display_path, root, absolute_display = self._resolve_existing(
            path,
            expected="directory",
        )
        entries: list[dict[str, object]] = []
        filtered_count = 0
        truncated = False
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.ACCESS_DENIED) from error

        for child in children:
            safe = self._safe_discovery_candidate(
                child,
                root=root,
                absolute_display=absolute_display,
            )
            if safe is None:
                filtered_count += 1
                continue
            resolved, child_display = safe
            entry: dict[str, object]
            if child.is_symlink():
                entry = {"path": child_display, "type": "symlink"}
                if resolved.is_file():
                    entry["size"] = resolved.stat().st_size
            elif resolved.is_dir():
                entry = {"path": child_display, "type": "directory"}
            elif resolved.is_file():
                entry = {
                    "path": child_display,
                    "type": "file",
                    "size": resolved.stat().st_size,
                }
            else:
                filtered_count += 1
                continue
            if len(entries) >= max_results:
                truncated = True
                break
            entries.append(entry)
        return {
            "ok": True,
            "path": display_path,
            "entries": entries,
            "truncated": truncated,
            "filtered_count": filtered_count,
        }

    def glob_files(
        self,
        pattern: str,
        *,
        path: str = ".",
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> dict[str, object]:
        """Find safe files matching a workspace-relative glob pattern."""
        _validate_result_limit(max_results)
        normalized_pattern = self._validate_pattern(pattern)
        directory, display_path, root, absolute_display = self._resolve_existing(
            path,
            expected="directory",
        )
        files, filtered_count, scan_truncated = self._walk_files(
            directory,
            root=root,
            absolute_display=absolute_display,
        )
        matches = sorted(
            (
                display
                for lexical, _resolved, display in files
                if _matches_pattern(
                    lexical.relative_to(directory).as_posix(),
                    normalized_pattern,
                )
            ),
            key=str.casefold,
        )
        return {
            "ok": True,
            "path": display_path,
            "pattern": normalized_pattern,
            "matches": matches[:max_results],
            "truncated": scan_truncated or len(matches) > max_results,
            "filtered_count": filtered_count,
        }

    def grep_files(
        self,
        query: str,
        *,
        path: str = ".",
        file_pattern: str = "**/*",
        case_sensitive: bool = False,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> dict[str, object]:
        """Search for a literal string in bounded, safe UTF-8 workspace files."""
        if not query or len(query) > 500:
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_REQUEST)
        _validate_result_limit(max_results)
        normalized_pattern = self._validate_pattern(file_pattern)
        directory, display_path, root, absolute_display = self._resolve_existing(
            path,
            expected="directory",
        )
        files, filtered_count, scan_truncated = self._walk_files(
            directory,
            root=root,
            absolute_display=absolute_display,
        )
        files = [
            item
            for item in files
            if _matches_pattern(item[0].relative_to(directory).as_posix(), normalized_pattern)
        ]
        files.sort(key=lambda item: item[2].casefold())
        matches: list[dict[str, object]] = []
        files_scanned = 0
        bytes_scanned = 0
        truncated = scan_truncated
        needle = query if case_sensitive else query.casefold()

        for _lexical, resolved, file_display in files:
            if files_scanned >= self.max_search_files:
                truncated = True
                break
            try:
                size = resolved.stat().st_size
            except OSError:
                filtered_count += 1
                continue
            if size > self.max_file_bytes:
                filtered_count += 1
                continue
            if bytes_scanned + size > self.max_search_bytes:
                truncated = True
                break
            try:
                data = resolved.read_bytes()
                text = self._decode_text(data)
            except (OSError, WorkspaceFileError):
                filtered_count += 1
                continue
            files_scanned += 1
            bytes_scanned += len(data)
            for line_number, raw_line in enumerate(text.splitlines(), start=1):
                haystack = raw_line if case_sensitive else raw_line.casefold()
                if needle not in haystack:
                    continue
                line, _ = _truncate_chars(raw_line, self.max_line_chars)
                matches.append(
                    {
                        "path": file_display,
                        "line_number": line_number,
                        "line": line,
                    }
                )
                if len(matches) > max_results:
                    truncated = True
                    break
            if len(matches) > max_results:
                break
        return {
            "ok": True,
            "path": display_path,
            "query": query,
            "matches": matches[:max_results],
            "files_scanned": files_scanned,
            "truncated": truncated,
            "filtered_count": filtered_count,
        }

    def _resolve_existing(
        self,
        path: str,
        *,
        expected: str,
    ) -> tuple[Path, str, Path, bool]:
        candidate, absolute_display = self._candidate_path(path)
        lexical = Path(os.path.abspath(candidate))
        lexical_root = self._authorized_root_for(lexical)
        if lexical_root is None:
            raise WorkspaceFileError(WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE)
        lexical_relative = self._relative_to_root(lexical, lexical_root)
        self._assert_allowed(lexical_relative)
        try:
            resolved = lexical.resolve(strict=True)
        except FileNotFoundError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.NOT_FOUND) from error
        except PermissionError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.ACCESS_DENIED) from error
        except OSError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH) from error
        resolved_root = self._authorized_root_for(resolved)
        # A link or junction may not silently jump from one authorized root to
        # another; callers can address the second root by its own absolute path.
        if resolved_root is None or resolved_root != lexical_root:
            raise WorkspaceFileError(WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE)
        resolved_relative = self._relative_to_root(resolved, resolved_root)
        self._assert_allowed(resolved_relative)
        if expected == "file" and not resolved.is_file():
            raise WorkspaceFileError(WorkspaceFileErrorCode.NOT_A_FILE)
        if expected == "directory" and not resolved.is_dir():
            raise WorkspaceFileError(WorkspaceFileErrorCode.NOT_A_DIRECTORY)
        display = self._display_path(lexical, lexical_root, absolute_display)
        return resolved, display, lexical_root, absolute_display

    def _candidate_path(self, path: str) -> tuple[Path, bool]:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH)
        expanded = Path(path).expanduser()
        if expanded.is_absolute():
            return expanded, True
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if any(part == ".." for part in parts):
            raise WorkspaceFileError(WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE)
        if any(":" in part for part in parts):
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH)
        return self.root / Path(normalized), False

    def _validate_pattern(self, pattern: str) -> str:
        if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 500:
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_REQUEST)
        normalized = pattern.replace("\\", "/")
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or (len(normalized) >= 2 and normalized[1] == ":")
            or any(part == ".." for part in parts)
        ):
            raise WorkspaceFileError(WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE)
        if "\x00" in normalized or any(":" in part for part in parts):
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH)
        return normalized

    def _authorized_root_for(self, path: Path) -> Path | None:
        candidates = [
            root
            for root in self.roots
            if path == root or path.is_relative_to(root)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda root: len(root.parts))

    @staticmethod
    def _relative_to_root(path: Path, root: Path) -> Path:
        try:
            return path.relative_to(root)
        except ValueError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.PATH_OUTSIDE_WORKSPACE) from error

    @staticmethod
    def _display_path(path: Path, root: Path, absolute: bool) -> str:
        if absolute:
            return str(path)
        relative = path.relative_to(root).as_posix()
        return "." if relative in ("", ".") else relative

    def _assert_allowed(self, relative: Path) -> None:
        lowered_parts = tuple(part.casefold() for part in relative.parts)
        if any(part in _IGNORED_DIRECTORY_NAMES for part in lowered_parts):
            raise WorkspaceFileError(WorkspaceFileErrorCode.SENSITIVE_FILE)
        if any(part in _SENSITIVE_DIRECTORY_NAMES for part in lowered_parts):
            raise WorkspaceFileError(WorkspaceFileErrorCode.SENSITIVE_FILE)
        if not lowered_parts:
            return
        name = lowered_parts[-1]
        if name in _ENV_TEMPLATE_NAMES:
            return
        if (
            name == ".env"
            or name.startswith(".env.")
            or name in _SENSITIVE_FILE_NAMES
            or Path(name).suffix in _SENSITIVE_SUFFIXES
        ):
            raise WorkspaceFileError(WorkspaceFileErrorCode.SENSITIVE_FILE)

    def _safe_discovery_candidate(
        self,
        candidate: Path,
        *,
        root: Path,
        absolute_display: bool,
    ) -> tuple[Path, str] | None:
        try:
            lexical_relative = candidate.relative_to(root)
            self._assert_allowed(lexical_relative)
            resolved = candidate.resolve(strict=True)
            if self._authorized_root_for(resolved) != root:
                return None
            resolved_relative = self._relative_to_root(resolved, root)
            self._assert_allowed(resolved_relative)
        except (OSError, WorkspaceFileError, ValueError):
            return None
        return resolved, self._display_path(candidate, root, absolute_display)

    def _walk_files(
        self,
        directory: Path,
        *,
        root: Path,
        absolute_display: bool,
    ) -> tuple[list[tuple[Path, Path, str]], int, bool]:
        found: list[tuple[Path, Path, str]] = []
        filtered_count = 0
        scanned_files = 0
        scan_truncated = False

        for current_raw, directory_names, file_names in os.walk(
            directory, topdown=True, followlinks=False
        ):
            current = Path(current_raw)
            safe_directories: list[str] = []
            for name in sorted(directory_names, key=str.casefold):
                candidate = current / name
                safe = self._safe_discovery_candidate(
                    candidate,
                    root=root,
                    absolute_display=absolute_display,
                )
                if safe is None or candidate.is_symlink() or not safe[0].is_dir():
                    filtered_count += 1
                    continue
                safe_directories.append(name)
            directory_names[:] = safe_directories

            for name in sorted(file_names, key=str.casefold):
                scanned_files += 1
                if scanned_files > self.max_scan_files:
                    scan_truncated = True
                    break
                candidate = current / name
                safe = self._safe_discovery_candidate(
                    candidate,
                    root=root,
                    absolute_display=absolute_display,
                )
                if safe is None or not safe[0].is_file():
                    filtered_count += 1
                    continue
                found.append((candidate, safe[0], safe[1]))
            if scan_truncated:
                break
        return found, filtered_count, scan_truncated

    def _read_bytes(self, path: Path) -> bytes:
        try:
            size = path.stat().st_size
            if size > self.max_file_bytes:
                raise WorkspaceFileError(WorkspaceFileErrorCode.FILE_TOO_LARGE)
            data = path.read_bytes()
        except WorkspaceFileError:
            raise
        except PermissionError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.ACCESS_DENIED) from error
        except OSError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.ACCESS_DENIED) from error
        if len(data) > self.max_file_bytes:
            raise WorkspaceFileError(WorkspaceFileErrorCode.FILE_TOO_LARGE)
        return data

    @staticmethod
    def _decode_text(data: bytes) -> str:
        if b"\x00" in data[:8192]:
            raise WorkspaceFileError(WorkspaceFileErrorCode.BINARY_FILE)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_ENCODING) from error


_ACTIVE_WORKSPACE = ContextVar("active_workspace_filesystem", default=None)


@contextmanager
def workspace_scope(
    root: str | Path | None,
    *,
    additional_roots: Sequence[str | Path] = (),
) -> Iterator[None]:
    """Bind one session's filesystem capability for nested Agent tool calls."""
    if root is None:
        yield
        return
    token = _ACTIVE_WORKSPACE.set(
        WorkspaceFileSystem(root, additional_roots=additional_roots)
    )
    try:
        yield
    finally:
        _ACTIVE_WORKSPACE.reset(token)


def active_workspace_filesystem(default: WorkspaceFileSystem) -> WorkspaceFileSystem:
    """Return the current session capability, or an explicit startup fallback."""
    return _ACTIVE_WORKSPACE.get() or default


def _matches_pattern(relative_path: str, pattern: str) -> bool:
    path = PurePosixPath(relative_path)
    if path.match(pattern):
        return True
    return pattern.startswith("**/") and path.match(pattern[3:])


def _truncate_chars(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return f"{value[: max(0, limit - 1)]}{_TRUNCATION_MARKER}", True


def _fit_line_to_bytes(prefix: str, line: str, budget: int) -> str | None:
    prefix_bytes = len(prefix.encode("utf-8"))
    marker_bytes = len(_TRUNCATION_MARKER.encode("utf-8"))
    available = budget - prefix_bytes - marker_bytes
    if available < 0:
        return None
    kept: list[str] = []
    used = 0
    for character in line:
        width = len(character.encode("utf-8"))
        if used + width > available:
            break
        kept.append(character)
        used += width
    return f"{prefix}{''.join(kept)}{_TRUNCATION_MARKER}"


def _validate_result_limit(value: int) -> None:
    if not 1 <= value <= DEFAULT_MAX_RESULTS:
        raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_REQUEST)


def _positive(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _at_least(value: int, minimum: int, field_name: str) -> int:
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


__all__ = [
    "WorkspaceFileError",
    "WorkspaceFileErrorCode",
    "WorkspaceFileSystem",
    "active_workspace_filesystem",
    "workspace_scope",
]
