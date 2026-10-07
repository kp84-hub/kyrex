// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import java.net.URI;
import java.net.URL;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import org.json.JSONObject;

/** Phone-scoped snapshot/command credential. Never receives Google cookies or cloud reads. */
class CloudLink {
    static final class RevokedLinkException extends IllegalStateException {
        RevokedLinkException() { super("Kyrex account link revoked or expired. Get a new code in Chat and link again."); }
    }
    final String origin, token;
    final boolean allowSend;
    CloudLink(String origin, String token, boolean allowSend) { this.origin = origin; this.token = token; this.allowSend = allowSend; }
    CloudLink withSending(boolean enabled) { return new CloudLink(origin, token, enabled); }
    static String origin(String input) throws Exception {
        URI uri = new URI(input.trim());
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getRawUserInfo() != null ||
            uri.getRawQuery() != null || uri.getRawFragment() != null ||
            !(uri.getRawPath() == null || uri.getRawPath().isEmpty() || "/".equals(uri.getRawPath())))
            throw new IllegalArgumentException("Enter the HTTPS server address shown in Kyrex Chat, without a path.");
        return new URI("https", null, uri.getHost(), uri.getPort(), null, null, null).toASCIIString();
    }
    static CloudLink restore(String saved) throws Exception {
        if (saved.isEmpty()) return null;
        JSONObject data = new JSONObject(saved);
        String token = data.getString("token");
        if (!token.matches("[A-Za-z0-9_-]{30,100}")) throw new IllegalArgumentException("Invalid saved account link");
        return new CloudLink(origin(data.getString("origin")), token, data.optBoolean("allow_send", false));
    }
    String saved() throws Exception { return new JSONObject().put("origin", origin).put("token", token).put("allow_send", allowSend).toString(); }
    static CloudLink pair(String origin, String code) throws Exception {
        if (!code.matches("[A-Za-z0-9_-]{20,100}")) throw new IllegalArgumentException("Paste the pairing code from Kyrex Chat.");
        JSONObject response = post(origin, "/api/connections/messages/pair", "", new JSONObject().put("pairing_code", code).toString());
        return restore(new JSONObject().put("origin", origin).put("token", response.getString("upload_token")).toString());
    }
    int sync(String snapshot) throws Exception {
        return post(origin, "/api/connections/messages/sync", token, snapshot).getInt("count");
    }
    JSONObject poll() throws Exception {
        return post(origin, "/api/connections/messages/device/poll", token, new JSONObject().put("allow_send", allowSend).toString());
    }
    void heartbeat(String state) throws Exception {
        post(origin, "/api/connections/messages/device/heartbeat", token,
            new JSONObject().put("state", state).toString());
    }
    void acknowledge(String id, String action, JSONObject result) throws Exception {
        post(origin, "/api/connections/messages/device/ack", token, new JSONObject().put("id", id).put("action", action).put("result", result).toString());
    }
    private static JSONObject post(String origin, String path, String token, String json) throws Exception {
        byte[] data = json.getBytes(StandardCharsets.UTF_8);
        if (data.length > 1100000) throw new IllegalArgumentException("Snapshot too large. Previous cloud snapshot kept.");
        HttpURLConnection connection = (HttpURLConnection) new URL(origin + path).openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(15000); connection.setReadTimeout(30000);
            connection.setRequestMethod("POST"); connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            if (!token.isEmpty()) connection.setRequestProperty("Authorization", "Bearer " + token);
            connection.setFixedLengthStreamingMode(data.length);
            try (java.io.OutputStream out = connection.getOutputStream()) { out.write(data); }
            int status = connection.getResponseCode();
            if (status != 200) {
                if (status == 401 || status == 403) throw new RevokedLinkException();
                if (status == 400) throw new IllegalStateException("Pairing code or account link expired/revoked. Get a new code in Kyrex Chat and link again.");
                if (status == 404) throw new IllegalStateException("Phone pairing is unavailable on this server. The Kyrex server needs the companion update.");
                throw new IllegalStateException("Kyrex request failed (HTTP " + status + "). Check the server address and connection.");
            }
            try (InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[4096]; int count;
                while ((count = input.read(buffer)) != -1) {
                    if (output.size() + count > 16384) throw new IllegalStateException("Unexpected Kyrex response. Check the server address.");
                    output.write(buffer, 0, count);
                }
                return new JSONObject(output.toString(StandardCharsets.UTF_8.name()));
            }
        } catch (java.io.IOException e) {
            throw new IllegalStateException("Could not reach Kyrex. Check internet access and the server address. If linking failed, get a new pairing code before retrying.");
        } finally { connection.disconnect(); }
    }
}

