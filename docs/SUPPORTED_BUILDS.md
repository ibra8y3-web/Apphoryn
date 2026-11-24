# Build adapters and package verification

This is a list of supported project patterns, not a promise that every project using a language will build. A build is only published when a recognized output exists and the artifact verifier accepts it.

| Target | Adapter | Published result | Conditions |
|---|---|---|---|
| Android | Gradle wrapper | Signed APK | Project has a working Gradle wrapper, Android Gradle configuration and required SDK platform. \`assembleRelease\` must produce an APK that passes \`apksigner verify\`. Debug APKs and AAB-only outputs are not published as directly installable apps. |
| Windows | .NET publish | PE executable (\`.exe\`) | A \`.csproj\` project publishes a Windows self-contained apphost. This does not create an MSI installer. |
| Linux | .NET publish | ELF portable binary (\`.bin\`) | A \`.csproj\` publishes a Linux self-contained apphost. This is a portable executable, not DEB/RPM. |
| macOS | .NET publish | Mach-O portable binary (\`.macosbin\`) | A \`.csproj\` publishes an osx-x64 apphost. Code signing/notarization is not performed. |
| Windows/Linux/macOS | Go build | Native executable | A \`go.mod\` project compiles with the runner's toolchain. The adapter currently targets amd64. |
| Windows/Linux/macOS | Electron \`dist\` script | Project-produced installer/package | \`package.json\` must identify Electron and provide a working \`dist\` script plus host-platform packaging configuration. |

## Package import validation

| Package | Format checks | Signature state |
|---|---|---|
| APK | ZIP integrity, Android manifest, DEX/native executable code | Mandatory \`apksigner verify\`; signer-certificate SHA-256 recorded when available |
| EXE | DOS header and PE header pointer/signature | Authenticode not yet verified |
| MSI | OLE compound-file signature | Authenticode/Windows Installer trust not yet verified |
| MSIX/APPX | ZIP integrity, manifest, block map, signature block presence | Cryptographic publisher trust not yet verified |
| AppImage / portable Linux binary | ELF header; AppImage runtime marker for AppImage | Publisher signature not yet verified |
| DEB | ar container and control/data archive members | Package signature not independently verified |
| RPM | RPM lead/header magic and minimum header size | RPM signature not independently verified |
| DMG | UDIF \`koly\` trailer | Developer ID/notarization not yet verified |
| PKG | XAR header and length bounds | Installer signature/notarization not yet verified |
| JAR | ZIP integrity, manifest with a valid `Main-Class` entry and compiled class file; plain Java libraries are rejected | JAR signer trust not yet verified |

Format verification is not malware scanning, does not test application behaviour, and is not a guarantee that a package will install on every device. Unsupported or invalid outputs are rejected rather than relabelled as installable software.
