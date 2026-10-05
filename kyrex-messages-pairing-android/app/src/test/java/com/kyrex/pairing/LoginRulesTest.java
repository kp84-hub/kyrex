package com.kyrex.pairing;
import org.junit.Test;
import static org.junit.Assert.*;
public class LoginRulesTest {
    @Test public void rejectsLookalikesAndInsecurePages() {
        assertFalse(LoginRules.allowed("https://messages.google.com.evil.test/web/config"));
        assertFalse(LoginRules.allowed("https://messages.google.com@evil.test/web/config"));
        assertFalse(LoginRules.allowed("http://messages.google.com/web/config"));
        assertFalse(LoginRules.allowed("https://messages.google.com:444/web/config"));
        assertFalse(LoginRules.allowed("javascript:alert(1)"));
    }
    @Test public void harvestOnlyAtExactConfigEndpoint() {
        assertTrue(LoginRules.complete("https://messages.google.com/web/config?test=1"));
        assertTrue(LoginRules.allowed("https://accounts.google.com/signin"));
        assertFalse(LoginRules.complete("https://accounts.google.com/signin?next=messages.google.com/web/config"));
        assertFalse(LoginRules.complete("https://messages.google.com/web"));
    }
}
