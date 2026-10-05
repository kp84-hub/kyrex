// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;
import java.net.URI;
final class LoginRules {
    static boolean allowed(String value) {
        try {
            URI uri = new URI(value);
            String host = uri.getHost();
            return "https".equals(uri.getScheme()) && uri.getUserInfo() == null &&
                (uri.getPort() == -1 || uri.getPort() == 443) &&
                ("accounts.google.com".equals(host) || "messages.google.com".equals(host));
        } catch (Exception e) { return false; }
    }
    static boolean complete(String value) {
        try {
            URI uri = new URI(value);
            return allowed(value) && "messages.google.com".equals(uri.getHost()) && "/web/config".equals(uri.getPath());
        } catch (Exception e) { return false; }
    }
}
