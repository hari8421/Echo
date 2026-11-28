'use strict';

const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron');
const path = require('node:path');
const backend = require('./backend');
const { readAttachments } = require('./attachments');
const { buildMenu } = require('./menu');

let mainWindow = null;
let backendProcess = null;
let backendPromise = null;
const logLines = [];

function log(entry) {
  const text = `[${new Date().toISOString()}] ${entry.level}: ${entry.text}`;
  logLines.push(text);
  if (logLines.length > 500) logLines.shift();
}

function ensureBackend() {
  if (!backendPromise) {
    backendPromise = backend
      .startBackend({ app, onLog: log })
      .then((started) => {
        backendProcess = started.child;
        return { baseUrl: started.baseUrl, port: started.port };
      })
      .catch((error) => {
        backendPromise = null;
        log({ level: 'error', text: error.message });
        throw error;
      });
  }
  return backendPromise;
}

function dispatch(action) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('echo:action', action);
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 1040,
    minHeight: 680,
    show: true,
    backgroundColor: '#0b1017',
    title: 'Echo',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'));
  Menu.setApplicationMenu(
    Menu.buildFromTemplate(buildMenu({ dispatch, openExternal: (url) => shell.openExternal(url) }))
  );
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function registerIpc() {
  ipcMain.handle('echo:info', () => ({
    platform: process.platform,
    version: app.getVersion(),
    home: backend.echoHome(),
  }));

  ipcMain.handle('echo:setup-info', () => backend.readSetupInfo());

  ipcMain.handle('echo:setup-save', (_event, values) => backend.saveSetup(values));

  ipcMain.handle('echo:backend-start', async () => {
    const started = await ensureBackend();
    return started;
  });

  ipcMain.handle('echo:api', async (_event, apiPath, body) => {
    const { baseUrl } = await ensureBackend();
    return backend.apiRequest(baseUrl, apiPath, body);
  });

  ipcMain.handle('echo:pick-files', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: 'Attach files to chat',
      properties: ['openFile', 'multiSelections'],
      filters: [
        {
          name: 'Text and code files',
          extensions: [
            'txt', 'md', 'markdown', 'text', 'json', 'jsonl', 'csv', 'tsv', 'yaml', 'yml',
            'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'html', 'css', 'xml', 'sql',
            'log', 'rst', 'ini', 'toml', 'cfg', 'env', 'sh',
          ],
        },
        { name: 'All files', extensions: ['*'] },
      ],
    });
    if (result.canceled) return [];
    return readAttachments(result.filePaths);
  });

  ipcMain.handle('echo:log', () => logLines.slice(-200).join('\n'));

  ipcMain.handle('echo:open-folder', (_event, folder) =>
    shell.openPath(folder || backend.echoHome())
  );

  ipcMain.handle('echo:open-external', (_event, url) => {
    if (!backend.isSafeExternalUrl(url)) throw new Error('Only http(s) links can be opened.');
    return shell.openExternal(url);
  });
}

app.whenReady().then(() => {
  registerIpc();
  createWindow();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  backend.stopBackend(backendProcess);
});
