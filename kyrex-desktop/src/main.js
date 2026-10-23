import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { render } from './ui.js';
import './style.css';

const mode = getCurrentWindow().label === 'launcher' ? 'launcher' : 'settings';
render(document.querySelector('#app'), { mode, invoke, listen });
