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

The Android client currently installs verified `.apk` packages only. It downloads into its private cache, checks the SHA-256 digest supplied by the catalogue, and opens Android's system package installer with a FileProvider URI. Android's install permission and confirmation screens remain under the user's control. XAPK/split-APK bundles are not yet enabled.


## Backend CORS

The embedded Android storefront uses the stable loopback origin `http://127.0.0.1:8765`. Add it to the Apphoryn backend's `APPHORYN_CORS_ORIGINS` setting so live catalog requests work. When serving the Electron client from the same backend, also allow `apphoryn://store`.
