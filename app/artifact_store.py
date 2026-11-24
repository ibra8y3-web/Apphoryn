from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ArtifactStoreError(ValueError):
    """Raised when an artifact cannot be safely persisted in Apphoryn storage."""


class ArtifactStore:
    """Atomic, content-addressed storage for validated installable artifacts.

    Files are stored under objects/<first-two-hex>/<sha256><extension>. The
    original publisher filename stays in the catalogue metadata, while the
    physical object identity is its SHA-256 digest. This deduplicates identical
    binaries across versions and applications without confusing their names.
    """

    def __init__(self, root: str | Path):
        configured_root = Path(root)
        configured_root.mkdir(parents=True, exist_ok=True)
        if configured_root.is_symlink():
            raise ArtifactStoreError("Artifact storage root must not be a symbolic link.")
        self.root = configured_root.resolve()

    @staticmethod
    def _hash_file(path: Path, max_bytes: int | None = None) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ArtifactStoreError("Artifact exceeds the configured storage size limit.")
                    digest.update(chunk)
        except OSError as exc:
            raise ArtifactStoreError(f"Could not read artifact for storage: {exc}") from exc
        return size, digest.hexdigest()

    def _target_for(self, digest: str, suffix: str) -> Path:
        directory = self.root / "objects" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        resolved_directory = directory.resolve()
        if self.root != resolved_directory and self.root not in resolved_directory.parents:
            raise ArtifactStoreError("Artifact object directory resolves outside the configured storage root.")
        if directory.is_symlink():
            raise ArtifactStoreError("Artifact object directory must not be a symbolic link.")
        target = directory / f"{digest}{suffix.lower()}"
        if target.is_symlink():
            raise ArtifactStoreError("Refusing to overwrite a symbolic link in artifact storage.")
        return target

    def put(
        self,
        source: str | Path,
        *,
        expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> Path:
        """Copy a regular file into the store and return its stable object path."""
        source_path = Path(source)
        if source_path.is_symlink() or not source_path.is_file():
            raise ArtifactStoreError("Only regular, non-symlink files can be stored.")
        suffix = source_path.suffix.lower()
        if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
            raise ArtifactStoreError("Artifact filename must have a simple package extension.")

        initial_size, initial_digest = self._hash_file(source_path, max_bytes)
        if initial_size <= 0:
            raise ArtifactStoreError("Empty artifact files cannot be stored.")
        if expected_sha256:
            expected = str(expected_sha256).strip().lower()
            if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise ArtifactStoreError("Expected SHA-256 must be a 64-character hexadecimal digest.")
            if initial_digest != expected:
                raise ArtifactStoreError("Artifact SHA-256 does not match the expected digest.")

        target = self._target_for(initial_digest, suffix)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ArtifactStoreError("Existing content-addressed object is not a regular file.")
            existing_size, existing_digest = self._hash_file(target, max_bytes)
            if existing_size == initial_size and existing_digest == initial_digest:
                return target
            raise ArtifactStoreError("Existing content-addressed object failed its integrity check.")

        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{initial_digest}.", suffix=".part", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            copied_size = 0
            copied_digest = hashlib.sha256()
            with source_path.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                descriptor = -1
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    copied_size += len(chunk)
                    if max_bytes is not None and copied_size > max_bytes:
                        raise ArtifactStoreError("Artifact exceeded the configured storage size limit while copying.")
                    copied_digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())

            if copied_size != initial_size or copied_digest.hexdigest() != initial_digest:
                raise ArtifactStoreError("Artifact changed while it was being copied into storage.")

            # Re-check after writing to handle another worker winning the same
            # content-addressed object race.
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise ArtifactStoreError("Concurrent object is not a regular file.")
                existing_size, existing_digest = self._hash_file(target, max_bytes)
                if existing_size != copied_size or existing_digest != initial_digest:
                    raise ArtifactStoreError("Concurrent content-addressed object failed its integrity check.")
                temporary_path.unlink(missing_ok=True)
                return target

            os.replace(temporary_path, target)
            temporary_path = None
            return target
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError(f"Could not commit artifact into Apphoryn storage: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def contains(self, path: str | Path) -> bool:
        """Return true only for an existing regular object beneath this store."""
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            return False
        resolved = candidate.resolve()
        return self.root == resolved or self.root in resolved.parents

