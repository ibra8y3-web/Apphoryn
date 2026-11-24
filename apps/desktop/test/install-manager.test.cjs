const test = require("node:test");
const assert = require("node:assert/strict");
const {
  normalizedPlatform,
  validateInstallRequest,
  buildDownloadUrl
} = require("../install-manager.cjs");

const request = (overrides = {}) => ({
  apiBase: "https://store.example.org",
  artifactId: "a4c2f7aa-10ac-4ef8-9f1f-df8b4c3e5b8a",
  filename: "Example-Setup.exe",
  platform: "windows",
  packageType: "exe",
  sha256: "a".repeat(64),
  ...overrides
});

test("maps native host platforms to catalogue platform names", () => {
  assert.equal(normalizedPlatform("win32"), "windows");
  assert.equal(normalizedPlatform("darwin"), "macos");
  assert.equal(normalizedPlatform("linux"), "linux");
});

test("accepts only HTTPS Apphoryn API roots and safe artifact IDs", () => {
  assert.throws(() => validateInstallRequest(request({ apiBase: "http://example.org" }), "win32"), /HTTPS/);
  assert.throws(() => validateInstallRequest(request({ artifactId: "../secret" }), "win32"), /identifier/);
  assert.throws(() => validateInstallRequest(request({ filename: "../evil.exe" }), "win32"), /filename/);
});

test("checks the package platform and allowed installer extension", () => {
  assert.throws(() => validateInstallRequest(request({ platform: "linux", filename: "package.deb" }), "win32"), /targets linux/);
  assert.throws(() => validateInstallRequest(request({ filename: "payload.zip" }), "win32"), /cannot install/);
  assert.doesNotThrow(() => validateInstallRequest(request(), "win32"));
  assert.doesNotThrow(() => validateInstallRequest(request({ platform: "linux", filename: "package.deb" }), "linux"));
});

test("builds an API-owned download URL beneath the configured base path", () => {
  const result = buildDownloadUrl("https://store.example.org/prefix", "artifact-id");
  assert.equal(result.toString(), "https://store.example.org/prefix/api/v1/downloads/artifact-id");
});
