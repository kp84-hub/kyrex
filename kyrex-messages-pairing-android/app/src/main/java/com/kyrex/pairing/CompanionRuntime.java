// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import pairbridge.Bridge;
import pairbridge.Pairbridge;
import pairbridge.Sink;

/** Service-owned protocol session. No Activity, View, or UI lifecycle owns this work. */
class CompanionRuntime {
    interface Listener { void changed(); void event(String kind, String value); }
    interface Result { void complete(String value, String error); }
    private final Listener listener;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ExecutorService presenceWorker = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Handler cloudHandler = main;
    private final SessionStore sessions, cloudSessions;
    private final ConnectionRecovery recovery = new ConnectionRecovery();
    private volatile Bridge bridge;
    private volatile int generation;
    private volatile boolean destroyed;
    volatile CloudLink cloudLink;
    private final Object bridgeLock = new Object();
    boolean cloudLoaded, needsUserAttention;
    private volatile boolean foreground, background;
    private boolean cloudBusy, sendBusy, localPreview, presenceBusy, cloudRejected;
    private long lastSyncStart;
    String state = "Not connected.", cloudState = "Checking saved account link…", remoteState = "Chat sending is off.";
    String heartbeatState = "No cloud check-in yet.";
    private final CloudPresence presence = new CloudPresence();
    JSONArray conversations = new JSONArray();
    int liveCount;
    private final Runnable connectionCheck = this::checkConnection;
    private final Runnable pollCommands = this::pollCloudCommands;
    private final Runnable heartbeatTask = this::heartbeat;
    private final Runnable autoSync = () -> {
        if (canHandleCommands() && cloudLink != null) {
            if (cloudBusy) scheduleCloudSync(); else syncCloud();
        }
    };

