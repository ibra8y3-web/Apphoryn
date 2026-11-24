const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { resolveAssetPath, contentType } = require("../asset-server.cjs");

test("resolves a normal app asset inside the packaged web root", () => {
  const root = path.resolve(__dirname, "../../web");
  assert.equal(resolveAssetPath(root, "/index.html"), path.join(root, "index.html"));
});

test("blocks traversal and malformed encoded asset paths", () => {
  const root = path.resolve(__dirname, "../../web");
  assert.equal(resolveAssetPath(root, "/../../etc/passwd"), null);
  assert.equal(resolveAssetPath(root, "/%2e%2e/%2e%2e/secret"), null);
  assert.equal(resolveAssetPath(root, "/%E0%A4%A"), null);
});

test("serves correct content types for the web app assets", () => {
  assert.equal(contentType("index.html").startsWith("text/html"), true);
  assert.equal(contentType("app.js").startsWith("text/javascript"), true);
  assert.equal(contentType("catalog/index.json").startsWith("application/json"), true);
});
