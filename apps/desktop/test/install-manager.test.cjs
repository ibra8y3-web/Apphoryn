const test = require("node:test");
const assert = require("node:assert/strict");
const {
  normalizedPlatform,
  validateInstallRequest,
  validateApphorynReleaseUrl,
  isApprovedReleaseRedirect
} = require("../install-manager.cjs");

const request = (overrides = {}) => ({
  artifactId: "a4c2f7aa-10ac-4ef8-9f1f-df8b4c3e5b8a",
  downloadUrl: "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/Example-Setup.exe",
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

test("accepts only HTTPS packages owned by the Apphoryn release repository", () => {
  assert.doesNotThrow(() => validateApphorynReleaseUrl(request().downloadUrl));
  assert.throws(() => validateApphorynReleaseUrl("http://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.exe"), /Apphoryn/);
  assert.throws(() => validateApphorynReleaseUrl("https://github.com/vendor/app/releases/download/v1/app.exe"), /Apphoryn/);
  assert.throws(() => validateApphorynReleaseUrl("https://user:pass@github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.exe"), /Apphoryn/);
  assert.throws(() => validateApphorynReleaseUrl("https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.exe?token=x"), /Apphoryn/);
});

test("accepts only HTTPS GitHub release asset redirect hosts", () => {
  assert.equal(isApprovedReleaseRedirect(new URL("https://release-assets.githubusercontent.com/path?signature=ok")), true);
  assert.equal(isApprovedReleaseRedirect(new URL("https://objects.githubusercontent.com/path")), true);
  assert.equal(isApprovedReleaseRedirect(new URL("https://evil.example/path")), false);
  assert.equal(isApprovedReleaseRedirect(new URL("http://release-assets.githubusercontent.com/path")), false);
});

test("rejects invalid artifact identifiers and path traversal filenames", () => {
  assert.throws(() => validateInstallRequest(request({ artifactId: "../secret" }), "win32"), /identifier/);
  assert.throws(() => validateInstallRequest(request({ filename: "../evil.exe" }), "win32"), /filename/);
});

test("checks the package platform and allowed installer extension", () => {
  assert.throws(() => validateInstallRequest(request({ platform: "linux", filename: "package.deb" }), "win32"), /targets linux/);
  assert.throws(() => validateInstallRequest(request({ filename: "payload.zip" }), "win32"), /cannot install/);
  assert.doesNotThrow(() => validateInstallRequest(request(), "win32"));
  assert.doesNotThrow(() => validateInstallRequest(request({
    platform: "linux", filename: "package.deb",
    downloadUrl: "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/package.deb"
  }), "linux"));
});
