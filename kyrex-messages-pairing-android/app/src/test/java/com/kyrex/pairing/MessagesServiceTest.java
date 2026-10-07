// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.app.Service;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.Looper;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.android.controller.ServiceController;
import org.robolectric.annotation.Config;
import org.robolectric.util.ReflectionHelpers;
import static org.robolectric.Shadows.shadowOf;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = {28, 35})
public class MessagesServiceTest {
    private ServiceController<TestService> controller;
    private TestService service;
    private FakeRuntime session;
    private final CompanionRuntime.Listener screen = new CompanionRuntime.Listener() {
        public void changed() { }
        public void event(String kind, String value) { }
    };
    public static class FakeRuntime extends CompanionRuntime {
        boolean screenActive, backgroundActive, closed;
        int networkChanges;
        FakeRuntime(android.content.Context context, Listener listener) {
            super(context, listener, false); cloudLoaded = true;
            cloudLink = new CloudLink("https://chat.kyrex.dev", "test_token", false);
        }
        @Override void setActive(boolean visible, boolean enabled) { screenActive = visible; backgroundActive = enabled; }
        @Override void networkChanged() { networkChanges++; }
        @Override void close() { closed = true; super.close(); }
    }
    public static class TestService extends MessagesService {
        @Override CompanionRuntime createRuntime(CompanionRuntime.Listener listener) { return new FakeRuntime(this, listener); }
    }
    @Before public void create() {
        MessagesService.saveEnabled(RuntimeEnvironment.getApplication(), false);
        controller = Robolectric.buildService(TestService.class).create();
        service = controller.get(); session = (FakeRuntime) service.runtime;
    }
    @After public void destroy() { if (controller != null) controller.destroy(); }
    private int enable() { return service.onStartCommand(new Intent(service, MessagesService.class).setAction(MessagesService.START), 0, 1); }
    @Test public void leavingAndReturningKeepsOneSessionWithoutChangingDraftGeneration() {
        assertEquals(Service.START_STICKY, enable());
        service.attach(screen); int generation = session.generation();
        service.detach();
        assertFalse(session.screenActive); assertTrue(session.backgroundActive);
        assertSame(session, service.runtime); assertFalse(session.closed);
        service.attach(screen);
        assertSame(session, service.runtime); assertEquals(generation, session.generation());
        assertTrue(session.screenActive); assertTrue(session.backgroundActive);
        assertFalse(session.cloudLink.allowSend); // Background connection never enables sending.
    }
    @Test public void stopDisablesRestartButKeepsBoundScreenUsable() {
        enable(); service.attach(screen);
        assertEquals(Service.START_NOT_STICKY, service.onStartCommand(new Intent(service, MessagesService.class).setAction(MessagesService.STOP), 0, 2));
        assertFalse(MessagesService.enabled(service)); assertFalse(session.backgroundActive);
        assertTrue(session.screenActive); assertFalse(session.closed);
        service.detach(); assertFalse(session.screenActive);
    }
    @Test public void nullRestartDoesNotStartWithoutPriorOptIn() {
        assertEquals(Service.START_NOT_STICKY, service.onStartCommand(null, 0, 1));
        assertFalse(service.background); assertFalse(MessagesService.enabled(service));
    }
    @Test public void optedInRestartCreatesFreshRuntimeWithoutReplayingOrEnablingSend() {
        enable(); int previousGeneration = session.generation();
        controller.destroy(); controller = null; assertTrue(session.closed);
        controller = Robolectric.buildService(TestService.class).create(); service = controller.get();
        assertNotSame(session, service.runtime);
        assertEquals(Service.START_STICKY, service.onStartCommand(null, 0, 2));
        assertEquals(previousGeneration, service.runtime.generation());
        assertFalse(service.runtime.cloudLink.allowSend);
    }
    @Test public void missingOrRevokedAccountLinkStopsBackgroundOperation() {
        session.cloudLink = null;
        assertEquals(Service.START_NOT_STICKY, enable());
        assertFalse(service.background); assertFalse(MessagesService.enabled(service));
    }
    @Test public void rejectedGooglePairingStopsBackgroundOperation() {
        session.needsUserAttention = true;
        assertEquals(Service.START_NOT_STICKY, enable()); assertFalse(service.background);
    }
    @Test public void validatedInternetRecoveryWakesRuntimeWithoutOpeningScreen() {
        enable(); service.detach();
        ConnectivityManager.NetworkCallback callback = ReflectionHelpers.getField(service, "networkCallback");
        int before = session.networkChanges;
        callback.onCapabilitiesChanged(null, new NetworkCapabilities());
        shadowOf(Looper.getMainLooper()).idle(); assertEquals(before, session.networkChanges);
        callback.onCapabilitiesChanged(null, new NetworkCapabilities().addCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
        shadowOf(Looper.getMainLooper()).idle(); assertEquals(before + 1, session.networkChanges);
        callback.onCapabilitiesChanged(null, new NetworkCapabilities().addCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
        shadowOf(Looper.getMainLooper()).idle(); assertEquals(before + 1, session.networkChanges);
        callback.onCapabilitiesChanged(null, new NetworkCapabilities());
        callback.onCapabilitiesChanged(null, new NetworkCapabilities().addCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
        shadowOf(Looper.getMainLooper()).idle(); assertEquals(before + 2, session.networkChanges);
        assertTrue(session.backgroundActive); assertFalse(session.screenActive);
    }
}
