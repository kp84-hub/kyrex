import React from 'react';
import { androidNativeIntent } from '../lib/androidHealth.js';

// Setup delegates to the one Android app. Opening it is never proof of pairing,
// sync or send consent; the adjacent live status remains authoritative.
export default function MessagesSetup({ onAndroid, savedTexts, sendEnabled }) {
  return <section aria-label="Messages setup guide">
    {savedTexts ? <p>Manage your phone connection and sync in Kyrex for Android.</p> : <>
      <h3>Connect your phone once</h3>
      <ol>
        <li><strong>Pair Google Messages.</strong> Approve the matching emoji on your phone.</li>
        <li><strong>Link your Kyrex account.</strong> Review which account receives your recent texts.</li>
        <li><strong>Choose your options.</strong> Background connection and confirmed sending are separate choices.</li>
        <li><strong>Verify sync.</strong> Kyrex checks text sync and the live connection without sending a test message.</li>
      </ol>
    </>}
    {onAndroid ? <>
      <a className="connection-btn" href={androidNativeIntent('messages')}>
        {savedTexts ? 'Manage Messages on this phone' : 'Start phone setup'}
      </a>
      <p>Opens Messages setup in the Kyrex Android app. If nothing opens, install or update Kyrex for Android, then return here.</p>
    </> : <p>Open Kyrex on your Android phone, then choose Connections → Messages → Connect. Once linked, synced texts are available here too.</p>}
    <p>{sendEnabled ? 'Confirmed sending from Chat is enabled on your phone.'
      : 'Chat sending is off. You can enable it in phone setup; every message still needs your review and confirmation.'}</p>
    <p>Sync shares up to 100 SMS/RCS texts from 10 recent conversations. Google session keys stay on your phone. Keep background connection enabled for sync after leaving setup; Android may limit it to save battery.</p>
  </section>;
}
