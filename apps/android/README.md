# Apphoryn Android client

This is the native Android storefront shell. It packages the same `apps/web`
HTML/CSS/JavaScript/catalog assets into the APK and serves them from a loopback
HTTP origin so relative catalogue requests and the service worker work without
unsafe file-origin access. Downloads are handed to Android DownloadManager.

The client supports an optional hosted Apphoryn API endpoint through the
storefront Settings page. With no API endpoint it uses the packaged static
catalogue and does not invent offline search, import or build results.

CI produces an installable **debug-signed development APK** for verification.
A distributable production APK must be signed using a stable private keystore;
the ephemeral debug signing key must not be treated as a production signing
identity or used for Play Store publication.