    CompanionRuntime(Context context, Listener listener) { this(context, listener, true); }
    // The false form lets service lifecycle tests use a transport-free session.
    CompanionRuntime(Context context, Listener listener, boolean restore) {
        this.listener = listener;
        sessions = new SessionStore(context);
        cloudSessions = new SessionStore(context, "cloud_link", "kyrex_messages_cloud_v1");
        if (!restore) return;
        worker.execute(() -> {
            try {
                CloudLink saved = CloudLink.restore(cloudSessions.load());
                main.post(() -> {
                    if (destroyed) return;
                    cloudLink = saved; cloudLoaded = true;
                    setCloudState(saved == null ? "No Kyrex account linked." : "Account link restored: " + saved.origin);
                    setRemoteState(saved != null && saved.allowSend ? "Confirmed sending from Chat is enabled." : "Chat sending is off.");
                    scheduleCommands(1000); scheduleHeartbeat(0); scheduleCloudSync();
                });
            } catch (Exception e) { main.post(() -> {
                if (destroyed) return;
                cloudLoaded = true; needsUserAttention = true;
                setCloudState("Account link could not be restored. Link again using a new code.");
            }); }
        });
        reconnect();
    }
    void setActive(boolean visible, boolean backgroundEnabled) {
        boolean wasActive = canHandleCommands();
        foreground = visible; background = backgroundEnabled;
        if (canHandleCommands()) {
            if (!wasActive) recovery.disconnected();
            scheduleConnectionCheck(0); scheduleCloudSync(); scheduleCommands(1000); scheduleHeartbeat(0);
        } else {
            main.removeCallbacks(connectionCheck); main.removeCallbacks(autoSync);
            main.removeCallbacks(pollCommands); main.removeCallbacks(heartbeatTask);
        }
    }
    boolean linked() { return cloudLoaded && cloudLink != null && !cloudRejected; }
    boolean ready() { return recovery.ready(); }
    int generation() { return generation; }
    String notificationState() {
        if (!linked()) return "Link your Kyrex account in the companion.";
        if (needsUserAttention) return "Messages needs attention. Open the companion.";
        if (!recovery.ready()) return "Reconnecting Messages…";
        return presence.current(android.os.SystemClock.elapsedRealtime()) ? "Messages connected" : "Checking the Kyrex connection…";
    }
    private boolean canHandleCommands() { return !destroyed && (foreground || background); }
    private void changed() { if (!destroyed) listener.changed(); }
    private void setState(String value) { state = value; changed(); scheduleHeartbeat(0); }
    private void setCloudState(String value) { cloudState = value; changed(); }
    private void setRemoteState(String value) { remoteState = value; changed(); }
    private void ui(int token, Runnable action) { main.post(() -> { if (!destroyed && token == generation) action.run(); }); }
    int abandon() {
        recovery.stop(); main.removeCallbacks(connectionCheck);
        return replaceBridge();
    }
    private int replaceBridge() {
        final Bridge previous;
        synchronized (bridgeLock) { generation++; previous = bridge; bridge = null; }
        sendBusy = false; localPreview = false; conversations = new JSONArray();
        if (previous != null) new Thread(previous::close, "close-pairing").start();
        listener.event("REPLACED", "");
        return generation;
    }
    private boolean installBridge(int token, Bridge next) {
        synchronized (bridgeLock) {
            if (destroyed || token != generation) { next.close(); return false; }
            bridge = next; return true;
        }
    }
    private Sink sink(int token) {
        return (kind, value) -> {
            if (destroyed || token != generation) return;
            if ("SAVE".equals(kind)) {
                try { worker.execute(() -> {
                    Bridge b = bridge;
                    if (b != null && token == generation && !destroyed) try { sessions.save(b.exportSession()); }
                    catch (Exception e) { ui(token, () -> setState("Could not update saved pairing. Reconnect may require sign-in.")); }
                }); } catch (RejectedExecutionException ignored) { /* Service was stopped. */ }
            } else ui(token, () -> {
                switch (kind) {
                    case "EMOJI": listener.event(kind, value); break;
                    case "NEW_MESSAGE": liveCount++; changed(); scheduleCloudSync(); break;
                    case "UNPAIRED":
                        recovery.stop(); needsUserAttention = true; main.removeCallbacks(connectionCheck);
                        setState("Google unpaired or expired this session. Tap Connect Messages again."); break;
                    case "OFFLINE": case "ERROR":
                        recovery.disconnected(); setState("Messages connection unavailable. Checking saved pairing…");
                        scheduleConnectionCheck(5000); break;
                    case "RECOVERED": recovery.disconnected(); scheduleConnectionCheck(0); break;
                    default: break;
                }
            });
        };
    }
    void pair(int token, String cookies) {
        needsUserAttention = false;
        worker.execute(() -> {
            try {
                Bridge b = Pairbridge.newBridge("", sink(token));
                if (!installBridge(token, b)) return;
                b.pair(cookies);
                if (destroyed || token != generation) { b.close(); return; }
                sessions.save(b.exportSession());
                ui(token, () -> {
                    recovery.manualReconnect(); recovery.beginRestore(true, false);
                    listener.event("PAIRED", ""); setState("Paired. Checking the phone connection…"); loadConversations();
                });
            } catch (Exception e) { ui(token, () -> {
                needsUserAttention = true; setState("Pairing failed. Connect Messages again."); listener.event("PAIR_FAILED", "");
            }); }
        });
    }
    void list() {
        if (cloudBusy || sendBusy || recovery.running()) { setState("Wait for the current operation before refreshing."); return; }
        loadConversations();
    }
    private void loadConversations() {
        final int token = generation; final Bridge b = bridge;
        if (b == null) { setState("Connect Messages first."); return; }
        setState("Loading conversations…");
        worker.execute(() -> {
            try {
                JSONArray rows = new JSONObject(b.list()).getJSONArray("conversations");
                if (destroyed || token != generation) return;
                sessions.save(b.exportSession());
                ui(token, () -> {
                    if (!recovery.enabled()) return;
                    recovery.verified(); needsUserAttention = false; conversations = rows;
                    setState("Connected. Loaded " + rows.length() + " conversations (up to 100).");
                    scheduleCloudSync(); scheduleCommands(0);
                });
            } catch (Exception e) { ui(token, () -> {
                if (!recovery.enabled()) return;
                recovery.restoreFailed(); recovery.disconnected();
                setState("Conversation loading failed. Checking saved pairing…"); scheduleConnectionCheck(recovery.retryDelay());
            }); }
        });
    }
    void linkCloud(String origin, String code) {
        if (cloudBusy || !cloudLoaded || bridge == null) { setCloudState("Wait for Google Messages and the saved account check before linking."); return; }
        cloudBusy = true; setCloudState("Linking account…");
        worker.execute(() -> {
            try {
                CloudLink linked = CloudLink.pair(origin, code); cloudSessions.save(linked.saved());
                main.post(() -> {
                    if (destroyed) return;
                    cloudLink = linked; cloudRejected = false; presence.reset(); needsUserAttention = false; cloudBusy = false;
                    setRemoteState("Chat sending is off. Enable it below to send from Chat.");
                    listener.event("LINKED", ""); scheduleCommands(1000); scheduleHeartbeat(0);
                    setCloudState("Account linked. Starting first sync…"); syncCloud();
                });
            } catch (Exception e) { main.post(() -> { if (!destroyed) {
                cloudBusy = false; setCloudState(safeCloudError(e, "Could not save account link. Get a new code in Chat and link again."));
            } }); }
        });
    }
    private String safeCloudError(Exception e, String fallback) {
        return e instanceof IllegalStateException || e instanceof IllegalArgumentException ? e.getMessage() : fallback;
    }
    private void cloudFailure(CloudLink linked, Exception error) {
        if (cloudLink == null || !cloudLink.token.equals(linked.token) || destroyed) return;
        if (error instanceof CloudLink.RevokedLinkException) {
            cloudRejected = true; needsUserAttention = true; presence.reset();
            main.removeCallbacks(heartbeatTask); main.removeCallbacks(autoSync); main.removeCallbacks(pollCommands);
            setCloudState("Kyrex account link revoked or expired. Get a new code in Chat and link again.");
        }
    }
    void forget() {
        abandon(); needsUserAttention = true;
        worker.execute(() -> {
            try { sessions.clear(); main.post(() -> { if (!destroyed) setState("Local pairing forgotten. Remove this device in Google Messages > Device pairing too."); }); }
            catch (Exception e) { main.post(() -> { if (!destroyed) setState("Could not remove saved pairing. Clear this app's storage in Android settings."); }); }
        });
    }
    void read(String id, String cursor, String query, Result callback) {
        final Bridge b = bridge; final int token = generation;
        if (b == null) { callback.complete(null, "Connect Messages first."); return; }
        worker.execute(() -> {
            try { String page = b.read(id, cursor, query); ui(token, () -> callback.complete(page, null)); }
            catch (Exception e) { ui(token, () -> callback.complete(null, "History loading failed. Check the Messages connection.")); }
        });
    }
    void prepareLocal(String id, String text, Result callback) {
        if (cloudBusy || sendBusy || !recovery.ready() || bridge == null) { callback.complete(null, "Wait for the Messages connection or current operation."); return; }
        sendBusy = true; final Bridge b = bridge; final int token = generation;
        worker.execute(() -> {
            try {
                String draft = b.prepareSend(id, text);
                ui(token, () -> { localPreview = true; callback.complete(draft, null); });
            } catch (Exception e) { ui(token, () -> { sendBusy = false; callback.complete(null, "Could not prepare message. Nothing sent."); }); }
        });
    }
    void cancelLocalPreview() { if (localPreview) { localPreview = false; sendBusy = false; } }
    void sendLocal(int token, String draftToken, Result callback) {
        if (token != generation || !localPreview || bridge == null) { callback.complete(null, "Connection changed. Review again. Nothing sent."); return; }
        localPreview = false; final Bridge b = bridge;
        worker.execute(() -> {
            String result = null, error = null;
            try { result = b.send(draftToken); }
            catch (Exception e) { error = "Send outcome unknown. Check Google Messages before retrying. No automatic retry."; }
            final String outcome = result, failure = error;
            ui(token, () -> { sendBusy = false; callback.complete(outcome, failure); scheduleCloudSync(); });
        });
    }
    void close() {
        destroyed = true; main.removeCallbacksAndMessages(null); abandon();
        worker.shutdownNow(); presenceWorker.shutdownNow();
    }
    void networkChanged() {
        if (!canHandleCommands() || !recovery.enabled()) return;
        recovery.disconnected(); scheduleConnectionCheck(0); scheduleHeartbeat(0);
    }
    void reconnect() {
        if (cloudBusy || sendBusy || recovery.running()) { setState("Wait for the current operation before reconnecting."); return; }
        needsUserAttention = false; recovery.manualReconnect(); scheduleConnectionCheck(0);
    }

