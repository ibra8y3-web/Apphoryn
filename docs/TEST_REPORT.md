# Apphoryn 0.1.0 — local verification report

Run in the project workspace before packaging.

## Automated checks

| Check | Result |
|---|---|
| Python `compileall` (`app`, `scripts`, `tests`) | PASS |
| Python unit/API tests | PASS — 16 tests |
| JavaScript syntax (`node --check apps/web/app.js`) | PASS |
| CLI argument parser smoke checks (`ci_build.py`, `publish_artifacts.py`, `harvest_and_import.py`) | PASS |
| YAML syntax for all 3 GitHub Actions workflows | PASS |
| FastAPI `TestClient` health endpoint | PASS — HTTP 200 |
| FastAPI storefront route | PASS — HTTP 200 |
| FastAPI catalog endpoint | PASS — HTTP 200 |
| FastAPI discovery route (mocked GitHub service) | PASS — HTTP 200 |
| Artifact storage/hash unit test | PASS |
| Missing-license import block | PASS |

## Responsive layout measurements

Chromium layout checks confirmed that the document and body do not exceed the viewport width at these sizes:

| Viewport | Width | Document scroll width | Result |
|---|---:|---:|---|
| Desktop | 1365 px | 1365 px | PASS |
| Laptop | 1024 px | 1024 px | PASS |
| Tablet | 820 px | 820 px | PASS |
| Phone | 390 px | 390 px | PASS |
| Small phone | 320 px | 320 px | PASS |
| Phone RTL | 390 px | 390 px | PASS |

## Not executed in this local verification

- No external GitHub workflow was dispatched from this workspace.
- No third-party repository was cloned or built as part of the test suite.
- No release package was downloaded from GitHub or published to a deployed server.
- The optional external AI provider was not called because no model credentials are included.
- Production deployment, malware scanning, code signing, macOS notarization and native mobile/desktop clients are not certified by these tests.

The automated tests validate Apphoryn's local logic and API boundaries; they do not certify imported third-party software as safe.
