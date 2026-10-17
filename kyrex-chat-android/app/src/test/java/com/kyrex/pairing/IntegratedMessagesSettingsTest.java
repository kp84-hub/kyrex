// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.List;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;
import org.robolectric.util.ReflectionHelpers;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = {28})
public class IntegratedMessagesSettingsTest {
    public static class TestService extends MessagesService {
        @Override CompanionRuntime createRuntime(CompanionRuntime.Listener listener) {
            return new CompanionRuntime(this, listener, false);
        }
    }
    private List<View> views(View view) {
        List<View> all = new ArrayList<>(); all.add(view);
        if (view instanceof ViewGroup) for (int i=0;i<((ViewGroup)view).getChildCount();i++) all.addAll(views(((ViewGroup)view).getChildAt(i)));
        return all;
    }
    @Test public void unifiedMessagesShowsSetupAndConsentWithoutDeveloperInputsOrSendTest() {
        var service = Robolectric.buildService(TestService.class).create();
        var activity = Robolectric.buildActivity(IntegratedMessagesActivity.class).create();
        try {
            ReflectionHelpers.setField(activity.get(), "service", service.get());
            ReflectionHelpers.setField(activity.get(), "runtime", service.get().runtime);
            ReflectionHelpers.callInstanceMethod(activity.get(), "home");
            List<View> all = views(activity.get().getWindow().getDecorView());
            assertFalse(all.stream().anyMatch(v -> v instanceof EditText));
            assertTrue(all.stream().anyMatch(v -> v instanceof TextView && "Connect messages to Kyrex".contentEquals(((TextView)v).getText())));
            assertTrue(all.stream().anyMatch(v -> v instanceof TextView && "Allow sends confirmed in Kyrex Chat".contentEquals(((TextView)v).getText())));
            assertFalse(all.stream().anyMatch(v -> v instanceof TextView && "Review message".contentEquals(((TextView)v).getText())));
            assertFalse(service.get().background);
        } finally { activity.destroy(); service.destroy(); }
    }
}
