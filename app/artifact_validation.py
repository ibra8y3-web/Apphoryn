from __future__ import annotations

import os
import re
import shutil
import struct
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
    """Accept runnable PE application images, not DLLs or header-only lookalikes."""
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            return False
        stream.seek(0x3C)
        pointer = stream.read(4)
        if len(pointer) != 4:
            return False
        offset = struct.unpack("<I", pointer)[0]
        if offset < 64 or offset > file_size - 24:
            return False
        stream.seek(offset)
        headers = stream.read(24)
        if len(headers) != 24 or headers[:4] != bytes((0x50, 0x45, 0, 0)):
            return False

        machine = struct.unpack("<H", headers[4:6])[0]
        section_count = struct.unpack("<H", headers[6:8])[0]
        optional_size = struct.unpack("<H", headers[20:22])[0]
        characteristics = struct.unpack("<H", headers[22:24])[0]
        known_machines = {0x014C, 0x8664, 0x01C0, 0x01C2, 0x01C4, 0xAA64}
        if machine not in known_machines or not 1 <= section_count <= 96:
            return False
        if not (characteristics & 0x0002) or (characteristics & 0x2000):
            return False
        if optional_size < 96 or offset + 24 + optional_size > file_size:
            return False

        optional_header = stream.read(optional_size)
        if len(optional_header) != optional_size:
            return False
        magic = struct.unpack("<H", optional_header[:2])[0]
        if magic not in {0x10B, 0x20B}:
            return False
        entry_point = struct.unpack("<I", optional_header[16:20])[0]
        if entry_point == 0:
            return False

        section_table = offset + 24 + optional_size
        section_table_end = section_table + section_count * 40
        if section_table_end > file_size:
            return False
        stream.seek(section_table)
        entry_is_in_executable_section = False
        for _ in range(section_count):
            section = stream.read(40)
            if len(section) != 40:
                return False
            virtual_size = struct.unpack("<I", section[8:12])[0]
            virtual_address = struct.unpack("<I", section[12:16])[0]
            raw_size = struct.unpack("<I", section[16:20])[0]
            raw_pointer = struct.unpack("<I", section[20:24])[0]
            section_flags = struct.unpack("<I", section[36:40])[0]
            if not raw_size or raw_pointer < section_table_end or raw_pointer + raw_size > file_size:
                continue
            if not (section_flags & 0x20000000):
                continue
            span = max(virtual_size, raw_size)
            if virtual_address <= entry_point < virtual_address + span:
                entry_is_in_executable_section = True
        return entry_is_in_executable_section


def _elf_header(path: Path) -> bytes:
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(64)
    if len(header) < 20 or header[:4] != bytes((0x7F, 0x45, 0x4C, 0x46)):
        raise ArtifactValidationError("File does not contain a valid ELF header.")
    if header[4] not in (1, 2) or header[5] not in (1, 2) or header[6] != 1:
        raise ArtifactValidationError("ELF class, byte order, or version is invalid.")
    byte_order = "<" if header[5] == 1 else ">"
    is_64_bit = header[4] == 2
    minimum_size = 64 if is_64_bit else 52
    if len(header) < minimum_size:
        raise ArtifactValidationError("ELF executable header is truncated.")

    file_type = struct.unpack_from(byte_order + "H", header, 16)[0]
    entry_point = struct.unpack_from(byte_order + ("Q" if is_64_bit else "I"), header, 24)[0]
    if file_type not in {2, 3} or entry_point == 0:
        raise ArtifactValidationError(
            "ELF is not a runnable executable; relocatable objects and shared libraries are not store apps."
        )

    if is_64_bit:
        program_offset = struct.unpack_from(byte_order + "Q", header, 32)[0]
        entry_size = struct.unpack_from(byte_order + "H", header, 54)[0]
        entry_count = struct.unpack_from(byte_order + "H", header, 56)[0]
        minimum_entry_size = 56
    else:
        program_offset = struct.unpack_from(byte_order + "I", header, 28)[0]
        entry_size = struct.unpack_from(byte_order + "H", header, 42)[0]
        entry_count = struct.unpack_from(byte_order + "H", header, 44)[0]
        minimum_entry_size = 32
    if (
        program_offset == 0 or not 1 <= entry_count <= 1024
        or entry_size < minimum_entry_size
        or program_offset + entry_size * entry_count > file_size
    ):
        raise ArtifactValidationError("ELF has no valid program-header table for execution.")

    with path.open("rb") as stream:
        stream.seek(program_offset)
        table = stream.read(entry_size * entry_count)
    executable_segment_found = False
    for index in range(entry_count):
        segment = table[index * entry_size:(index + 1) * entry_size]
        if len(segment) < minimum_entry_size:
            break
        segment_type = struct.unpack_from(byte_order + "I", segment, 0)[0]
        if is_64_bit:
            flags = struct.unpack_from(byte_order + "I", segment, 4)[0]
            file_offset = struct.unpack_from(byte_order + "Q", segment, 8)[0]
            virtual_address = struct.unpack_from(byte_order + "Q", segment, 16)[0]
            file_bytes = struct.unpack_from(byte_order + "Q", segment, 32)[0]
            memory_bytes = struct.unpack_from(byte_order + "Q", segment, 40)[0]
        else:
            file_offset = struct.unpack_from(byte_order + "I", segment, 4)[0]
            virtual_address = struct.unpack_from(byte_order + "I", segment, 8)[0]
            file_bytes = struct.unpack_from(byte_order + "I", segment, 16)[0]
            memory_bytes = struct.unpack_from(byte_order + "I", segment, 20)[0]
            flags = struct.unpack_from(byte_order + "I", segment, 24)[0]
        if (
            segment_type == 1 and (flags & 1) and file_bytes > 0 and memory_bytes > 0
            and file_offset + file_bytes <= file_size
            and virtual_address <= entry_point < virtual_address + memory_bytes
        ):
            executable_segment_found = True
            break
    if not executable_segment_found:
        raise ArtifactValidationError("ELF has no executable load segment containing its entry point.")
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
        manifest_name = next((name for name in members if name.upper() == "META-INF/MANIFEST.MF"), None)
        if manifest_name is None or not any(name.endswith(".class") for name in members):
            raise ArtifactValidationError("JAR lacks a manifest or compiled Java classes.")
        try:
            with zipfile.ZipFile(candidate) as archive:
                manifest_bytes = archive.read(manifest_name)
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            raise ArtifactValidationError("JAR manifest cannot be read.") from exc
        # Java manifest continuation lines start with one space; join them before
        # parsing so a wrapped Main-Class value is handled correctly.
        unfolded_manifest = re.sub(rb"\r?\n ", b"", manifest_bytes)
        if not re.search(rb"(?im)^Main-Class:\s*[^\s]+", unfolded_manifest):
            raise ArtifactValidationError(
                "JAR is not an executable application: its manifest has no Main-Class entry."
            )
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
