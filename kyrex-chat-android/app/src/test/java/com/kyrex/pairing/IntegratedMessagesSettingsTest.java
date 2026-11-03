// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Intent;
import android.os.Looper;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.List;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import org.robolectric.util.ReflectionHelpers;
import static org.junit.Assert.*;
import static org.robolectric.Shadows.shadowOf;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = {28})
public class IntegratedMessagesSettingsTest {
    static class TestRuntime extends CompanionRuntime {
        boolean paired, account;
        int syncs, checkins, reconnects, sendPreferences;
        TestRuntime(android.content.Context context, Listener listener) {
            super(context, listener, false); cloudLoaded = true; needsUserAttention = true;
        }
        @Override boolean ready() { return paired; }
        @Override boolean linked() { return account; }
        @Override void syncCloud() { syncs++; }
        @Override void heartbeat() { checkins++; }
        @Override void reconnect() { reconnects++; }
        @Override void setActive(boolean visible, boolean background) { }
        @Override void setRemoteSending(boolean enabled) { sendPreferences++; cloudLink = cloudLink.withSending(enabled); }
    }
    public static class TestService extends MessagesService {
        @Override CompanionRuntime createRuntime(CompanionRuntime.Listener listener) { return new TestRuntime(this, listener); }
    }
    private List<View> views(View view) {
        List<View> all = new ArrayList<>(); all.add(view);
        if (view instanceof ViewGroup) for (int i=0;i<((ViewGroup)view).getChildCount();i++) all.addAll(views(((ViewGroup)view).getChildAt(i)));
        return all;
    }
    private List<View> all(IntegratedMessagesActivity activity) { return views(activity.getWindow().getDecorView()); }
    private String text(IntegratedMessagesActivity activity) {
        StringBuilder result = new StringBuilder();
        for (View v : all(activity)) if (v instanceof TextView) result.append(((TextView)v).getText()).append('\n');
        return result.toString();
    }
    private Button button(IntegratedMessagesActivity activity, String title) {
        return (Button) all(activity).stream().filter(v -> v instanceof Button && title.contentEquals(((Button)v).getText())).findFirst().orElseThrow();
    }
    private boolean connectedTitle(IntegratedMessagesActivity activity) {
        return all(activity).stream().anyMatch(v -> v instanceof TextView && "Messages connected".contentEquals(((TextView)v).getText()));
    }
    private void run(java.util.function.BiConsumer<IntegratedMessagesActivity, TestService> check) {
        var service = Robolectric.buildService(TestService.class).create();
        shadowOf(RuntimeEnvironment.getApplication()).setComponentNameAndServiceForBindService(
            new ComponentName(RuntimeEnvironment.getApplication(), MessagesService.class), service.get().onBind(new Intent()));
        var activity = Robolectric.buildActivity(IntegratedMessagesActivity.class).create();
        try { shadowOf(Looper.getMainLooper()).idle(); check.accept(activity.get(), service.get()); }
        finally { activity.destroy(); service.destroy(); }
    }
    @Test public void freshSetupShowsOnlyPairingWithoutDeveloperInputsOrAutomaticConsent() {
        run((activity, service) -> {
            assertTrue(text(activity).contains("Step 1 of 4"));
            assertFalse(all(activity).stream().anyMatch(v -> v instanceof EditText));
            assertFalse(text(activity).contains("Link Kyrex account"));
            assertFalse(text(activity).contains("Allow sends confirmed"));
            assertFalse(text(activity).contains("Review message"));
            assertFalse(service.background);
            assertEquals(0, ((TestRuntime)service.runtime).sendPreferences);
            button(activity, "Pair Google Messages").performClick();
            assertNotNull(org.robolectric.shadows.ShadowAlertDialog.getLatestAlertDialog());
            org.robolectric.shadows.ShadowAlertDialog.getLatestAlertDialog().getButton(AlertDialog.BUTTON_NEGATIVE).performClick();
            assertTrue(text(activity).contains("Step 1 of 4"));
        });
    }
    @Test public void pairingLinkOptionsAndVerifiedSyncAdvanceWithoutEnablingSends() {
        run((activity, service) -> {
            TestRuntime runtime = (TestRuntime) service.runtime;
            runtime.paired = true; runtime.needsUserAttention = false; activity.changed();
            assertTrue(text(activity).contains("Step 2 of 4"));
            assertNotNull(button(activity, "Link Kyrex account"));
            runtime.account = true; runtime.cloudLink = new CloudLink("https://chat.example", "t".repeat(43), false);
            activity.event("LINKED", "");
            assertTrue(text(activity).contains("Step 3 of 4"));
            assertFalse(service.background); assertEquals(0, runtime.sendPreferences);
            button(activity, "Allow sends confirmed in Kyrex Chat").performClick();
            var dialog = org.robolectric.shadows.ShadowAlertDialog.getLatestAlertDialog();
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick();
            assertEquals(0, runtime.sendPreferences);
            button(activity, "Continue to connection check").performClick();
            assertTrue(text(activity).contains("Step 4 of 4"));
            assertEquals(1, runtime.syncs); assertEquals(1, runtime.checkins);
            assertFalse(connectedTitle(activity));
            runtime.lastCloudSyncAt = 1000; runtime.lastCloudSyncCount = 0; activity.changed();
            assertTrue(text(activity).contains("Step 4 of 4"));
            CloudPresence presence = ReflectionHelpers.getField(runtime, "presence");
            presence.acknowledged(SystemClock.elapsedRealtime()); activity.changed();
            assertTrue(connectedTitle(activity));
            assertTrue(text(activity).contains("Synced 0 text messages"));
            assertFalse(runtime.cloudLink.allowSend); assertFalse(service.background);
            button(activity, "Reconnect saved pairing").performClick();
            assertEquals(1, runtime.reconnects); assertEquals(0, runtime.sendPreferences);
            runtime.abandon();
            assertEquals(0, runtime.lastCloudSyncAt);
            assertFalse(runtime.cloudVerified());
            runtime.paired = false; runtime.needsUserAttention = true; activity.changed();
            assertTrue(text(activity).contains("Step 1 of 4"));
            assertFalse(connectedTitle(activity));
        });
    }
    @Test public void sendOptInNeedsExplicitPositiveChoiceAndStaysIndependentOfBackground() {
        run((activity, service) -> {
            TestRuntime runtime = (TestRuntime)service.runtime;
            runtime.paired = runtime.account = true; runtime.needsUserAttention = false;
            runtime.cloudLink = new CloudLink("https://chat.example", "t".repeat(43), false);
            activity.event("LINKED", "");
            button(activity, "Allow sends confirmed in Kyrex Chat").performClick();
            org.robolectric.shadows.ShadowAlertDialog.getLatestAlertDialog().getButton(AlertDialog.BUTTON_POSITIVE).performClick();
            shadowOf(Looper.getMainLooper()).idle();
            activity.changed();
            assertTrue(runtime.cloudLink.allowSend); assertEquals(1, runtime.sendPreferences);
            assertFalse(service.background);
            assertNotNull(button(activity, "Turn off Chat sending"));
        });
    }
}