    private void scheduleConnectionCheck(long delay) {
        cloudHandler.removeCallbacks(connectionCheck);
        if (!destroyed && canHandleCommands() && recovery.enabled())
            cloudHandler.postDelayed(connectionCheck, delay);
    }

    private void checkConnection() {
        if (destroyed || !canHandleCommands() || !recovery.enabled()) return;
        // Never replace a bridge while a prepare/send or sync is using it.
        if (cloudBusy || sendBusy) { scheduleConnectionCheck(1000); return; }
        if (recovery.beginRestore(true, false)) { restorePairing(); return; }
        if (!recovery.beginProbe(true, false)) return;
        final int token = generation; final Bridge b = bridge;
        if (b == null) { recovery.probeFailed(); scheduleConnectionCheck(0); return; }
        worker.execute(() -> {
            try {
                b.checkConnection();
                ui(token, () -> { if (!recovery.enabled()) return; recovery.verified(); setState("Messages connection verified."); scheduleCloudSync(); scheduleCommands(0); });
            } catch (Exception e) {
                ui(token, () -> { if (!recovery.enabled()) return; recovery.probeFailed(); setState("Restoring the saved Messages pairing…"); scheduleConnectionCheck(recovery.retryDelay()); });
            }
        });
    }

    private void restorePairing() {
        // Keep the recovery state, but retire the old connection and drafts.
        // A send token from that connection is not transferred or replayed.
        final int token = replaceBridge(); setState("Restoring saved Messages pairing…");
        worker.execute(() -> {
            final Bridge b;
            try {
                String saved = sessions.load();
                if (saved.isEmpty()) { ui(token, () -> { recovery.stop(); needsUserAttention = true; setState("Not paired yet. Tap Connect Messages."); }); return; }
                b = Pairbridge.newBridge(saved, sink(token));
            } catch (Exception e) {
                ui(token, () -> { recovery.stop(); needsUserAttention = true; setState("Saved pairing could not be read. Tap Connect Messages to sign in again."); });
                return;
            }
            if (!installBridge(token, b)) return;
            try {
                b.connect();
                ui(token, () -> { setState("Pairing restored. Checking the phone connection…"); loadConversations(); });
            } catch (Exception e) { ui(token, () -> {
                recovery.restoreFailed();
                if (recovery.enabled()) { setState("Connection unavailable. Saved pairing kept; retrying shortly."); scheduleConnectionCheck(recovery.retryDelay()); }
            }); }
        });
    }

