const { app, BrowserWindow, protocol, shell } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { resolveAssetPath, contentType } = require("./asset-server.cjs");

protocol.registerSchemesAsPrivileged([
  {
    scheme: "apphoryn",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
      allowServiceWorkers: true
    }
  }
]);

let mainWindow;

function getWebRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "web")
    : path.resolve(__dirname, "../web");
}

async function registerStoreProtocol() {
  const webRoot = getWebRoot();
  await protocol.handle("apphoryn", async (request) => {
    const url = new URL(request.url);
    const filePath = resolveAssetPath(webRoot, url.pathname);
    if (!filePath) {
      return new Response("Invalid path", { status: 400 });
    }
    try {
      const data = await fs.readFile(filePath);
      return new Response(data, {
        status: 200,
        headers: {
          "Content-Type": contentType(filePath),
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff"
        }
      });
    } catch (error) {
      if (error && error.code === "ENOENT") {
        return new Response("Not found", { status: 404 });
      }
      console.error("Apphoryn asset request failed:", error);
      return new Response("Asset server error", { status: 500 });
    }
  });
}

function openExternal(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:") void shell.openExternal(parsed.toString());
  } catch {
    // Invalid or non-HTTPS external navigation is intentionally ignored.
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: "#0b1018",
    title: "Apphoryn",
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false
    }
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith("apphoryn://store/")) {
      event.preventDefault();
      openExternal(url);
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  void mainWindow.loadURL("apphoryn://store/index.html");
}

app.whenReady().then(async () => {
  await registerStoreProtocol();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
