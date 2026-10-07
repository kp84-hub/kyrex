// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
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

/** Pairing/settings UI only. Closing this screen never closes the background protocol session. */
public final class MainActivity extends Activity implements CompanionRuntime.Listener {
    private static boolean webDirectorySet;
    private static final String LOGIN_URL = "https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config";
    private static final Set<String> COOKIE_NAMES = new HashSet<>(Arrays.asList(
        "SID", "HSID", "SSID", "OSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS", "__Secure-1PSIDCC", "__Secure-3PSIDCC"));
    private MessagesService service;
    private CompanionRuntime runtime;
    private boolean bound, visible, destroyed, harvesting, pairingScreen;
    private LinearLayout content, conversations;
    private TextView status, cloudStatus, remoteStatus, presenceStatus, backgroundStatus, result, live, sendResult, selected;
    private EditText cloudUrl, cloudCode, phrase, message;
    private Button sendButton;
    private WebView webView;
    private AlertDialog sendDialog;
    private JSONArray displayedConversations;
    private String sendConversationId = "", conversationId = "", cursor = "", needle = "", searchedNeedle = "";
    private boolean hasOlder, sendBusy;
    private final Set<String> seen = new HashSet<>(), matches = new HashSet<>();
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((MessagesService.LocalBinder) binder).service(); runtime = service.runtime;
            home(); if (visible) service.attach(MainActivity.this);
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service = null; runtime = null;
            if (!destroyed) { LinearLayout layout = column(); root(layout); text(layout, "Messages service stopped. Reopen the companion.", 18); }
        }
    };
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (!webDirectorySet) { WebView.setDataDirectorySuffix("kyrex_pairing_test"); webDirectorySet = true; }
        LinearLayout layout = column(); root(layout); text(layout, "Starting Kyrex Messages…", 20);
        bound = bindService(new Intent(this, MessagesService.class), connection, Context.BIND_AUTO_CREATE);
    }
    @Override public void onStart() {
        super.onStart(); visible = true;
        if (MessagesService.enabled(this)) startBackground();
        if (service != null) service.attach(this);
    }
    @Override public void onStop() {
        visible = false;
        if (sendDialog != null) { sendDialog.dismiss(); sendDialog = null; }
        if (runtime != null) runtime.cancelLocalPreview();
        sendBusy = false;
        if (service != null) service.detach();
        super.onStop();
    }
    @Override public void onDestroy() {
        destroyed = true; destroyWebView();
        if (service != null) service.detach();
        if (bound) unbindService(connection);
        super.onDestroy();
    }
    @Override public void changed() {
        if (destroyed || runtime == null) return;
        if (pairingScreen && runtime.ready()) { CookieManager.getInstance().removeAllCookies(null); home(); }
        if (status != null) status.setText(runtime.state);
        if (cloudStatus != null) cloudStatus.setText(runtime.cloudState);
        if (remoteStatus != null) remoteStatus.setText(runtime.remoteState);
        if (presenceStatus != null) presenceStatus.setText(runtime.heartbeatState);
        if (backgroundStatus != null) backgroundStatus.setText(!service.backgroundError.isEmpty() ? service.backgroundError :
            service.background ? "Background connection ON. You can return to Kyrex Chat; check-ins and sync continue." : "Background connection OFF. Tap Keep Messages connected before returning to Kyrex Chat; otherwise check-ins stop when you leave.");
        if (live != null) live.setText("New message events this run: " + runtime.liveCount);
        if (displayedConversations != runtime.conversations) renderConversations();
    }
    @Override public void event(String kind, String value) {
        if (destroyed) return;
        if ("EMOJI".equals(kind)) emoji(value);
        else if ("PAIRED".equals(kind) || "PAIR_FAILED".equals(kind)) { CookieManager.getInstance().removeAllCookies(null); home(); }
        else if ("LINKED".equals(kind)) {
            if (cloudCode != null) cloudCode.setText("");
            if (visible && runtime.ready() && !service.background) enableBackground();
        }
        else if ("REPLACED".equals(kind)) {
            sendConversationId = ""; displayedConversations = null;
            if (sendDialog != null) { sendDialog.dismiss(); sendDialog = null; }
            sendBusy = false;
            if (selected != null) selected.setText("Choose a conversation again after reconnecting.");
            if (sendButton != null) sendButton.setEnabled(false);
        }
    }
    private void enableBackground() {
        if (!runtime.linked() || !runtime.ready()) { backgroundStatus.setText("Connect Google Messages and link your Kyrex account first."); return; }
        new AlertDialog.Builder(this).setTitle("Keep Messages connected?")
            .setMessage("Kyrex will keep the Messages connection, cloud check-ins and text syncing active after you leave this screen. Android shows a connection notification with a Stop control. This uses battery and network data. Sending stays off unless you separately enable it, and every send still needs your confirmation in Chat.")
            .setNegativeButton("Cancel", null).setPositiveButton("Enable", (d,w) -> {
                if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 8);
                else startBackground();
            }).show();
    }
    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        // Android allows the service when notifications are denied; its Task Manager still offers Stop.
        if (requestCode == 8 && visible) startBackground();
    }
    private void startBackground() {
        try { startForegroundService(new Intent(this, MessagesService.class).setAction(MessagesService.START)); }
        catch (RuntimeException e) {
            MessagesService.saveEnabled(this, false);
            if (backgroundStatus != null) backgroundStatus.setText("Android could not start the connection service. Try enabling it again while this app is open.");
        }
    }
    private int abandon() { service.stopBackground(); return runtime.abandon(); }
    private void reconnect() { runtime.reconnect(); }
    private void list() { runtime.list(); }
    private void syncCloud() { runtime.syncCloud(); }
    private void setRemoteSending(boolean enabled) { runtime.setRemoteSending(enabled); }
    private void removeCloudLink() { service.stopBackground(); runtime.removeCloudLink(); }
    private void setState(String value) { if (status != null) status.setText(value); }
    private void ui(int token, Runnable action) { if (!destroyed && runtime != null && token == runtime.generation()) action.run(); }
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
        pairingScreen = false; displayedConversations = null; destroyWebView();
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
        content = column(); root(content);
        text(content, "Kyrex Messages — v" + BuildConfig.VERSION_NAME, 25);
        text(content, "Connect Google Messages, confirm the emoji, then check a known message. Your existing phone number and texting app stay in use.", 16);
        status = text(content, runtime.state, 18);
        text(content, "Stay connected to Kyrex Chat", 21);
        backgroundStatus = text(content, "Checking connection service…", 16);
        presenceStatus = text(content, runtime.heartbeatState, 16);
        button(content, "Keep Messages connected", this::enableBackground);
        button(content, "Stop background connection", () -> service.stopBackground());
        text(content, "If check-ins stop with the screen locked, choose unrestricted battery use in Android Settings > Apps > Kyrex Messages > Battery.", 14);
        button(content, "Android battery settings", () -> {
            try { startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, android.net.Uri.parse("package:" + getPackageName()))); }
            catch (RuntimeException e) { backgroundStatus.setText("Open Android Settings > Apps > Kyrex Messages > Battery manually."); }
        });
        button(content, "Connect Messages", () -> new AlertDialog.Builder(this)
            .setTitle("Connect Google Messages?")
            .setMessage("This test uses an unofficial Google Messages protocol. Google sign-in and pairing stay on this phone. The saved Google session is encrypted with Android Keystore. History checks stay local. Message text is uploaded to Kyrex only after you separately confirm Link and sync. Messages are sent only after you review the recipients and confirm Send. If Google blocks sign-in, stop and report that result.")
            .setNegativeButton("Cancel", null).setPositiveButton("Continue", (d, w) -> login()).show());
        button(content, "Reconnect", this::reconnect);
        text(content, "Link Kyrex account", 21);
        text(content, "In Kyrex Chat, open Connections > Messages > Connect. Copy its server address and pairing code here. Linking uploads message text to that Kyrex account.", 16);
        cloudUrl = new EditText(this); cloudUrl.setHint("HTTPS server address from Kyrex Chat");
        cloudUrl.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        cloudUrl.setText(runtime.cloudLink == null ? "https://chat.kyrex.dev" : runtime.cloudLink.origin); content.addView(cloudUrl);
        cloudCode = new EditText(this); cloudCode.setHint("Pairing code from Kyrex Chat"); cloudCode.setSaveEnabled(false);
        cloudCode.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); content.addView(cloudCode);
        button(content, "Link and sync", this::linkCloud);
        button(content, "Sync now", this::syncCloud);
        cloudStatus = text(content, runtime.cloudState, 16);
        text(content, "Sync shares up to 100 text messages (10 from each of 10 recent inbox conversations), including SMS/RCS and sent messages. Attachments and oversized messages are skipped. New events sync while this app is open or background connection is enabled. Chat shows the last successful sync time. Google credentials stay on this phone.", 14);
        button(content, "Remove account link from this phone", () -> new AlertDialog.Builder(this)
            .setTitle("Remove phone account link?").setMessage("Stops uploads from this phone. To delete the cloud snapshot and revoke the credential, also tap Disconnect in Kyrex Chat > Connections > Messages.")
            .setNegativeButton("Cancel", null).setPositiveButton("Remove", (d,w) -> removeCloudLink()).show());
        text(content, "Send from Kyrex Chat", 21);
        text(content, "Enable background connection below to keep Messages available while you use Kyrex Chat. Chat shows the exact message and every verified recipient before you confirm Send.", 16);
        remoteStatus = text(content, runtime.remoteState, 16);
        button(content, "Allow sends confirmed in Kyrex Chat", () -> new AlertDialog.Builder(this)
            .setTitle("Allow confirmed Chat sends?")
            .setMessage("Your linked Kyrex account may prepare a message in an existing conversation. The phone verifies all recipients, and Chat displays the exact recipients and text. Only pressing Send in Chat submits the prepared draft. Each confirmation is single-use and expires. No automatic send retries. Enable background connection to handle confirmed Chat sends after leaving this screen.")
            .setNegativeButton("Cancel", null).setPositiveButton("Allow", (d,w) -> setRemoteSending(true)).show());
        button(content, "Turn off Chat sending", () -> setRemoteSending(false));
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
        live = text(content, "New message events this run: " + runtime.liveCount, 16);
        text(content, "Only counts and whether your text matched are displayed. Conversation loading is bounded to 100 inbox threads. A missing match means only that it was not found in the pages checked. Keep this test open for incoming-message checks.", 14);
        button(content, "Forget pairing on this phone", () -> new AlertDialog.Builder(this).setTitle("Forget local pairing?")
            .setMessage("This removes the encrypted session and Google sign-in from this test. Also remove this device in Google Messages > Device pairing to revoke the remote pairing.")
            .setNegativeButton("Cancel", null).setPositiveButton("Forget", (d,w) -> forget()).show());
        changed();
    }
    @SuppressLint("SetJavaScriptEnabled")
    private void login() {
        needle = phrase.getText().toString();
        final int token = abandon(); harvesting = false;
        pairingScreen = true; destroyWebView(); getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
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
                if (token != runtime.generation() || harvesting || !LoginRules.complete(url)) return;
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
                    harvesting = true; runtime.pair(token, jar.toString());
                } catch (Exception e) { text(layout, "Could not finish sign-in. Cancel and try again.", 16); }
            }
        });
        // A separate cookie jar, cleared before each fresh sign-in.
        manager.removeAllCookies(removed -> { manager.flush(); if (token == runtime.generation() && webView == browser) browser.loadUrl(LOGIN_URL); });
    }
    private void emoji(String value) {
        pairingScreen = true; destroyWebView(); getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
        LinearLayout layout = column(); root(layout);
        text(layout, "Confirm this emoji", 25); text(layout, value, 64);
        text(layout, "Switch to Google Messages and choose this emoji in the pairing prompt. Then return here. If Google asks whether the pairing is yours, confirm only this test you just started.", 18);
        button(layout, "Open Google Messages", () -> {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.google.android.apps.messaging");
            if (launch != null) startActivity(launch); else text(layout, "Open Google Messages manually from your app list.", 16);
        });
        button(layout, "Cancel pairing", () -> { abandon(); home(); setState("Pairing cancelled."); });
    }
    private void destroyWebView(){if(webView!=null){webView.stopLoading();webView.destroy();webView=null;}}
    private void renderConversations() {
        if (conversations == null || runtime == null) return;
        displayedConversations = runtime.conversations; conversations.removeAllViews();
        for (int i=0; i<displayedConversations.length(); i++) {
            JSONObject row = displayedConversations.optJSONObject(i); if (row == null) continue;
            String id = row.optString("id"), name = row.optString("name", "Conversation " + (i+1));
            if (name.isEmpty()) name = "Conversation " + (i+1);
            final String label = name;
            button(conversations, name + " · " + row.optString("kind"), () -> {
                sendConversationId = id; selected.setText("Selected conversation: " + label + " · " + row.optString("kind"));
                sendButton.setEnabled(!sendBusy); read(id, "", true);
            });
        }
    }
    private void linkCloud() {
        final String origin, code = cloudCode.getText().toString().trim();
        try { origin = CloudLink.origin(cloudUrl.getText().toString()); }
        catch (Exception e) { cloudStatus.setText("Enter the HTTPS server address shown in Chat, without a path."); return; }
        new AlertDialog.Builder(this).setTitle("Share message text with Kyrex?")
            .setMessage("Server: " + origin + "\n\nUploads up to 100 recent text messages from 10 conversations to the Kyrex account that generated this code. Incoming/outgoing text, sender, conversation name and time are included. New events sync while this screen is open or background connection is enabled. Google credentials stay on this phone. Disconnect in Chat revokes access and deletes its snapshot.")
            .setNegativeButton("Cancel", null).setPositiveButton("Link and sync", (d,w) -> runtime.linkCloud(origin, code)).show();
    }
    private void read(String id, String next, boolean reset) {
        final String query = phrase.getText().toString(); needle = query;
        if (!reset && !query.equals(searchedNeedle)) { result.setText("Search text changed. Select the conversation again."); return; }
        if (reset) { conversationId=id; cursor=""; hasOlder=false; seen.clear(); matches.clear(); searchedNeedle=query; }
        result.setText("Reading this conversation…");
        runtime.read(id, next, query, (raw,error) -> {
            if (!visible || destroyed || !id.equals(conversationId) || !query.equals(searchedNeedle)) return;
            if (error != null) { result.setText(error); return; }
            try {
                JSONObject page = new JSONObject(raw);
                JSONArray ids=page.optJSONArray("ids"), found=page.optJSONArray("matches");
                if (ids!=null) for(int i=0;i<ids.length();i++) seen.add(ids.optString(i));
                if (found!=null) for(int i=0;i<found.length();i++) matches.add(found.optString(i));
                cursor=page.optString("cursor"); hasOlder=page.optBoolean("hasOlder");
                String verdict=query.isEmpty()?"Exact-text check not tested.":matches.isEmpty()?"Exact text not found in pages checked.":"Exact text FOUND in " + matches.size() + " messages.";
                result.setText("Read " + seen.size() + " unique messages. " + verdict + (hasOlder?" Tap Load older messages to continue.":" No further cursor returned; this does not prove full archive coverage."));
            } catch (Exception e) { result.setText("Could not read the history result."); }
        });
    }
    private void reviewSend() {
        if (sendBusy) return;
        final String id=sendConversationId, body=message.getText().toString(); final int token=runtime.generation();
        if (id.isEmpty() || body.trim().isEmpty()) { sendResult.setText("Select a conversation and enter a message first."); return; }
        sendBusy=true; sendButton.setEnabled(false); sendResult.setText("Checking recipients… Nothing sent.");
        runtime.prepareLocal(id, body, (raw,error) -> {
            if (!visible || destroyed) { runtime.cancelLocalPreview(); return; }
            if (error!=null) { finishSend(error); return; }
            try {
                JSONObject draft=new JSONObject(raw); JSONArray recipients=draft.optJSONArray("recipients");
                StringBuilder preview=new StringBuilder("Conversation: ").append(draft.optString("name")).append("\nType: ").append(draft.optString("kind")).append("\n\nTo:");
                if (recipients!=null) for(int i=0;i<recipients.length();i++) preview.append("\n").append(recipients.optString(i));
                preview.append("\n\nMessage:\n").append(draft.optString("text"));
                sendDialog=new AlertDialog.Builder(this).setTitle("Send this message?").setMessage(preview.toString())
                    .setNegativeButton("Cancel", (d,w) -> { runtime.cancelLocalPreview(); finishSend("Cancelled. Nothing sent."); })
                    .setOnCancelListener(d -> { runtime.cancelLocalPreview(); finishSend("Cancelled. Nothing sent."); })
                    .setPositiveButton("Send", (d,w) -> {
                        sendDialog=null; sendResult.setText("Sending once…");
                        runtime.sendLocal(token, draft.optString("token"), (outcome,failure) -> {
                            if (visible && !destroyed) finishSend(failure==null?outcome:failure);
                        });
                    }).create();
                sendDialog.show();
            } catch (Exception e) { runtime.cancelLocalPreview(); finishSend("Could not prepare message. Nothing sent."); }
        });
    }
    private void finishSend(String outcome) { sendBusy=false; sendButton.setEnabled(!sendConversationId.isEmpty()); sendResult.setText(outcome); }
    private void forget() {
        service.stopBackground(); runtime.forget();
        seen.clear(); matches.clear(); conversationId=""; cursor=""; hasOlder=false;
        CookieManager.getInstance().removeAllCookies(null); home();
    }
}
