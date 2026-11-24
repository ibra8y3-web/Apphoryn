# Apphoryn 0.2.0-alpha — local verification report

This report records checks run against the local updated source tree during development.

## Automated checks

| Check | Result |
|---|---|
| Python \`compileall\` (\`app\`, \`scripts\`, \`tests\`) | PASS |
| Python unit/API tests | PASS — 31 tests |
| JavaScript syntax (\`node --check apps/web/app.js\`, \`apps/web/sw.js\`) | PASS |
| CLI entrypoints (\`ci_build\`, \`harvest_and_import\`, both publishers) | PASS — all \`--help\` commands start successfully |
| Static JSON manifests | PASS — seed list and catalogue are valid JSON |
| FastAPI TestClient health, storefront, catalogue and discovery endpoints | PASS |
| Artifact rejection tests | PASS — fake APK bytes are rejected even with a matching local SHA-256 |
| APK signature fail-closed test | PASS — APK is rejected if \`apksigner\` is unavailable |
| Release mirror manifest tests | PASS — generated catalogue URLs point to Apphoryn-owned releases |

Test fixtures construct minimal file headers to exercise validators. They are test fixtures only; they are not stored in the catalogue or advertised as downloadable applications.

## Not executed in this local verification

- No third-party binary package was downloaded into the local editing workspace.
- No external GitHub Actions build or Scout workflow was dispatched from this development run.
- No external project was cloned or compiled as part of the 31-test local suite.
- No real installer was launched on Android, Windows, Linux or macOS.
- No external AI provider was called because no credentials were supplied.
- Production malware scanning, Windows Authenticode verification, macOS notarization and native mobile/desktop clients are not certified by these tests.

The GitHub repository branch \`apphoryn/real-artifact-pipeline\` contains the updated source and automated workflows. Actual application binaries become part of the Apphoryn store only after the Scout workflow runs successfully with repository Actions write permissions. The workflow downloads official release files, validates them and mirrors acceptable packages into Apphoryn Releases; it does not pre-fill the catalogue with simulated binaries.
