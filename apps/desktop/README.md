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

- Windows: `.exe`, `.msi`, `.msix` and `.appx` (MSIX/APPX use the native Windows AppX deployment service and retain Windows signature/UAC policy).
- Linux: `.deb` and `.rpm` through `pkexec`; `.AppImage` is made executable and launched as a portable application.
- macOS: `.pkg` through the native privileged installer prompt, and `.dmg` by mounting the image, locating one unambiguous `.app` bundle, and copying it to `/Applications` through the macOS administrator prompt.

Platform signing/notarization certificates are not configured by this development build. macOS DMGs with multiple app bundles or no app bundle are refused rather than guessed.
