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

const APPHORYN_RELEASE_PREFIX = "/ibra8y3-web/Apphoryn/releases/download/";
const APPROVED_RELEASE_HOSTS = new Set([
  "github.com",
  "objects.githubusercontent.com",
  "release-assets.githubusercontent.com",
  "github-releases.githubusercontent.com"
]);

function validateApphorynReleaseUrl(value) {
  const url = new URL(String(value || ""));
  if (
    url.protocol !== "https:" ||
    url.hostname.toLowerCase() !== "github.com" ||
    (url.port && url.port !== "443") ||
    url.username || url.password || url.search || url.hash ||
    !url.pathname.startsWith(APPHORYN_RELEASE_PREFIX)
  ) {
    throw new TypeError("Only HTTPS packages stored in Apphoryn Releases can be installed.");
  }
  return url;
}

function isApprovedReleaseRedirect(url) {
  return url.protocol === "https:" &&
    (!url.port || url.port === "443") &&
    APPROVED_RELEASE_HOSTS.has(url.hostname.toLowerCase()) &&
    !url.username && !url.password && !url.hash;
}

function validateInstallRequest(payload, actualPlatform = process.platform) {
  if (!payload || typeof payload !== "object") throw new TypeError("Invalid install request.");
  const artifactId = String(payload.artifactId || "");
  if (!ID_PATTERN.test(artifactId)) throw new TypeError("Invalid Apphoryn artifact identifier.");
  const rawFilename = String(payload.filename || "");
  const filename = path.basename(rawFilename);
  if (!filename || filename !== rawFilename || filename.includes("/") || filename.includes(String.fromCharCode(92)) || /[\r\n]/.test(filename)) {
    throw new TypeError("Invalid installer filename.");
  }
  const expectedSha256 = String(payload.sha256 || "");
  if (!SHA256_PATTERN.test(expectedSha256)) throw new TypeError("A valid catalogue SHA-256 digest is required.");
  const downloadUrl = validateApphorynReleaseUrl(payload.downloadUrl);

  const platform = normalizedPlatform(actualPlatform);
  const declaredPlatform = String(payload.platform || "").toLowerCase();
  if (declaredPlatform !== platform) {
    throw new TypeError(`This package targets ${declaredPlatform}, but this Apphoryn client runs on ${platform}.`);
  }
  const extension = extensionOf(filename);
  const allowed = {
    windows: new Set([".exe", ".msi", ".msix", ".appx"]),
    linux: new Set([".deb", ".rpm", ".appimage"]),
    macos: new Set([".pkg", ".dmg"])
  };
  if (!allowed[platform] || !allowed[platform].has(extension)) {
    throw new TypeError(`The ${platform} client cannot install this package format yet.`);
  }
  return { artifactId, filename, expectedSha256, platform, extension, downloadUrl };
}

function getApprovedReleaseResponse(url, redirects = 0) {
  if (redirects > 5 || !isApprovedReleaseRedirect(url)) {
    return Promise.reject(new Error("Apphoryn package redirected to an unapproved host or exceeded its redirect limit."));
  }
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      headers: { Accept: "application/octet-stream", "User-Agent": "Apphoryn-Desktop/1.0" }
    }, (response) => {
      const status = Number(response.statusCode || 0);
      if (status >= 300 && status < 400) {
        const location = response.headers.location;
        response.resume();
        if (!location || redirects >= 5) {
          reject(new Error("Apphoryn package exceeded its redirect limit."));
          return;
        }
        let next;
        try { next = new URL(location, url); }
        catch { reject(new Error("Apphoryn package returned an invalid redirect.")); return; }
        if (!isApprovedReleaseRedirect(next)) {
          reject(new Error("Apphoryn package redirected to an unapproved host."));
          return;
        }
        getApprovedReleaseResponse(next, redirects + 1).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        response.resume();
        reject(new Error(`Apphoryn release returned HTTP ${status || "unknown"}.`));
        return;
      }
      resolve(response);
    });
    req.setTimeout(60000, () => req.destroy(new Error("Timed out while downloading the Apphoryn package.")));
    req.on("error", reject);
  });
}

