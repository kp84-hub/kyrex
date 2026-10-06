// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;
import org.junit.Test;
import static org.junit.Assert.*;
public class CloudLinkTest {
    @Test public void acceptsOnlyExplicitHttpsOrigin() throws Exception {
        assertEquals("https://chat.kyrex.dev", CloudLink.origin(" https://chat.kyrex.dev/ "));
        assertEquals("https://test.example:8443", CloudLink.origin("https://test.example:8443"));
        for (String value : new String[]{"http://chat.kyrex.dev", "https://user:secret@example.com", "https://example.com/api", "https://example.com?token=secret", "https://example.com#fragment", "javascript:alert(1)", "https:///missing"}) {
            try { CloudLink.origin(value); fail("Unsafe origin accepted: " + value); } catch (Exception expected) { }
        }
    }
}
