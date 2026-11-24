# Build adapter support in 0.1.0

This table describes adapter detection, not a promise that every project using the language will build.

| Target | Adapter | Expected outputs | Conditions |
|---|---|---|---|
| Android | Gradle wrapper | APK / AAB | Project contains a working Gradle wrapper and Android Gradle configuration; Android SDK and Java 17 runner setup available. Unsigned release APKs are rejected; a signed debug APK may be used for test installation. AAB is a store bundle, not a direct-install APK. |
| Windows | .NET publish | EXE when produced by project | Project contains a `.csproj` and publishes a platform-specific Windows runtime |
| Linux | .NET publish | Recognized output packages only | Project contains a `.csproj`; raw extensionless native output is not silently packaged |
| macOS | .NET publish | Recognized output packages only | Project contains a `.csproj`; native signing/notarization is not provided |
| Windows/Linux/macOS | Go build | EXE / portable binary (.bin on Linux, .macosbin on macOS) | Project contains `go.mod`; produces a native command-line binary, not an installer |
| Windows/Linux/macOS | Electron dist | EXE / MSI / AppImage / DMG / PKG, if configured | `package.json` identifies Electron and the project has a working `dist` script and packaging config |

Unsupported build systems fail visibly. The current workflow does not claim to produce signed store packages, notarized macOS installers, universal binaries, Linux DEB/RPM packages or Windows MSI packages unless the source project's own build configuration produces them.