async function downloadVerifiedArtifact(request, tempDirectory = os.tmpdir()) {
  await fsp.mkdir(tempDirectory, { recursive: true });
  const destination = path.join(tempDirectory, `apphoryn-${crypto.randomUUID()}-${request.extension.slice(1)}`);

  const response = await getApprovedReleaseResponse(request.downloadUrl);
  await new Promise((resolve, reject) => {
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

function runCommand(command, args, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let stdout = "";
    let stderr = "";
    const collect = (current, chunk) => (current + chunk.toString("utf8")).slice(-12000);
    child.stdout.on("data", (chunk) => { stdout = collect(stdout, chunk); });
    child.stderr.on("data", (chunk) => { stderr = collect(stderr, chunk); });
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`${command} timed out while processing the installer.`));
    }, timeoutMs);
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${command} failed with exit code ${code}: ${(stderr || stdout).trim().slice(-1200)}`));
    });
  });
}

async function findAppBundles(root, depth = 4) {
  if (depth < 0) return [];
  const results = [];
  const entries = await fsp.readdir(root, { withFileTypes: true });
  for (const entry of entries) {
    const candidate = path.join(root, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory() && entry.name.toLowerCase().endsWith(".app")) {
      results.push(candidate);
      continue;
    }
    if (entry.isDirectory() && depth > 0 && !entry.name.startsWith(".")) {
      results.push(...await findAppBundles(candidate, depth - 1));
    }
  }
  return results;
}

async function installWindowsAppx(filePath) {
  // PowerShell single-quoted string escaping is applied to a generated private-cache path.
  const literalPath = String(filePath).replace(/'/g, "''");
  const command = `Add-AppxPackage -LiteralPath '${literalPath}' -ErrorAction Stop`;
  const result = await runCommand("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", command], 300000);
  return { started: true, message: "Windows App Installer completed the MSIX/APPX package operation.", output: result.stdout.trim().slice(-1200) };
}

async function installMacDmg(filePath) {
  const tempRoot = await fsp.mkdtemp(path.join(os.tmpdir(), "apphoryn-dmg-"));
  const mountPoint = path.join(tempRoot, "mount");
  await fsp.mkdir(mountPoint);
  let mounted = false;
  try {
    await runCommand("hdiutil", ["attach", "-nobrowse", "-readonly", "-mountpoint", mountPoint, filePath], 180000);
    mounted = true;
    const bundles = await findAppBundles(mountPoint);
    if (bundles.length !== 1) {
      throw new Error(bundles.length
        ? "The DMG contains multiple app bundles; automatic selection was refused."
        : "The DMG contains no application bundle that Apphoryn can install.");
    }
    const sourceApp = bundles[0];
    const destinationApp = path.join("/Applications", path.basename(sourceApp));
    const quote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";
    const shellCommand = `/usr/bin/ditto ${quote(sourceApp)} ${quote(destinationApp)}`;
    const appleScript = `do shell script ${JSON.stringify(shellCommand)} with administrator privileges`;
    await runCommand("osascript", ["-e", appleScript], 600000);
    return { started: true, message: `Installed ${path.basename(sourceApp)} into /Applications.` };
  } finally {
    if (mounted) {
      await runCommand("hdiutil", ["detach", "-force", mountPoint], 60000).catch(() => {});
    }
    await fsp.rm(tempRoot, { recursive: true, force: true }).catch(() => {});
  }
}

async function startNativeInstaller(filePath, extension, platform = process.platform) {
  const target = normalizedPlatform(platform);
  if (target === "windows") {
    if (extension === ".msi") return spawnDetached("msiexec.exe", ["/i", filePath]);
    if (extension === ".exe") return spawnDetached(filePath, []);
    if (extension === ".msix" || extension === ".appx") return installWindowsAppx(filePath);
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
  if (target === "macos" && extension === ".dmg") return installMacDmg(filePath);
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
  validateApphorynReleaseUrl,
  isApprovedReleaseRedirect,
  getApprovedReleaseResponse,
  downloadVerifiedArtifact,
  startNativeInstaller,
  installArtifact
};
