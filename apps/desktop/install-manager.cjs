const crypto = require("node:crypto");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const MAX_INSTALL_BYTES = 1024 * 1024 * 1024;
const ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/i;

function normalizedPlatform(platform) {
  if (platform === "win32") return "windows";
  if (platform === "darwin") return "macos";
  return platform;
}

function extensionOf(filename) {
  return path.extname(String(filename || "")).toLowerCase();
}

function validateInstallRequest(payload, actualPlatform = process.platform) {
  if (!payload || typeof payload !== "object") throw new TypeError("Invalid install request.");
  const base = new URL(String(payload.apiBase || ""));
  if (base.protocol !== "https:" || !base.hostname || base.username || base.password || base.search || base.hash) {
    throw new TypeError("The Apphoryn API must be a valid HTTPS address.");
  }
  if (base.pathname && base.pathname !== "/" && base.pathname.includes("..")) {
    throw new TypeError("Invalid Apphoryn API path.");
  }
  const artifactId = String(payload.artifactId || "");
  if (!ID_PATTERN.test(artifactId)) throw new TypeError("Invalid Apphoryn artifact identifier.");
  const rawFilename = String(payload.filename || "");
  const filename = path.basename(rawFilename);
  if (!filename || filename !== rawFilename || filename.includes("/") || filename.includes(String.fromCharCode(92)) || /[\r\n]/.test(filename)) {
    throw new TypeError("Invalid installer filename.");
  }
  const expectedSha256 = String(payload.sha256 || "");
  if (!SHA256_PATTERN.test(expectedSha256)) throw new TypeError("A valid catalogue SHA-256 digest is required.");

  const platform = normalizedPlatform(actualPlatform);
  const declaredPlatform = String(payload.platform || "").toLowerCase();
  if (declaredPlatform !== platform) {
    throw new TypeError(`This package targets ${declaredPlatform}, but this Apphoryn client runs on ${platform}.`);
  }
  const extension = extensionOf(filename);
  const allowed = {
    windows: new Set([".exe", ".msi"]),
    linux: new Set([".deb", ".rpm", ".appimage"]),
    macos: new Set([".pkg"])
  };
  if (!allowed[platform] || !allowed[platform].has(extension)) {
    throw new TypeError(`The ${platform} client cannot install this package format yet.`);
  }
  return { apiBase: base, artifactId, filename, expectedSha256, platform, extension };
}

function buildDownloadUrl(apiBase, artifactId) {
  const base = new URL(apiBase);
  const prefix = base.pathname.replace(new RegExp("/+$"), "");
  const endpoint = new URL(`${prefix}/api/v1/downloads/${encodeURIComponent(artifactId)}`, base.origin);
  if (endpoint.origin !== base.origin || endpoint.protocol !== "https:") {
    throw new TypeError("Package URL is outside the configured Apphoryn server.");
  }
  return endpoint;
}

async function downloadVerifiedArtifact(request, tempDirectory = os.tmpdir()) {
  const url = buildDownloadUrl(request.apiBase.toString(), request.artifactId);
  await fsp.mkdir(tempDirectory, { recursive: true });
  const destination = path.join(tempDirectory, `apphoryn-${crypto.randomUUID()}-${request.extension.slice(1)}`);

  await new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { Accept: "application/octet-stream", "User-Agent": "Apphoryn-Desktop/0.4" } }, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`Apphoryn download API returned HTTP ${response.statusCode || "unknown"}.`));
        return;
      }
      const declared = Number(response.headers["content-length"] || 0);
      if (declared > MAX_INSTALL_BYTES) {
        response.destroy();
        reject(new Error("Package exceeds the desktop client size limit."));
        return;
      }
      const output = fs.createWriteStream(destination, { flags: "wx", mode: 0o600 });
      const digest = crypto.createHash("sha256");
      let total = 0;
      response.on("data", (chunk) => {
        total += chunk.length;
        if (total > MAX_INSTALL_BYTES) {
          response.destroy(new Error("Package exceeded the desktop client size limit."));
          return;
        }
        digest.update(chunk);
      });
      response.on("error", (error) => output.destroy(error));
      output.on("error", reject);
      output.on("finish", () => {
        const actual = digest.digest("hex");
        if (actual.toLowerCase() !== request.expectedSha256.toLowerCase()) {
          fsp.rm(destination, { force: true }).finally(() => reject(new Error("Downloaded package SHA-256 does not match the Apphoryn catalogue.")));
          return;
        }
        resolve();
      });
      response.pipe(output);
    });
    req.setTimeout(60000, () => req.destroy(new Error("Timed out while downloading the Apphoryn package.")));
    req.on("error", reject);
  }).catch(async (error) => {
    await fsp.rm(destination, { force: true }).catch(() => {});
    throw error;
  });

  return destination;
}

function spawnDetached(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { detached: true, stdio: "ignore", windowsHide: false });
    child.once("error", reject);
    child.once("spawn", () => {
      child.unref();
      resolve({ started: true });
    });
  });
}

async function startNativeInstaller(filePath, extension, platform = process.platform) {
  const target = normalizedPlatform(platform);
  if (target === "windows") {
    if (extension === ".msi") return spawnDetached("msiexec.exe", ["/i", filePath]);
    if (extension === ".exe") return spawnDetached(filePath, []);
  }
  if (target === "linux") {
    if (extension === ".deb") return spawnDetached("pkexec", ["dpkg", "-i", filePath]);
    if (extension === ".rpm") return spawnDetached("pkexec", ["rpm", "-Uvh", filePath]);
    if (extension === ".appimage") {
      await fsp.chmod(filePath, 0o700);
      return spawnDetached(filePath, []);
    }
  }
  if (target === "macos" && extension === ".pkg") {
    const quote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";
    const command = `/usr/sbin/installer -pkg ${quote(filePath)} -target /`;
    const script = `do shell script ${JSON.stringify(command)} with administrator privileges`;
    return spawnDetached("osascript", ["-e", script]);
  }
  throw new Error(`No native installer adapter is available for ${target} ${extension} packages.`);
}

async function installArtifact(payload, options = {}) {
  const platform = options.platform || process.platform;
  const request = validateInstallRequest(payload, platform);
  const tempDirectory = options.tempDirectory || path.join(os.tmpdir(), "Apphoryn", "Installers");
  const installerPath = await downloadVerifiedArtifact(request, tempDirectory);
  const result = await startNativeInstaller(installerPath, request.extension, platform);
  return { ...result, filename: request.filename, message: "The operating system installer was started. Follow its confirmation prompts." };
}

module.exports = {
  MAX_INSTALL_BYTES,
  normalizedPlatform,
  validateInstallRequest,
  buildDownloadUrl,
  downloadVerifiedArtifact,
  startNativeInstaller,
  installArtifact
};
