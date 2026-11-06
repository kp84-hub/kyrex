import React from 'react';

export default function VoiceIcon({ name = 'mic', ...props }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
    {name === 'mic' ? <><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" /></>
      : name === 'up-left' ? <path d="M18 18 6 6m0 12V6h12" />
        : name === 'up-right' ? <path d="M6 18 18 6M6 6h12v12" />
      : name === 'close' ? <path d="m6 6 12 12M18 6 6 18" />
        : name === 'stop' ? <rect x="6" y="6" width="12" height="12" rx="3" />
          : name === 'check' ? <path d="m5 12 4 4L19 6" />
            : name === 'wake' ? <><path d="M8 8a6 6 0 0 1 8 0M5 5a10 10 0 0 1 14 0M8 16a6 6 0 0 0 8 0M5 19a10 10 0 0 0 14 0" /><circle cx="12" cy="12" r="1" /></>
              : <><path d="M8 5v14M16 5v14" /></>}
  </svg>;
}
