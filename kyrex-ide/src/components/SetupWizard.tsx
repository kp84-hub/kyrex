import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";

interface Props {
  onComplete: () => void;
  configPath: string;
}

const PROVIDER_PRESETS: Record<string, { baseUrl: string; label: string }> = {
  opencode: { baseUrl: "https://opencode.ai/zen/go/v1", label: "OpenCode (recommended)" },
  openrouter: { baseUrl: "https://openrouter.ai/api/v1", label: "OpenRouter" },
  openai: { baseUrl: "https://api.openai.com/v1", label: "OpenAI" },
  anthropic: { baseUrl: "https://api.anthropic.com", label: "Anthropic" },
  ollama: { baseUrl: "http://localhost:11434/v1", label: "Ollama (local)" },
  custom: { baseUrl: "", label: "Custom" },
};

export default function SetupWizard({ onComplete, configPath }: Props) {
  const [providerKey, setProviderKey] = useState("opencode");
  const [baseUrl, setBaseUrl] = useState(PROVIDER_PRESETS.opencode.baseUrl);
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[] | null>(null);
  const [status, setStatus] = useState<{ kind: "idle" | "testing" | "pass" | "fail"; message?: string }>({
    kind: "idle",
  });
  const [saving, setSaving] = useState(false);

  // Anthropic uses its own SDK path in ConfigManager; everything else goes
  // through the OpenAI-compatible path. This mirrors config.py's provider logic.
  const enginProvider = providerKey === "anthropic" ? "anthropic" : "openai";

  function handleProviderChange(key: string) {
    setProviderKey(key);
    setBaseUrl(PROVIDER_PRESETS[key].baseUrl);
    setModels(null);
    setStatus({ kind: "idle" });
  }

  async function handleFetchModels() {
    setStatus({ kind: "testing", message: "Fetching models..." });
    try {
      const raw = await invoke<string>("run_wizard_step", {
        requestJson: JSON.stringify({
          action: "list_models",
          provider: enginProvider,
          api_key: apiKey,
          base_url: baseUrl,
        }),
      });
      const result = JSON.parse(raw);
      if (result.success && result.models) {
        setModels(result.models);
        setStatus({ kind: "idle" });
        if (!model && result.models.length > 0) {
          setModel(result.models[0]);
        }
      } else {
        setModels(null);
        setStatus({ kind: "fail", message: "Could not fetch models. You can still type a model name manually." });
      }
    } catch (e) {
      setStatus({ kind: "fail", message: `Failed to fetch models: ${e}` });
    }
  }

  async function handleTestConnection() {
    setStatus({ kind: "testing", message: "Testing connection..." });
    try {
      const raw = await invoke<string>("run_wizard_step", {
        requestJson: JSON.stringify({
          action: "test_connection",
          provider: enginProvider,
          api_key: apiKey,
          base_url: baseUrl,
          model,
        }),
      });
      const result = JSON.parse(raw);
      if (result.success) {
        setStatus({ kind: "pass", message: result.message ?? "Connection passed." });
      } else {
        setStatus({ kind: "fail", message: result.message ?? "Connection failed." });
      }
    } catch (e) {
      setStatus({ kind: "fail", message: `Test errored: ${e}` });
    }
  }

  async function handleSave() {
    setSaving(true);
    try {
      const config = JSON.stringify(
        { provider: enginProvider, api_key: apiKey, base_url: baseUrl, model },
        null,
        2
      );
      await invoke("write_file_contents", {
        path: configPath,
        contents: config,
      });
      onComplete();
    } catch (e) {
      setStatus({ kind: "fail", message: `Failed to save config: ${e}` });
    } finally {
      setSaving(false);
    }
  }

  const canSave = apiKey.trim().length > 0 && model.trim().length > 0;

  return (
    <div className="setup-wizard">
      <div className="setup-wizard-card">
        <h2>Welcome to Kyrex IDE</h2>
        <p className="setup-wizard-subtitle">Connect an AI provider to get started.</p>

        <label className="setup-field">
          <span>Provider</span>
          <select value={providerKey} onChange={(e) => handleProviderChange(e.target.value)}>
            {Object.entries(PROVIDER_PRESETS).map(([key, preset]) => (
              <option key={key} value={key}>
                {preset.label}
              </option>
            ))}
          </select>
        </label>

        <label className="setup-field">
          <span>Base URL</span>
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        </label>

        <label className="setup-field">
          <span>API Key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-..."
          />
        </label>

        <label className="setup-field">
          <span>Model</span>
          {models ? (
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="e.g. deepseek/deepseek-v4-flash"
            />
          )}
        </label>

        <div className="setup-wizard-actions">
          <button onClick={handleFetchModels} disabled={!apiKey || status.kind === "testing"}>
            Fetch Models
          </button>
          <button onClick={handleTestConnection} disabled={!canSave || status.kind === "testing"}>
            Test Connection
          </button>
        </div>

        {status.kind !== "idle" && (
          <div className={`setup-status setup-status-${status.kind}`}>{status.message}</div>
        )}

        <button
          className="setup-wizard-save-btn"
          onClick={handleSave}
          disabled={!canSave || saving}
        >
          {saving ? "Saving..." : "Save & Continue"}
        </button>
      </div>
    </div>
  );
}
