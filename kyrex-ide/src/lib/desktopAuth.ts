import { openUrl } from "@tauri-apps/plugin-opener";
import { onOpenUrl } from "@tauri-apps/plugin-deep-link";

export const DESKTOP_REDIRECT_URI = "kyrex://auth/callback";

interface PendingLogin {
  state: string;
  verifier: string;
}

let pending: PendingLogin | null = null;
let callbackConsumed = false;
let cleanup: (() => void) | null = null;

function randomUrlSafe(bytes = 32): string {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  return btoa(String.fromCharCode(...data))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256Base64Url(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function startDesktopLogin(cloudBaseUrl: string): Promise<void> {
  pending = { state: randomUrlSafe(), verifier: randomUrlSafe(48) };
  callbackConsumed = false;
  const challenge = await sha256Base64Url(pending.verifier);
  const url = new URL("/auth/desktop/start", cloudBaseUrl);
  url.search = new URLSearchParams({
    state: pending.state,
    redirect_uri: DESKTOP_REDIRECT_URI,
    code_challenge: challenge,
    code_challenge_method: "S256",
  }).toString();
  await openUrl(url.toString());
}

export async function installDesktopAuthHandler(
  cloudBaseUrl: string,
  onAuthenticated: (tokens: { access_token: string; refresh_token: string; username: string }) => void,
  onError: (message: string) => void,
): Promise<() => void> {
  cleanup?.();
  cleanup = await onOpenUrl(async (urls) => {
    if (callbackConsumed || urls.length !== 1) return onError("Malformed desktop OAuth callback");
    const parsed = new URL(urls[0]);
    if (parsed.protocol !== "kyrex:" || parsed.hostname !== "auth" || parsed.pathname !== "/callback") {
      return onError("Invalid desktop OAuth callback URL");
    }
    const attempt = pending;
    const code = parsed.searchParams.get("code");
    const state = parsed.searchParams.get("state");
    if (!attempt || !code || state !== attempt.state) return onError("Invalid or replayed desktop OAuth callback");
    callbackConsumed = true;
    pending = null;
    try {
      const response = await fetch(new URL("/auth/desktop/exchange", cloudBaseUrl), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, redirect_uri: DESKTOP_REDIRECT_URI, code_verifier: attempt.verifier }),
      });
      if (!response.ok) throw new Error(`Desktop OAuth exchange failed (${response.status})`);
      const result = await response.json() as { access_token?: string; refresh_token?: string; username?: string };
      if (!result.access_token || !result.refresh_token || !result.username) throw new Error("Desktop OAuth exchange returned incomplete credentials");
      onAuthenticated({ access_token: result.access_token, refresh_token: result.refresh_token, username: result.username });
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  });
  return () => { cleanup?.(); cleanup = null; };
}
