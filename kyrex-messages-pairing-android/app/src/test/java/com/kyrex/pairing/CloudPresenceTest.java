// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;
import org.junit.Test;
import static org.junit.Assert.*;
public class CloudPresenceTest {
    @Test public void needsAnAcknowledgementAndExpiresAtServerDeadline() {
        CloudPresence presence = new CloudPresence();
        assertFalse(presence.current(0));
        presence.acknowledged(100);
        assertTrue(presence.current(45099));
        assertFalse(presence.current(45100));
        assertFalse(presence.current(99));
    }
    @Test public void failedHeartbeatAndNewLinkClearOldSuccess() {
        CloudPresence presence = new CloudPresence();
        presence.acknowledged(0); presence.failed(); assertFalse(presence.current(1));
        presence.acknowledged(2); presence.reset(); assertFalse(presence.current(3));
    }
}
