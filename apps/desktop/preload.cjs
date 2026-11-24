const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ApphorynInstaller", Object.freeze({
  installArtifact: (payload) => ipcRenderer.invoke("apphoryn:install-artifact", payload)
}));