    private void scheduleCloudSync() {
        if (!canHandleCommands() || cloudLink == null || cloudRejected || destroyed) return;
        cloudHandler.removeCallbacks(autoSync);
        long delay = Math.max(5000, 30000 - (android.os.SystemClock.elapsedRealtime() - lastSyncStart));
        cloudHandler.postDelayed(autoSync, delay);
    }

    void syncCloud() {
        if (cloudBusy || sendBusy || cloudRejected) return;
        if (!canHandleCommands()) return;
        if (!recovery.ready()) { setCloudState("Waiting for the Messages connection to recover."); scheduleConnectionCheck(0); return; }
        final CloudLink linked = cloudLink; final Bridge b = bridge; final int token = generation;
        if (linked == null) { setCloudState("Link your Kyrex account first."); return; }
        if (b == null) { setCloudState("Reconnect Google Messages before syncing."); return; }
        cloudBusy = true; lastSyncStart = android.os.SystemClock.elapsedRealtime(); setCloudState("Reading phone snapshot and syncing…");
        worker.execute(() -> {
            try {
                String snapshot;
                try { snapshot = b.snapshot(); }
                catch (Exception e) { ui(token, () -> { recovery.disconnected(); scheduleConnectionCheck(5000); }); throw e; }
                if (token != generation || destroyed) throw new IllegalStateException("Phone connection changed. Sync again after reconnecting.");
                int count = linked.sync(snapshot);
                main.post(() -> { if (!destroyed) { cloudBusy = false; setCloudState("Synced " + count + " text messages to " + linked.origin + " at " + java.text.DateFormat.getTimeInstance().format(new java.util.Date()) + ". Ask Kyrex Chat: Show my texts."); } });
            } catch (Exception e) { main.post(() -> { if (!destroyed) { cloudBusy = false; cloudFailure(linked, e); setCloudState(e instanceof IllegalStateException || e instanceof IllegalArgumentException ? e.getMessage() : "Sync failed. Reconnect Google Messages and try Sync now. Previous cloud snapshot kept."); } }); }
        });
    }

