const path = require("node:path");

function resolveAssetPath(rootDirectory, requestPath) {
  let decoded;
  try { decoded = decodeURIComponent(String(requestPath || "/")); }
  catch { return null; }
  decoded = decoded.replace(/\\\\/g, "/").replace(/^\\/+/, "");
  if (!decoded || decoded === ".") decoded = "index.html";
  if (decoded.split("/").some((part) => part === ".." || part === ".")) return null;
  const root = path.resolve(rootDirectory);
  const candidate = path.resolve(root, decoded);
  const relative = path.relative(root, candidate);
  if (relative.startsWith("..") || path.isAbsolute(relative)) return null;
  return candidate;
}

function contentType(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  const types = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon"
  };
  return types[extension] || "application/octet-stream";
}

module.exports = { resolveAssetPath, contentType };
