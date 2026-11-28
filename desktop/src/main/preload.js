'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/** A narrow, typed-by-convention bridge. The renderer never sees Node APIs. */
contextBridge.exposeInMainWorld('echo', Object.freeze({
  platform: process.platform,

  info: () => ipcRenderer.invoke('echo:info'),
  setupInfo: () => ipcRenderer.invoke('echo:setup-info'),
  saveSetup: (values) => ipcRenderer.invoke('echo:setup-save', values),
  startBackend: () => ipcRenderer.invoke('echo:backend-start'),
  api: (apiPath, body) => ipcRenderer.invoke('echo:api', apiPath, body),
  pickFiles: () => ipcRenderer.invoke('echo:pick-files'),
  log: () => ipcRenderer.invoke('echo:log'),
  openFolder: (folder) => ipcRenderer.invoke('echo:open-folder', folder),
  openExternal: (url) => ipcRenderer.invoke('echo:open-external', url),

  onAction: (callback) => {
    const listener = (_event, action) => callback(action);
    ipcRenderer.on('echo:action', listener);
    return () => ipcRenderer.removeListener('echo:action', listener);
  },
}));
