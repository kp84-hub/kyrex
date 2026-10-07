// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.Manifest;
import android.content.pm.PackageManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Binder;
import android.os.Build;
import android.os.IBinder;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.Looper;

/** The one owner of pairing, sync, command polling and presence, including between screens. */
public class MessagesService extends Service {
    static final String START = "com.kyrex.pairing.START_MESSAGES";
    static final String STOP = "com.kyrex.pairing.STOP_MESSAGES";
    private static final String CHANNEL = "kyrex_messages_connection";
    private static final int NOTIFICATION = 8;
    private final LocalBinder binder = new LocalBinder();
    private CompanionRuntime.Listener ui;
    CompanionRuntime runtime;
    boolean background, visible;
    String backgroundError = "";
    private final Handler main = new Handler(Looper.getMainLooper());
    private ConnectivityManager.NetworkCallback networkCallback;
    private String notificationText = "";

    public final class LocalBinder extends Binder { MessagesService service() { return MessagesService.this; } }
    static boolean enabled(Context context) {
        return context.getSharedPreferences("messages_background", MODE_PRIVATE).getBoolean("enabled", false);
    }
    static boolean saveEnabled(Context context, boolean enabled) {
        return context.getSharedPreferences("messages_background", MODE_PRIVATE).edit().putBoolean("enabled", enabled).commit();
    }
    @Override public void onCreate() {
        super.onCreate();
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(
            CHANNEL, "Messages connection", NotificationManager.IMPORTANCE_LOW));
        runtime = createRuntime(new CompanionRuntime.Listener() {
            @Override public void changed() {
                if (background && runtime != null && runtime.cloudLoaded && (!runtime.linked() || runtime.needsUserAttention))
                    stopBackground();
                updateNotification();
                if (ui != null) ui.changed();
            }
            @Override public void event(String kind, String value) { if (ui != null) ui.event(kind, value); }
        });
        ConnectivityManager connectivity = getSystemService(ConnectivityManager.class);
        networkCallback = new ConnectivityManager.NetworkCallback() {
            private boolean internetValidated;
            @Override public void onAvailable(Network network) { internetValidated = false; main.post(() -> { if (runtime != null) runtime.networkChanged(); }); }
            @Override public void onLost(Network network) { internetValidated = false; main.post(() -> { if (runtime != null) runtime.networkChanged(); }); }
            @Override public void onCapabilitiesChanged(Network network, NetworkCapabilities capabilities) {
                // Wi-Fi can regain internet access without changing the default Network.
                boolean validated = capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
                if (validated && !internetValidated)
                    main.post(() -> { if (runtime != null) runtime.networkChanged(); });
                internetValidated = validated;
            }
        };
        try { connectivity.registerDefaultNetworkCallback(networkCallback); }
        catch (RuntimeException e) { networkCallback = null; } // Protocol events and bounded checks still recover.
    }
    CompanionRuntime createRuntime(CompanionRuntime.Listener listener) { return new CompanionRuntime(this, listener); }
    @Override public IBinder onBind(Intent intent) { return binder; }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && STOP.equals(intent.getAction())) { stopBackground(); return START_NOT_STICKY; }
        // A system restart restores only a previously opted-in service. No command is retained/replayed.
        if (intent == null && !enabled(this)) { stopSelf(); return START_NOT_STICKY; }
        if (intent != null && !START.equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        try {
            Notification notification = notification("Connecting Messages…");
            if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING);
            else startForeground(NOTIFICATION, notification);
            if (!saveEnabled(this, true)) throw new IllegalStateException("Preference could not be saved");
            background = true; backgroundError = "";
            runtime.setActive(visible, true);
            if (runtime.cloudLoaded && (!runtime.linked() || runtime.needsUserAttention)) {
                stopBackground(); return START_NOT_STICKY;
            }
            updateNotification();
            if (ui != null) ui.changed();
            return START_STICKY;
        } catch (RuntimeException error) {
            backgroundError = "Android could not start the Messages service. Open the companion and enable it again.";
            stopBackground();
            return START_NOT_STICKY;
        }
    }
    void attach(CompanionRuntime.Listener listener) { ui = listener; visible = true; runtime.setActive(true, background); listener.changed(); }
    void detach() { ui = null; visible = false; runtime.setActive(false, background); }
    void stopBackground() {
        saveEnabled(this, false); background = false; notificationText = "";
        runtime.setActive(visible, false);
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf();
        if (ui != null) ui.changed();
    }
    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, MessagesService.class).setAction(STOP), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("Kyrex Messages").setContentText(text).setContentIntent(open)
            .setCategory(Notification.CATEGORY_SERVICE).setOngoing(true).setOnlyAlertOnce(true)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .addAction(new Notification.Action.Builder(null, "Stop", stop).build()).build();
    }
    private void updateNotification() {
        if (!background || runtime == null) return;
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
        String text = runtime.notificationState();
        if (!text.equals(notificationText)) {
            notificationText = text; getSystemService(NotificationManager.class).notify(NOTIFICATION, notification(text));
        }
    }
    @Override public void onDestroy() {
        ui = null;
        if (networkCallback != null) try { getSystemService(ConnectivityManager.class).unregisterNetworkCallback(networkCallback); } catch (RuntimeException ignored) { }
        main.removeCallbacksAndMessages(null);
        if (runtime != null) { runtime.close(); runtime = null; }
        stopForeground(STOP_FOREGROUND_REMOVE); super.onDestroy();
    }
}
