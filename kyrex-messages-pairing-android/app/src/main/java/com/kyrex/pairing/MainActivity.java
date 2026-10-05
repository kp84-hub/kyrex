// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import pairbridge.Bridge;
import pairbridge.Pairbridge;
import pairbridge.Sink;

public final class MainActivity extends Activity {
    private static boolean webDirectorySet;
    private static final String LOGIN_URL = "https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config";
    private static final Set<String> COOKIE_NAMES = new HashSet<>(Arrays.asList(
        "SID", "HSID", "SSID", "OSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS", "__Secure-1PSIDCC", "__Secure-3PSIDCC"));
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile Bridge bridge;
    private volatile int generation;
    private volatile boolean destroyed;
    private SessionStore sessions, cloudSessions;
    private CloudLink cloudLink;
    private EditText cloudUrl, cloudCode;
    private TextView cloudStatus;
    private String cloudState = "No Kyrex account linked.";
    private boolean cloudBusy, cloudLoaded, foreground;
    private final android.os.Handler cloudHandler = new android.os.Handler(android.os.Looper.getMainLooper());
    private long lastSyncStart;
    private final Runnable autoSync = () -> { if (foreground && !destroyed && cloudLink != null) { if (cloudBusy) scheduleCloudSync(); else syncCloud(); } };
    private LinearLayout content, conversations;
    private TextView status, result, live;
    private EditText phrase, message;
    private Button sendButton;
    private TextView sendResult, selected;
    private String sendConversationId = "", sendConversationName = "";
    private boolean sendBusy;
    private WebView webView;
    private boolean harvesting;
    private String state = "Not connected.", needle = "", conversationId = "", cursor = "", searchedNeedle = "";
    private boolean hasOlder;
    private int liveCount;
    private final Set<String> seen = new HashSet<>(), matches = new HashSet<>();

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (!webDirectorySet) { WebView.setDataDirectorySuffix("kyrex_pairing_test"); webDirectorySet = true; }
        sessions = new SessionStore(this);
        cloudSessions = new SessionStore(this, "cloud_link", "kyrex_messages_cloud_v1");
        home();
        worker.execute(() -> {
            try { CloudLink savedLink = CloudLink.restore(cloudSessions.load()); runOnUiThread(() -> {
                if (destroyed) return; cloudLink = savedLink; cloudLoaded = true;
                setCloudState(savedLink == null ? "No Kyrex account linked." : "Account link restored: " + savedLink.origin + ". Tap Sync now.");
            }); } catch (Exception e) { runOnUiThread(() -> { if (!destroyed) { cloudLoaded = true; setCloudState("Account link could not be restored. Link again using a new code."); } }); }
        });
        reconnect();
    }
    private int dp(int value) { return (int) (value * getResources().getDisplayMetrics().density); }
    private LinearLayout column() { LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view; }
    private TextView text(LinearLayout parent, String value, int size) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(size); view.setPadding(0, dp(8), 0, dp(8)); parent.addView(view); return view;
    }
    private Button button(LinearLayout parent, String label, Runnable action) {
        Button view = new Button(this); view.setText(label); view.setAllCaps(false); parent.addView(view);
        view.setOnClickListener(v -> action.run()); return view;
    }
    private void root(LinearLayout layout) {
        layout.setPadding(dp(20), dp(12), dp(20), dp(20));
        ScrollView scroll = new ScrollView(this); scroll.setFillViewport(true); scroll.addView(layout);
        scroll.setOnApplyWindowInsetsListener((v, insets) -> { v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(), insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom()); return insets; });
        setContentView(scroll);
    }
    private void home() {
        destroyWebView();
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
        content = column(); root(content);
        text(content, "Kyrex Messages — Companion Test", 25);
        text(content, "Connect Google Messages, confirm the emoji, then check a known message. Your existing phone number and texting app stay in use.", 16);
        status = text(content, state, 18);
        button(content, "Connect Messages", () -> new AlertDialog.Builder(this)
            .setTitle("Connect Google Messages?")
            .setMessage("This test uses an unofficial Google Messages protocol. Google sign-in and pairing stay on this phone. The saved Google session is encrypted with Android Keystore. History checks stay local. Message text is uploaded to Kyrex only after you separately confirm Link and sync. Messages are sent only after you review the recipients and confirm Send. If Google blocks sign-in, stop and report that result.")
            .setNegativeButton("Cancel", null).setPositiveButton("Continue", (d, w) -> login()).show());
        button(content, "Reconnect", this::reconnect);
        text(content, "Link Kyrex account", 21);
        text(content, "In Kyrex Chat, open Connections > Messages > Connect. Copy its server address and pairing code here. Linking uploads message text to that Kyrex account.", 16);
        cloudUrl = new EditText(this); cloudUrl.setHint("HTTPS server address from Kyrex Chat");
        cloudUrl.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        cloudUrl.setText(cloudLink == null ? "https://chat.kyrex.dev" : cloudLink.origin); content.addView(cloudUrl);
        cloudCode = new EditText(this); cloudCode.setHint("Pairing code from Kyrex Chat"); cloudCode.setSaveEnabled(false);
        cloudCode.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); content.addView(cloudCode);
        button(content, "Link and sync", this::linkCloud);
        button(content, "Sync now", this::syncCloud);
        cloudStatus = text(content, cloudState, 16);
        text(content, "Sync shares up to 100 text messages (10 from each of 10 recent inbox conversations), including SMS/RCS and sent messages. Attachments and oversized messages are skipped. New events trigger sync while this app is open. Chat shows the last successful sync time. Google credentials stay on this phone.", 14);
        button(content, "Remove account link from this phone", () -> new AlertDialog.Builder(this)
            .setTitle("Remove phone account link?").setMessage("Stops uploads from this phone. To delete the cloud snapshot and revoke the credential, also tap Disconnect in Kyrex Chat > Connections > Messages.")
            .setNegativeButton("Cancel", null).setPositiveButton("Remove", (d,w) -> removeCloudLink()).show());
        text(content, "History check", 21);
        text(content, "Copy a distinctive part of a message you know is RCS in Google Messages. Paste it here exactly, then choose that conversation below.", 16);
        phrase = new EditText(this); phrase.setHint("Exact text from your known RCS message"); phrase.setText(needle);
        phrase.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); phrase.setSaveEnabled(false); content.addView(phrase);
        button(content, "Refresh conversations", this::list);
        text(content, "Send test", 21);
        selected = text(content, "Tap a conversation below to choose the recipient.", 16);
        message = new EditText(this); message.setHint("Message to send");
        message.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        message.setSaveEnabled(false); content.addView(message);
        sendButton = button(content, "Review message", this::reviewSend);
        sendButton.setEnabled(false);
        sendResult = text(content, "Nothing sent. Review shows every recipient before confirmation.", 16);
        conversations = column(); content.addView(conversations);
        result = text(content, "Choose a conversation to read its latest 50 messages.", 17);
        button(content, "Load older messages", () -> {
            if (!hasOlder) { result.setText("No older page is available yet. Select a conversation first."); return; }
            read(conversationId, cursor, false);
        });
        live = text(content, "New message events this run: " + liveCount, 16);
        text(content, "Only counts and whether your text matched are displayed. Conversation loading is bounded to 100 inbox threads. A missing match means only that it was not found in the pages checked. Keep this test open for incoming-message checks.", 14);
        button(content, "Forget pairing on this phone", () -> new AlertDialog.Builder(this).setTitle("Forget local pairing?")
            .setMessage("This removes the encrypted session and Google sign-in from this test. Also remove this device in Google Messages > Device pairing to revoke the remote pairing.")
            .setNegativeButton("Cancel", null).setPositiveButton("Forget", (d,w) -> forget()).show());
    }
    private void setState(String value) { state = value; if (status != null) status.setText(value); }
    private void ui(int token, Runnable action) { runOnUiThread(() -> { if (!destroyed && token == generation) action.run(); }); }
    private int abandon() {
        generation++; sendBusy = false; Bridge previous = bridge; bridge = null;
        sendConversationId = ""; sendConversationName = "";
        if (sendButton != null) sendButton.setEnabled(false);
        if (selected != null) selected.setText("Choose a conversation again after reconnecting.");
        if (previous != null) new Thread(previous::close, "close-pairing").start();
        return generation;
    }
    private Sink sink(int token) {
        return (kind,value) -> {
            if (destroyed || token != generation) return;
            if ("SAVE".equals(kind)) {
                try {
                    worker.execute(() -> { Bridge b = bridge; if (b != null && token == generation) try { sessions.save(b.exportSession()); } catch (Exception e) { ui(token, () -> setState("Could not update saved pairing. Reconnect may require sign-in.")); } });
                } catch (RejectedExecutionException ignored) { /* Activity closed while native callback was in flight. */ }
            } else ui(token, () -> {
                switch (kind) {
                    case "EMOJI": emoji(value); break;
                    case "NEW_MESSAGE": liveCount++; if (live != null) live.setText("New message events this run: " + liveCount); scheduleCloudSync(); break;
                    case "UNPAIRED": setState("Google unpaired this session. Tap Connect Messages again."); break;
                    case "OFFLINE": case "ERROR": setState(value); break;
                    case "RECOVERED": setState("Connection recovered. Refresh conversations to verify."); break;
                    default: break;
                }
            });
        };
    }
    @SuppressLint("SetJavaScriptEnabled")
    private void login() {
        needle = phrase.getText().toString();
        final int token = abandon(); harvesting = false;
        destroyWebView(); getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        LinearLayout layout = column(); root(layout);
        text(layout, "Sign into Google", 24);
        text(layout, "Use the account selected in Google Messages > Device pairing. Google may reject this embedded browser; if so, tap the button below and report it.", 16);
        button(layout, "Google blocked sign-in", () -> { abandon(); home(); setState("TEST RESULT: Google blocked embedded sign-in. Stop here; this setup does not work on this device."); });
        button(layout, "Cancel", () -> { abandon(); home(); setState("Sign-in cancelled."); });
        WebView browser = new WebView(this); webView = browser;
        layout.addView(browser, new LinearLayout.LayoutParams(-1, dp(600)));
        CookieManager manager = CookieManager.getInstance(); manager.setAcceptCookie(true); manager.setAcceptThirdPartyCookies(browser, true);
        browser.getSettings().setJavaScriptEnabled(true); browser.getSettings().setDomStorageEnabled(true);
        // Keep the actual WebView user agent. Never disguise or bypass Google's rejection.
        browser.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) { return !LoginRules.allowed(request.getUrl().toString()); }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) ui(token, () -> text(layout, "Google sign-in page could not load. Check internet access or cancel.", 16));
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (token != generation || harvesting || !LoginRules.complete(url)) return;
                try {
                    JSONObject jar = new JSONObject();
                    for (String origin : new String[]{"https://accounts.google.com", "https://messages.google.com", "https://www.google.com"}) {
                        String cookies = manager.getCookie(origin); if (cookies == null) continue;
                        for (String pair : cookies.split(";\\s*")) {
                            int equals = pair.indexOf('='); if (equals <= 0) continue;
                            String name = pair.substring(0,equals); if (COOKIE_NAMES.contains(name)) jar.put(name, pair.substring(equals+1));
                        }
                    }
                    if (!jar.has("SAPISID") || !jar.has("SID")) { text(layout, "Google sign-in returned an incomplete session. Cancel and report this result.", 16); return; }
                    harvesting = true; pair(token, jar.toString());
                } catch (Exception e) { text(layout, "Could not finish sign-in. Cancel and try again.", 16); }
            }
        });
        // A separate cookie jar, cleared before each fresh sign-in.
        manager.removeAllCookies(removed -> { manager.flush(); if (token == generation && webView == browser) browser.loadUrl(LOGIN_URL); });
    }
    private void emoji(String value) {
        destroyWebView(); getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
        LinearLayout layout = column(); root(layout);
        text(layout, "Confirm this emoji", 25); text(layout, value, 64);
        text(layout, "Switch to Google Messages and choose this emoji in the pairing prompt. Then return here. If Google asks whether the pairing is yours, confirm only this test you just started.", 18);
        button(layout, "Open Google Messages", () -> {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.google.android.apps.messaging");
            if (launch != null) startActivity(launch); else text(layout, "Open Google Messages manually from your app list.", 16);
        });
        button(layout, "Cancel pairing", () -> { abandon(); home(); setState("Pairing cancelled."); });
    }
    private void pair(int token, String cookies) {
        worker.execute(() -> {
            try {
                Bridge b = Pairbridge.newBridge("", sink(token)); if (token != generation) { b.close(); return; } bridge = b;
                b.pair(cookies);
                if (token != generation) { b.close(); return; }
                sessions.save(b.exportSession());
                ui(token, () -> { CookieManager.getInstance().removeAllCookies(null); home(); setState("Paired. Checking the phone connection…"); list(); });
            } catch (Exception e) { ui(token, () -> { home(); setState(e.getMessage() == null ? "Pairing failed." : e.getMessage()); }); }
        });
    }
    private void reconnect() {
        final int token = abandon(); setState("Checking saved pairing…");
        worker.execute(() -> {
            try {
                String saved = sessions.load();
                if (saved.isEmpty()) { ui(token, () -> setState("Not paired yet. Tap Connect Messages.")); return; }
                Bridge b = Pairbridge.newBridge(saved, sink(token)); if (token != generation) {b.close();return;} bridge = b;
                b.connect();
                ui(token, () -> { setState("Pairing restored. Checking the phone connection…"); list(); });
            } catch (Exception e) { ui(token, () -> setState("Reconnect failed. Tap Connect Messages to sign in again.")); }
        });
    }
    private void list() {
        final int token = generation; final Bridge b = bridge;
        if (b == null) { setState("Connect Messages first."); return; }
        setState("Loading conversations…");
        worker.execute(() -> {
            try {
                String raw = b.list(); if (token != generation) return;
                sessions.save(b.exportSession()); JSONArray rows = new JSONObject(raw).getJSONArray("conversations");
                ui(token, () -> {
                    conversations.removeAllViews(); setState("Connected. Loaded " + rows.length() + " conversations (up to 100)."); scheduleCloudSync();
                    for (int i=0;i<rows.length();i++) {
                        JSONObject row = rows.optJSONObject(i); if (row == null) continue;
                        String id = row.optString("id"), name = row.optString("name", "Conversation " + (i+1));
                        if (name.isEmpty()) name = "Conversation " + (i+1);
                        final String label = name;
                        button(conversations, name + " · " + row.optString("kind"), () -> {
                            sendConversationId = id; sendConversationName = label;
                            selected.setText("Selected conversation: " + label + " · " + row.optString("kind"));
                            sendButton.setEnabled(!sendBusy);
                            read(id, "", true);
                        });
                    }
                });
            } catch (Exception e) { ui(token, () -> setState(e.getMessage() == null ? "Conversation loading failed." : e.getMessage())); }
        });
    }
    private void read(String id, String next, boolean reset) {
        final int token = generation; final Bridge b = bridge;
        if (b == null) { setState("Connect Messages first."); return; }
        final String query = phrase.getText().toString(); needle = query;
        if (!reset && !query.equals(searchedNeedle)) { result.setText("Search text changed. Select the conversation again to restart the check."); return; }
        if (reset) {conversationId=id;cursor="";hasOlder=false;seen.clear();matches.clear();searchedNeedle=query;}
        result.setText("Reading this conversation…");
        worker.execute(() -> {
            try {
                JSONObject page = new JSONObject(b.read(id,next,query));
                ui(token, () -> {
                    if (!id.equals(conversationId) || !query.equals(searchedNeedle)) return;
                    JSONArray ids=page.optJSONArray("ids"), found=page.optJSONArray("matches");
                    if (ids!=null) for(int i=0;i<ids.length();i++) seen.add(ids.optString(i));
                    if (found!=null) for(int i=0;i<found.length();i++) matches.add(found.optString(i));
                    cursor=page.optString("cursor");hasOlder=page.optBoolean("hasOlder");
                    String verdict=query.isEmpty()?"Exact-text check not tested.":matches.isEmpty()?"Exact text not found in pages checked.":"Exact text FOUND in " + matches.size() + " messages.";
                    result.setText("Read " + seen.size() + " unique messages in this conversation. " + verdict + (hasOlder?" Tap Load older messages to continue.":" No further cursor returned; this does not prove full archive coverage."));
                });
            } catch(Exception e){ui(token,()->result.setText(e.getMessage()==null?"History loading failed.":e.getMessage()));}
        });
    }
    private void reviewSend() {
        final int token = generation; final Bridge b = bridge;
        final String id = sendConversationId, body = message.getText().toString();
        if (sendBusy) return;
        if (b == null || id.isEmpty()) { sendResult.setText("Connect and select a conversation first."); return; }
        if (body.trim().isEmpty()) { sendResult.setText("Enter a message first."); return; }
        sendBusy = true; sendButton.setEnabled(false); sendResult.setText("Checking recipients… Nothing sent.");
        worker.execute(() -> {
            try {
                JSONObject draft = new JSONObject(b.prepareSend(id, body));
                ui(token, () -> {
                    JSONArray recipients = draft.optJSONArray("recipients");
                    StringBuilder preview = new StringBuilder("Conversation: ").append(draft.optString("name"))
                        .append("\nType: ").append(draft.optString("kind")).append("\n\nTo:");
                    if (recipients != null) for (int i = 0; i < recipients.length(); i++) preview.append("\n").append(recipients.optString(i));
                    preview.append("\n\nMessage:\n").append(draft.optString("text"));
                    new AlertDialog.Builder(this).setTitle("Send this message?").setMessage(preview.toString())
                        .setNegativeButton("Cancel", (d, w) -> finishSend("Cancelled. Nothing sent."))
                        .setOnCancelListener(d -> finishSend("Cancelled. Nothing sent."))
                        .setPositiveButton("Send", (d, w) -> {
                            if (token != generation || b != bridge) { finishSend("Connection changed. Review again. Nothing sent."); return; }
                            sendResult.setText("Sending once…");
                            worker.execute(() -> {
                                try { String outcome = b.send(draft.optString("token")); ui(token, () -> finishSend(outcome)); }
                                catch (Exception e) { ui(token, () -> finishSend(e.getMessage() == null ? "Send outcome unknown. Check Google Messages before retrying." : e.getMessage())); }
                            });
                        }).show();
                });
            } catch (Exception e) { ui(token, () -> finishSend(e.getMessage() == null ? "Could not prepare message. Nothing sent." : e.getMessage())); }
        });
    }
    private void finishSend(String outcome) {
        sendBusy = false; sendButton.setEnabled(bridge != null && !sendConversationId.isEmpty()); sendResult.setText(outcome);
    }
    private void setCloudState(String value) { cloudState = value; if (cloudStatus != null) cloudStatus.setText(value); }
    private void linkCloud() {
        if (cloudBusy) return;
        if (!cloudLoaded) { setCloudState("Checking saved account link. Try again shortly."); return; }
        if (bridge == null) { setCloudState("Connect Google Messages before linking your account."); return; }
        final String origin, code = cloudCode.getText().toString().trim();
        try { origin = CloudLink.origin(cloudUrl.getText().toString()); }
        catch (Exception e) { setCloudState("Enter the HTTPS server address shown in Kyrex Chat, without a path."); return; }
        new AlertDialog.Builder(this).setTitle("Share message text with Kyrex?")
            .setMessage("Server: " + origin + "\n\nThis uploads up to 100 recent text messages from 10 conversations to the Kyrex account that generated this code. Incoming and outgoing message text, sender, conversation name, and time are included. The encrypted cloud snapshot can be read in that account's Chat. New events will sync while this app is open. Google sign-in credentials stay on this phone. Use Disconnect in Chat to revoke access and delete the snapshot.")
            .setNegativeButton("Cancel", null).setPositiveButton("Link and sync", (d,w) -> {
                if (cloudBusy || destroyed) return;
                cloudBusy = true; setCloudState("Linking account…");
                worker.execute(() -> {
                    try {
                        CloudLink linked = CloudLink.pair(origin, code);
                        cloudSessions.save(linked.saved());
                        runOnUiThread(() -> { if (!destroyed) { cloudLink = linked; cloudBusy = false; cloudCode.setText(""); setCloudState("Account linked. Starting first sync…"); syncCloud(); } });
                    } catch (Exception e) { runOnUiThread(() -> { if (!destroyed) { cloudBusy = false; setCloudState(e instanceof IllegalStateException || e instanceof IllegalArgumentException ? e.getMessage() : "Could not save account link. Get a new code in Chat and link again."); } }); }
                });
            }).show();
    }
    private void scheduleCloudSync() {
        if (!foreground || cloudLink == null || destroyed) return;
        cloudHandler.removeCallbacks(autoSync);
        long delay = Math.max(5000, 30000 - (android.os.SystemClock.elapsedRealtime() - lastSyncStart));
        cloudHandler.postDelayed(autoSync, delay);
    }
    private void syncCloud() {
        if (cloudBusy) return;
        final CloudLink linked = cloudLink; final Bridge b = bridge; final int token = generation;
        if (linked == null) { setCloudState("Link your Kyrex account first."); return; }
        if (b == null) { setCloudState("Reconnect Google Messages before syncing."); return; }
        cloudBusy = true; lastSyncStart = android.os.SystemClock.elapsedRealtime(); setCloudState("Reading phone snapshot and syncing…");
        worker.execute(() -> {
            try {
                String snapshot = b.snapshot();
                if (token != generation || destroyed) throw new IllegalStateException("Phone connection changed. Sync again after reconnecting.");
                int count = linked.sync(snapshot);
                runOnUiThread(() -> { if (!destroyed) { cloudBusy = false; setCloudState("Synced " + count + " text messages to " + linked.origin + " at " + java.text.DateFormat.getTimeInstance().format(new java.util.Date()) + ". Ask Kyrex Chat: Show my texts."); } });
            } catch (Exception e) { runOnUiThread(() -> { if (!destroyed) { cloudBusy = false; setCloudState(e instanceof IllegalStateException || e instanceof IllegalArgumentException ? e.getMessage() : "Sync failed. Reconnect Google Messages and try Sync now. Previous cloud snapshot kept."); } }); }
        });
    }
    private void removeCloudLink() {
        if (cloudBusy) { setCloudState("Wait for the current sync/link to finish, then remove the link."); return; }
        cloudLink = null; cloudHandler.removeCallbacks(autoSync); cloudBusy = true;
        worker.execute(() -> { try { cloudSessions.clear(); runOnUiThread(() -> { if (!destroyed) { cloudBusy = false; setCloudState("Phone account link removed. Disconnect Messages in Chat to delete cloud data."); } }); }
            catch (Exception e) { runOnUiThread(() -> { if (!destroyed) { cloudBusy = false; setCloudState("Could not remove saved account link. Clear this app's storage in Android settings."); } }); } });
    }
    @Override public void onStart() { super.onStart(); foreground = true; scheduleCloudSync(); }
    @Override public void onStop() { foreground = false; cloudHandler.removeCallbacks(autoSync); super.onStop(); }
    private void forget() {
        abandon(); destroyWebView(); seen.clear(); matches.clear(); conversationId="";cursor="";hasOlder=false;
        CookieManager.getInstance().removeAllCookies(null);
        worker.execute(() -> { try {sessions.clear();runOnUiThread(() -> {if(!destroyed){home();setState("Local pairing forgotten. Remove this device in Google Messages > Device pairing too.");}});}catch(Exception e){runOnUiThread(()->{if(!destroyed)setState("Could not remove saved pairing. Clear this app's storage in Android settings.");});} });
    }
    private void destroyWebView(){if(webView!=null){webView.stopLoading();webView.destroy();webView=null;}}
    @Override public void onDestroy(){destroyed=true;cloudHandler.removeCallbacks(autoSync);abandon();destroyWebView();worker.shutdownNow();super.onDestroy();}
}
