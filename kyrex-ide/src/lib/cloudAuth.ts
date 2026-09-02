import { invoke } from "@tauri-apps/api/core";
import { installDesktopAuthHandler, startDesktopLogin } from "./desktopAuth";

export type CloudAuthState = "checking" | "unauthenticated" | "authenticated" | "refreshing" | "expired" | "error";

type Listener = () => void;

export class CloudAuthClient {
  private readonly baseUrl: string;
  private accessToken: string | null = null;
  private refreshToken: string | null = null;
  private username: string | null = null;
  private state: CloudAuthState = "checking";
  private listeners = new Set<Listener>();
  private disposeDeepLink: (() => void) | null = null;

  constructor(baseUrl: string) { this.baseUrl = baseUrl.replace(/\/$/, ""); }
  get authState() { return this.state; }
  get user() { return this.username; }
  get token() { return this.accessToken; }
  subscribe(listener: Listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private setState(state: CloudAuthState) { this.state = state; this.listeners.forEach((listener) => listener()); }
  private async saveRefresh(token: string) { await invoke("save_desktop_refresh_token", { token }); this.refreshToken = token; }
  private async clearRefresh() { this.refreshToken = null; await invoke("clear_desktop_refresh_token"); }

  async initialize() {
    this.disposeDeepLink = await installDesktopAuthHandler(this.baseUrl, async (tokens) => {
      try { await this.acceptExchange(tokens); } catch { this.setState("error"); }
    }, () => this.setState("error"));
    try {
      this.refreshToken = await invoke<string | null>("load_desktop_refresh_token");
      if (this.refreshToken) await this.refresh(); else this.setState("unauthenticated");
    } catch { this.setState("error"); }
  }

  async signIn() { await startDesktopLogin(this.baseUrl); }

  async acceptExchange(tokens: { access_token: string; refresh_token: string; username: string }) {
    this.accessToken = tokens.access_token; this.username = tokens.username;
    await this.saveRefresh(tokens.refresh_token); this.setState("authenticated");
  }

  async refresh(): Promise<boolean> {
    if (!this.refreshToken) return false;
    this.setState("refreshing");
    try {
      const response = await fetch(`${this.baseUrl}/auth/desktop/refresh`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: this.refreshToken }) });
      if (!response.ok) throw new Error("refresh rejected");
      await this.acceptExchange(await response.json());
      return true;
    } catch { await this.expire(); return false; }
  }

  async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (this.accessToken) headers.set("Authorization", `Bearer ${this.accessToken}`);
    let response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    if (response.status === 401 && this.refreshToken) {
      if (await this.refresh()) {
        headers.set("Authorization", `Bearer ${this.accessToken}`);
        response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
      }
    }
    if (response.status === 401 && this.refreshToken === null) this.setState("expired");
    return response;
  }

  async signOut() {
    try { if (this.accessToken) await fetch(`${this.baseUrl}/auth/desktop/logout`, { method: "POST", headers: { Authorization: `Bearer ${this.accessToken}` } }); } finally {
      this.accessToken = null; this.username = null; await this.clearRefresh(); this.setState("unauthenticated");
    }
  }

  async expire() { this.accessToken = null; this.username = null; await this.clearRefresh(); this.setState("expired"); }
  dispose() { this.disposeDeepLink?.(); this.disposeDeepLink = null; }
}
