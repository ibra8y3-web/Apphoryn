# Apphoryn desktop client

Electron packages the repository's existing `apps/web` storefront into one
desktop application for Windows (NSIS installer), Linux (AppImage and DEB) and
macOS (DMG). The custom `apphoryn://` secure protocol serves local assets with
fetch and service-worker support. Renderer Node access is disabled and external
navigation is limited to HTTPS links.

The CI packages are unsigned development builds unless the repository owner
configures native code-signing certificates. Windows SmartScreen and macOS
Gatekeeper may show warnings for unsigned packages; the pipeline does not
pretend those signatures or notarization exist.
