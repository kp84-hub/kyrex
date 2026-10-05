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
    private SessionStore sessions;
    private LinearLayout content, conversations;
    private TextView status, result, live;
    private EditText phrase;
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
        home();
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
        text(content, "Kyrex Messages — Pairing Test", 25);
        text(content, "Connect Google Messages, confirm the emoji, then check a known message. Your existing phone number and texting app stay in use.", 16);
        status = text(content, state, 18);
        button(content, "Connect Messages", () -> new AlertDialog.Builder(this)
            .setTitle("Connect Google Messages?")
            .setMessage("This test uses an unofficial Google Messages protocol. Google sign-in and pairing stay on this phone. The saved Google session is encrypted with Android Keystore. Message text is used only for your local check; nothing is uploaded to Kyrex. No messages are sent. If Google blocks sign-in, stop and report that result.")
            .setNegativeButton("Cancel", null).setPositiveButton("Continue", (d, w) -> login()).show());
        button(content, "Reconnect", this::reconnect);
        text(content, "History check", 21);
        text(content, "Copy a distinctive part of a message you know is RCS in Google Messages. Paste it here exactly, then choose that conversation below.", 16);
        phrase = new EditText(this); phrase.setHint("Exact text from your known RCS message"); phrase.setText(needle);
        phrase.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); phrase.setSaveEnabled(false); content.addView(phrase);
        button(content, "Refresh conversations", this::list);
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
        generation++; Bridge previous = bridge; bridge = null;
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
                    case "NEW_MESSAGE": liveCount++; if (live != null) live.setText("New message events this run: " + liveCount); break;
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
                    conversations.removeAllViews(); setState("Connected. Loaded " + rows.length() + " conversations (up to 100).");
                    for (int i=0;i<rows.length();i++) {
                        JSONObject row = rows.optJSONObject(i); if (row == null) continue;
                        String id = row.optString("id"), name = row.optString("name", "Conversation " + (i+1));
                        if (name.isEmpty()) name = "Conversation " + (i+1);
                        button(conversations, name + " · " + row.optString("kind"), () -> read(id, "", true));
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
    private void forget() {
        abandon(); destroyWebView(); seen.clear(); matches.clear(); conversationId="";cursor="";hasOlder=false;
        CookieManager.getInstance().removeAllCookies(null);
        worker.execute(() -> { try {sessions.clear();runOnUiThread(() -> {if(!destroyed){home();setState("Local pairing forgotten. Remove this device in Google Messages > Device pairing too.");}});}catch(Exception e){runOnUiThread(()->{if(!destroyed)setState("Could not remove saved pairing. Clear this app's storage in Android settings.");});} });
    }
    private void destroyWebView(){if(webView!=null){webView.stopLoading();webView.destroy();webView=null;}}
    @Override public void onDestroy(){destroyed=true;abandon();destroyWebView();worker.shutdownNow();super.onDestroy();}
}