    void setRemoteSending(boolean enabled) {
        if (cloudBusy) { setRemoteState("Wait for the current sync or command, then try again."); return; }
        if (!linked()) { setRemoteState("Link your Kyrex account first."); return; }
        final CloudLink changed = cloudLink.withSending(enabled); cloudBusy = true;
        worker.execute(() -> {
            try {
                cloudSessions.save(changed.saved());
                main.post(() -> { if (!destroyed) { cloudLink = changed; cloudBusy = false; setRemoteState(enabled ? "Chat sending enabled. Confirm each send in Chat." : "Chat sending turned off locally. Updating server…"); scheduleCommands(0); scheduleHeartbeat(0); } });
            } catch (Exception e) { main.post(() -> { if (!destroyed) { cloudBusy = false; setRemoteState("Could not save sending preference. Try again."); } }); }
        });
    }

    private void scheduleHeartbeat(long delay) {
        cloudHandler.removeCallbacks(heartbeatTask);
        if (canHandleCommands() && !destroyed && cloudLink != null && !cloudRejected)
            cloudHandler.postDelayed(heartbeatTask, delay);
    }

    void heartbeat() {
        if (!canHandleCommands() || cloudLink == null || cloudRejected) return;
        if (presenceBusy) { scheduleHeartbeat(1000); return; }
        final CloudLink linked = cloudLink; final String connection = recovery.presence();
        presenceBusy = true;
        presenceWorker.execute(() -> {
            Exception failure = null;
            try { linked.heartbeat(connection); } catch (Exception e) { failure = e; }
            final Exception error = failure;
            main.post(() -> {
                presenceBusy = false;
                if (destroyed) return;
                if (cloudLink != null && linked.token.equals(cloudLink.token)) {
                    if (error == null) {
                        presence.acknowledged(android.os.SystemClock.elapsedRealtime());
                        heartbeatState = "Last cloud check-in: " + java.text.DateFormat.getTimeInstance().format(new java.util.Date());
                    } else {
                        presence.failed(); cloudFailure(linked, error);
                        heartbeatState = safeCloudError(error, "Cloud check-in failed. Check internet access.");
                    }
                    changed();
                }
                scheduleHeartbeat(10000);
            });
        });
    }
    private void scheduleCommands(long delay) {
        cloudHandler.removeCallbacks(pollCommands);
        if (canHandleCommands() && !destroyed && cloudLink != null && !cloudRejected) cloudHandler.postDelayed(pollCommands, delay);
    }

