# Apphoryn desktop client

The Electron client embeds the same storefront assets in a sandboxed window with context isolation and no Node integration in the renderer.

## Build

From this directory:

```bash
npm install
npm test
npm run dist
```

The CI workflow builds Windows NSIS, Linux AppImage/DEB/RPM and macOS PKG/DMG packages on native GitHub runners.

## Connect the catalogue

Open Settings and set the HTTPS base address of the deployed Apphoryn FastAPI service. The native installer bridge downloads only from the Apphoryn `/api/v1/downloads/{artifact_id}` endpoint and checks the catalogue SHA-256 before handing a package to the operating system.

## Supported install actions

- Windows: `.exe` and `.msi` (the installer may display Windows/UAC confirmation).
- Linux: `.deb` and `.rpm` through `pkexec`; `.AppImage` is made executable and launched as a portable application.
- macOS: `.pkg` through the native privileged installer prompt.

A `.dmg` is a disk-image container and is not presented as a one-click install package in the current installer bridge. Platform signing/notarization certificates are not configured by this development build.
