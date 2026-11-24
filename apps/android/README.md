# Apphoryn Android client

This is an installable Android application embedding the Apphoryn web UI from packaged assets.

## Build

Use JDK 17, Android SDK platform 35, Build Tools 35.0.0 and Gradle 8.11.1:

```bash
gradle -p apps/android --no-daemon assembleDebug
```

The development APK is written to `app/build/outputs/apk/debug/app-debug.apk` and is debug-key signed, not production/Play Store signed.

## Connect the catalogue

Open Settings and enter the HTTPS base address of the deployed Apphoryn FastAPI service. The native client calls the Apphoryn download API; it does not open GitHub or a third-party download page.

## Install flow

The Android client installs verified `.apk` packages and signed `.xapk` split bundles. APK files are downloaded into the private cache, SHA-256 checked and passed to Android's system package installer. XAPK bundles are checked on the server for a signed `base.apk` and matching signer certificate across all APK splits; on device, the outer SHA-256 is checked, split APKs are extracted with path/size limits, and one Android PackageInstaller session is created. XAPK bundles containing OBB expansion files are refused for now. Android's install permission and confirmation screens remain under the user's control.


## Backend CORS

The embedded Android storefront uses the stable loopback origin `http://127.0.0.1:8765`. Add it to the Apphoryn backend's `APPHORYN_CORS_ORIGINS` setting so live catalog requests work. When serving the Electron client from the same backend, also allow `apphoryn://store`.
