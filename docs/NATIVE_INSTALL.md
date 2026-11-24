# Native Apphoryn clients and in-app installation

## Product architecture

- `apps/web` remains the shared UI source for the PWA and native clients.
- `apps/android` embeds the UI and exposes a narrow JavaScript interface for verified APK installation.
- `apps/desktop` embeds the UI in a sandboxed Electron renderer and exposes an allow-listed installation IPC handler.
- The storefront uses the Apphoryn API only for artifact delivery. It no longer renders upstream GitHub release download URLs or a source-repository hyperlink in the app details modal. Source provenance remains stored by the backend.
- The API streams Apphoryn-owned release assets instead of returning an HTTP redirect to GitHub.

## Required deployment setting

The native clients do not contain API credentials or provider secrets. Configure the HTTPS origin of the deployed Apphoryn FastAPI service in Settings. A static catalogue can be displayed without that API connection, but real downloads and installations require the API service to be online.

## Install safety

The install bridge validates the platform and filename, downloads through `/api/v1/downloads/{artifact_id}`, enforces a size limit and compares the downloaded bytes against the catalogue SHA-256 digest before calling the operating-system installer. It never accepts arbitrary URLs from the renderer.

The operating system remains responsible for the final install prompt, signature checks, UAC/Polkit authentication and platform security controls. Apphoryn does not silently bypass these controls.

Current direct install adapters:
- Android APK and signed XAPK split bundles (XAPK with OBB expansion data is refused).
- Windows EXE/MSI/MSIX/APPX (MSIX/APPX are handed to Windows AppX deployment and retain OS signature policy).
- Linux DEB/RPM/AppImage (AppImage runs portably; it is not registered as a system package).
- macOS PKG/DMG (DMG mounting requires exactly one `.app` bundle, which is copied into `/Applications` with the macOS administrator prompt).

XAPK support handles signed `base.apk` plus split APK files in one PackageInstaller session. XAPK bundles that include OBB expansion data remain unsupported and are rejected before installation. The Android development APK is debug-signed. Windows/macOS package code signing and macOS notarization require owner-managed certificates and are not configured.

## Automated builds

The `Build Apphoryn native store clients` workflow builds a debug Android APK and OS-native desktop installers on corresponding GitHub-hosted runners. It uploads them as workflow artifacts for review; this change does not publish an unsigned development package as a production release.


## CORS for the embedded clients

Set the server environment variable `APPHORYN_CORS_ORIGINS` to the exact embedded origins, for example:

```env
APPHORYN_CORS_ORIGINS=http://127.0.0.1:8765,apphoryn://store
```

The Android embedded asset server binds to loopback port `8765` so the origin is stable. The Electron app uses `apphoryn://store`. Keep the API itself on HTTPS; the local Android HTTP origin serves only packaged UI assets. Do not use a wildcard CORS origin for admin endpoints.
