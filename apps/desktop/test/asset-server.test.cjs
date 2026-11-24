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
  assert.match(contentType("index.html"), /^text\/html/);
  assert.match(contentType("app.js"), /^text\/javascript/);
  assert.match(contentType("catalog/index.json"), /^application\/json/);
});
