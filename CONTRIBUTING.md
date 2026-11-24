# Contributing to Apphoryn

1. Open an issue describing the problem or proposed feature before large changes.
2. Keep catalog data factual and attributable to source metadata.
3. Never add fake packages, example catalog apps, fake ratings or simulated download activity.
4. Add tests for domain rules, import behavior, API behavior and platform adapters.
5. Do not weaken the license allowlist or security checks without a documented review.
6. Do not add secrets, private keys, personal access tokens or real user data.
7. Run `python -m compileall -q app scripts tests`, `python -m unittest discover -s tests -v` and `node --check apps/web/app.js` before opening a pull request.

Platform-specific build adapters must be explicit, bounded, testable, and fail closed when unsupported. A source checkout is not an installable application.
