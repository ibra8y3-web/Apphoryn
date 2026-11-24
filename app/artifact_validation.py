from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import subprocess
import zipfile
from pathlib import Path


class ArtifactValidationError(ValueError):
    """Raised when a file is not structurally valid for its advertised package type."""


_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def _zip_members(path: Path) -> set[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 50000:
                raise ArtifactValidationError("Package archive has no members or an excessive member count.")
            expanded_total = 0
            members = set()
            for info in infos:
                name = info.filename
                normalized = name.replace("\\", "/")
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    raise ArtifactValidationError("Package archive contains an unsafe member path.")
                if info.file_size < 0 or info.compress_size < 0 or info.file_size > 1024 * 1024 * 1024:
                    raise ArtifactValidationError("Package member exceeds the 1 GiB expansion limit.")
                expanded_total += info.file_size
                if expanded_total > 2 * 1024 * 1024 * 1024:
                    raise ArtifactValidationError("Package archive exceeds the 2 GiB expanded-size limit.")
                if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > 300:
                    raise ArtifactValidationError("Package archive contains a suspicious compression ratio.")
                members.add(name)
            bad_member = archive.testzip()
            if bad_member:
                raise ArtifactValidationError(f"Archive CRC check failed for {bad_member!r}.")
            return members
    except ArtifactValidationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ArtifactValidationError("Package is not a readable ZIP container.") from exc


def _is_pe(path: Path) -> bool:
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            return False
        stream.seek(0x3C)
        pointer = stream.read(4)
        if len(pointer) != 4:
            return False
        offset = struct.unpack("<I", pointer)[0]
        if offset < 64 or offset > path.stat().st_size - 24:
            return False
        stream.seek(offset)
        return stream.read(4) == b"PE\x00\x00"


def _elf_header(path: Path) -> bytes:
    with path.open("rb") as stream:
        header = stream.read(64)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ArtifactValidationError("File does not contain a valid ELF header.")
    if header[4] not in (1, 2) or header[5] not in (1, 2) or header[6] != 1:
        raise ArtifactValidationError("ELF class, byte order, or version is invalid.")
    return header


def _ar_members(path: Path) -> set[str]:
    members: set[str] = set()
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            return members
        while True:
            header = stream.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b"\x60\n":
                return set()
            raw_name = header[:16].decode("ascii", errors="ignore").strip()
            name = raw_name.rstrip("/")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError:
                return set()
            if size < 0:
                return set()
            if name and not name.startswith("#1/"):
                members.add(name)
            stream.seek(size + (size % 2), os.SEEK_CUR)
    return members


def _verify_apk_signature(path: Path, *, required: bool) -> tuple[str, str | None]:
    apksigner = shutil.which("apksigner")
    if not apksigner:
        if required:
            raise ArtifactValidationError(
                "APK signature verification is mandatory, but apksigner is not installed on this worker."
            )
        return "not_checked", None
    try:
        result = subprocess.run(
            [apksigner, "verify", "--verbose", "--print-certs", str(path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactValidationError(f"Could not execute apksigner: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "apksigner rejected the APK").strip()
        raise ArtifactValidationError(f"APK signature verification failed: {detail[:500]}")
    signer_text = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F:]{64,95})", signer_text)
    fingerprint = match.group(1).replace(":", "").lower() if match else None
    return "verified", fingerprint


def validate_artifact(path: str | Path, *, require_apk_signature: bool = True) -> dict[str, object]:
    """Validate package structure and (for APKs) cryptographic signing.

    This is an integrity/format gate, not a malware scan or a guarantee that an
    application is safe. APKs fail closed unless apksigner verifies their signature.
    """
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ArtifactValidationError("Artifact must be a regular, non-symlink file.")
    size = candidate.stat().st_size
    if size < 64:
        raise ArtifactValidationError("Artifact is too small to be a valid installable package.")

    suffix = candidate.suffix.lower()
    signature_status = "not_checked"
    signer_certificate_sha256 = None

    if suffix == ".apk":
        members = _zip_members(candidate)
        has_dex = any(name == "classes.dex" or (name.startswith("classes") and name.endswith(".dex")) for name in members)
        has_native = any(name.startswith("lib/") and name.endswith(".so") for name in members)
        if "AndroidManifest.xml" not in members or not (has_dex or has_native):
            raise ArtifactValidationError("APK lacks AndroidManifest.xml or executable DEX/native code.")
        signature_status, signer_certificate_sha256 = _verify_apk_signature(candidate, required=require_apk_signature)
    elif suffix == ".xapk":
        members = _zip_members(candidate)
        apk_entries = []
        base_found = False
        if any(name.lower().endswith(".obb") for name in members):
            raise ArtifactValidationError("XAPK includes OBB expansion data; this installer supports APK splits only.")
        try:
            with zipfile.ZipFile(candidate) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".apk"):
                        continue
                    normalized = info.filename.replace("\\", "/")
                    if normalized.startswith("/") or ".." in normalized.split("/"):
                        raise ArtifactValidationError("XAPK contains an unsafe APK member path.")
                    name = Path(normalized).name
                    if not name.lower().endswith(".apk") or not name[:-4] or any(not (char.isalnum() or char in "._-") for char in name[:-4]):
                        raise ArtifactValidationError("XAPK contains an unsafe APK member name.")
                    apk_entries.append((info, name))
                    if name.lower() == "base.apk":
                        base_found = True
                if not base_found or not apk_entries:
                    raise ArtifactValidationError("XAPK must contain base.apk and at least one installable APK.")
                if len(apk_entries) > 16:
                    raise ArtifactValidationError("XAPK has too many APK split files.")
                names = [name.casefold() for _, name in apk_entries]
                if len(names) != len(set(names)):
                    raise ArtifactValidationError("XAPK contains duplicate APK split filenames.")
                signer_fingerprints: set[str] = set()
                with tempfile.TemporaryDirectory(prefix="apphoryn-xapk-") as temporary:
                    for index, (info, name) in enumerate(apk_entries):
                        nested_path = Path(temporary) / f"{index}-{name}"
                        total = 0
                        with archive.open(info) as source, nested_path.open("wb") as target:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > 512 * 1024 * 1024:
                                    raise ArtifactValidationError("An XAPK split exceeds the 512 MiB validation limit.")
                                target.write(chunk)
                        nested = validate_artifact(nested_path, require_apk_signature=require_apk_signature)
                        fingerprint = nested.get("signer_certificate_sha256")
                        if nested.get("signature_status") != "verified" or not fingerprint:
                            raise ArtifactValidationError(f"XAPK split {name} has no verified APK signer certificate.")
                        signer_fingerprints.add(str(fingerprint))
                if len(signer_fingerprints) != 1:
                    raise ArtifactValidationError("XAPK APK splits do not share the same signing certificate.")
                signature_status = "verified"
                signer_certificate_sha256 = next(iter(signer_fingerprints))
        except ArtifactValidationError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ArtifactValidationError("XAPK is not a readable split APK bundle.") from exc
    elif suffix == ".aab":
        members = _zip_members(candidate)
        if "BundleConfig.pb" not in members or "base/manifest/AndroidManifest.xml" not in members:
            raise ArtifactValidationError("AAB lacks its bundle configuration or base manifest.")
        if not any(name.startswith("base/dex/") and name.endswith(".dex") for name in members):
            raise ArtifactValidationError("AAB contains no base DEX code.")
    elif suffix in {".msix", ".appx"}:
        members = _zip_members(candidate)
        required = {"AppxManifest.xml", "AppxBlockMap.xml", "AppxSignature.p7x"}
        if not required.issubset(members):
            raise ArtifactValidationError("MSIX/APPX package lacks its manifest, block map, or signature.")
    elif suffix == ".jar":
        members = _zip_members(candidate)
        if "META-INF/MANIFEST.MF" not in members or not any(name.endswith(".class") for name in members):
            raise ArtifactValidationError("JAR lacks a manifest or compiled Java classes.")
    elif suffix == ".exe":
        if not _is_pe(candidate):
            raise ArtifactValidationError("EXE does not contain a valid Windows PE header.")
    elif suffix == ".msi":
        with candidate.open("rb") as stream:
            if stream.read(8) != bytes.fromhex("D0CF11E0A1B11AE1"):
                raise ArtifactValidationError("MSI does not contain an OLE compound-file header.")
    elif suffix == ".appimage":
        _elf_header(candidate)
        with candidate.open("rb") as stream:
            prefix = stream.read(128)
        if b"AI\x01" not in prefix and b"AI\x02" not in prefix:
            raise ArtifactValidationError("ELF file does not contain an AppImage runtime marker.")
    elif suffix == ".deb":
        members = _ar_members(candidate)
        if "debian-binary" not in members:
            raise ArtifactValidationError("DEB archive lacks its debian-binary member.")
        if not any(name.startswith("control.tar") for name in members):
            raise ArtifactValidationError("DEB archive lacks its control.tar payload.")
        if not any(name.startswith("data.tar") for name in members):
            raise ArtifactValidationError("DEB archive lacks its data.tar payload.")
    elif suffix == ".rpm":
        with candidate.open("rb") as stream:
            header = stream.read(112)
        if len(header) < 112 or header[:4] != bytes.fromhex("EDAB EEDB".replace(" ", "")):
            raise ArtifactValidationError("RPM does not contain a valid RPM lead/header.")
    elif suffix == ".dmg":
        with candidate.open("rb") as stream:
            stream.seek(-512, os.SEEK_END)
            trailer = stream.read(512)
        if len(trailer) != 512 or trailer[:4] != b"koly":
            raise ArtifactValidationError("DMG does not contain a UDIF koly trailer.")
    elif suffix == ".pkg":
        with candidate.open("rb") as stream:
            header = stream.read(28)
        if len(header) != 28 or header[:4] != b"xar!":
            raise ArtifactValidationError("PKG does not contain a valid XAR header.")
        header_size = struct.unpack(">H", header[4:6])[0]
        compressed_size = struct.unpack(">Q", header[8:16])[0]
        if header_size < 1 or 28 + header_size + compressed_size > size:
            raise ArtifactValidationError("PKG XAR header lengths are invalid.")
    elif suffix == ".bin":
        _elf_header(candidate)
    elif suffix == ".macosbin":
        with candidate.open("rb") as stream:
            if stream.read(4) not in _MACHO_MAGICS:
                raise ArtifactValidationError("macOS binary does not contain a recognized Mach-O header.")
    else:
        raise ArtifactValidationError(f"Unsupported installable package extension: {suffix or '(none)' }.")

    return {
        "valid": True,
        "format": suffix.lstrip("."),
        "size_bytes": size,
        "signature_status": signature_status,
        "signer_certificate_sha256": signer_certificate_sha256,
        "validator": "apphoryn-format-checks/1",
    }