    private void pollCloudCommands() {
        if (!canHandleCommands() || destroyed || cloudLink == null || cloudRejected) return;
        if (cloudBusy || sendBusy || bridge == null || !recovery.ready()) { scheduleCommands(5000); return; }
        final CloudLink linked = cloudLink; final Bridge b = bridge; final int token = generation;
        cloudBusy = true;
        worker.execute(() -> {
            String outcome = null; boolean sent = false, connectionFailed = false;
            try {
                JSONObject command = linked.poll().optJSONObject("command");
                if (command != null) {
                    String id = command.getString("id"), action = command.getString("action");
                    JSONObject result = new JSONObject();
                    if ("prepare".equals(action)) {
                        try {
                            if (!canHandleCommands() || token != generation || cloudLink != linked) throw new IllegalStateException("Connection changed");
                            result = new JSONObject(b.prepareCloudSend(command.getString("conversation_id"), command.getString("text")));
                            outcome = "Recipients checked. Review and confirm Send in Kyrex Chat.";
                        } catch (Exception e) { connectionFailed = true; result = new JSONObject().put("failed", true); outcome = "Could not prepare Chat message. Nothing sent."; }
                    } else if ("send".equals(action)) {
                        boolean accepted = false;
                        try {
                            if (!canHandleCommands() || token != generation || cloudLink != linked || !linked.allowSend) throw new IllegalStateException("Connection changed");
                            b.sendCloud(command.getString("token")); accepted = true; sent = true;
                            outcome = "Verified the outgoing Chat message in Google Messages. Confirm delivery with the recipient.";
                        } catch (Exception e) { connectionFailed = true; outcome = "Chat send outcome unknown. Check Google Messages before retrying. No automatic retry."; }
                        result.put("accepted", accepted);
                    } else { throw new IllegalStateException("Unsupported phone command"); }
                    // A failed acknowledgement never repeats the send RPC.
                    linked.acknowledge(id, action, result);
                }
            } catch (Exception e) { main.post(() -> cloudFailure(linked, e)); outcome = "Could not update Chat command status. Check Chat and Google Messages before retrying any send."; }
            final String state = outcome; final boolean syncAfter = sent;
            final boolean recoverAfter = connectionFailed;
            main.post(() -> {
                if (destroyed) return; cloudBusy = false;
                if (state != null) setRemoteState(state);
                else if (!linked.allowSend) setRemoteState("Chat sending is off. Pending unclaimed commands cancelled on server.");
                if (recoverAfter) { recovery.disconnected(); scheduleConnectionCheck(5000); }
                scheduleCommands(5000);
                if (syncAfter) scheduleCloudSync();
            });
        });
    }

    void removeCloudLink() {
        if (cloudBusy) { setCloudState("Wait for the current sync/link to finish, then remove the link."); return; }
        cloudLink = null; presence.reset(); changed(); cloudHandler.removeCallbacks(heartbeatTask); cloudHandler.removeCallbacks(autoSync); cloudHandler.removeCallbacks(pollCommands); cloudBusy = true; setRemoteState("No Kyrex account linked.");
        worker.execute(() -> { try { cloudSessions.clear(); main.post(() -> { if (!destroyed) { cloudBusy = false; setCloudState("Phone account link removed. Disconnect Messages in Chat to delete cloud data."); } }); }
            catch (Exception e) { main.post(() -> { if (!destroyed) { cloudBusy = false; setCloudState("Could not remove saved account link. Clear this app's storage in Android settings."); } }); } });
    }
}
