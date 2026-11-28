'use strict';

/** Native menus: the renderer handles the actual navigation/actions. */
function buildMenu({ dispatch } = {}) {
  const send = (action) => () => dispatch?.(action);
  const isMac = process.platform === 'darwin';

  const tabs = [
    ['chat', 'Chat', 'CmdOrCtrl+1'],
    ['docs', 'Documents', 'CmdOrCtrl+2'],
    ['ask', 'Ask docs', 'CmdOrCtrl+3'],
    ['browser', 'Browser', 'CmdOrCtrl+4'],
    ['memory', 'Memory', 'CmdOrCtrl+5'],
    ['outbox', 'Outbox', 'CmdOrCtrl+6'],
    ['settings', 'Settings', 'CmdOrCtrl+,'],
  ];

  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'New chat', accelerator: 'CmdOrCtrl+N', click: send('new-chat') },
        { label: 'New document', accelerator: 'CmdOrCtrl+Shift+N', click: send('new-doc') },
        { type: 'separator' },
        { label: 'Open workspace folder', accelerator: 'CmdOrCtrl+Shift+O', click: send('open-workspace') },
        { label: 'Refresh', accelerator: 'CmdOrCtrl+R', click: send('refresh') },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        ...tabs.map(([tab, label, accelerator]) => ({
          label,
          accelerator,
          click: send(`tab:${tab}`),
        })),
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Window',
      submenu: isMac
        ? [{ role: 'about' }, { type: 'separator' }, { role: 'minimize' }, { role: 'zoom' }, { role: 'front' }]
        : [{ role: 'minimize' }, { role: 'zoom' }, { role: 'close' }],
    },
  ];

  return template;
}

module.exports = { buildMenu };
