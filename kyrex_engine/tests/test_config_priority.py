import os
import pytest
import json
from pathlib import Path


class TestGetProviderPriority:
    """Test get_provider() priority: KYREX_PROVIDER > PROVIDER > config."""

    def test_env_kyrex_overrides_all(self, config_manager):
        """KYREX_PROVIDER env should win over everything."""
        os.environ["KYREX_PROVIDER"] = "openai"
        os.environ["PROVIDER"] = "ollama"
        config_manager._data = {"provider": "openrouter"}
        
        result = config_manager.get_provider()
        assert result == "openai"

    def test_env_provider_overrides_config(self, config_manager):
        """PROVIDER env should win over config file."""
        os.environ["PROVIDER"] = "ollama"
        config_manager._data = {"provider": "openai"}
        
        result = config_manager.get_provider()
        assert result == "ollama"

    def test_config_provider_fallback(self, config_manager):
        """Config file provider should be used when no env vars set."""
        config_manager._data = {"provider": "openrouter"}
        
        result = config_manager.get_provider()
        assert result == "openrouter"

    def test_default_openai(self, config_manager):
        """Default to 'openai' when nothing is set."""
        result = config_manager.get_provider()
        assert result == "openai"


class TestGetApiKeyPriority:
    """Test get_api_key() priority chain."""

    def test_env_kyrex_api_key_first(self, config_manager):
        """KYREX_API_KEY env should be checked first."""
        os.environ["KYREX_API_KEY"] = "kyrex-key"
        config_manager._data = {"api_key": "config-key"}
        
        result = config_manager.get_api_key()
        assert result == "kyrex-key"

    def test_api_key_env_second(self, config_manager):
        """api_key_env in config should be checked second."""
        os.environ["MY_CUSTOM_KEY"] = "custom-env-key"
        config_manager._data = {"api_key_env": "MY_CUSTOM_KEY"}
        
        result = config_manager.get_api_key()
        assert result == "custom-env-key"

    def test_api_key_config_third(self, config_manager):
        """api_key in config should be checked third."""
        config_manager._data = {"api_key": "config-key"}
        
        result = config_manager.get_api_key()
        assert result == "config-key"

    def test_prefixed_key_fourth(self, config_manager):
        """{provider}_api_key in config should be checked fourth."""
        config_manager._data = {"openai_api_key": "prefixed-key"}
        config_manager.get_provider = lambda: "openai"  # Mock
        
        result = config_manager.get_api_key()
        assert result == "prefixed-key"

    def test_openai_env_fallback(self, config_manager):
        """OPENAI_API_KEY env should be fallback for openai provider."""
        os.environ["OPENAI_API_KEY"] = "openai-env-key"
        config_manager._data = {}
        config_manager.get_provider = lambda: "openai"  # Mock
        
        result = config_manager.get_api_key()
        assert result == "openai-env-key"


class TestGetMethodPriority:
    """Test get() method priority: env vars > config > prefixed config."""

    def test_get_env_kyrex_prefix(self, config_manager):
        """get('api_key') should check KYREX_API_KEY first."""
        os.environ["KYREX_API_KEY"] = "kyrex-val"
        config_manager._data = {"api_key": "config-val"}
        
        result = config_manager.get("api_key")
        assert result == "kyrex-val"

    def test_get_config_key(self, config_manager):
        """get('api_key') should check config key second."""
        config_manager._data = {"api_key": "config-val"}
        
        result = config_manager.get("api_key")
        assert result == "config-val"

    def test_get_prefixed_config(self, config_manager):
        """get('api_key') should check {provider}_api_key third."""
        config_manager._data = {"openai_api_key": "prefixed-val"}
        config_manager.get_provider = lambda: "openai"  # Mock
        
        result = config_manager.get("api_key")
        assert result == "prefixed-val"


class TestGetBaseUrlPriority:
    """Test get('base_url') priority chain."""

    def test_env_kyrex_base_url_first(self, config_manager):
        """KYREX_BASE_URL env should be checked first."""
        os.environ["KYREX_BASE_URL"] = "https://kyrex.example.com"
        config_manager._data = {"base_url": "https://config.example.com"}
        
        result = config_manager.get("base_url")
        assert result == "https://kyrex.example.com"

    def test_config_base_url_second(self, config_manager):
        """base_url in config should be used when no env vars set."""
        config_manager._data = {"base_url": "https://config.example.com"}
        
        result = config_manager.get("base_url")
        assert result == "https://config.example.com"

    def test_openai_base_url_fallback(self, config_manager):
        """OPENAI_BASE_URL env should be fallback for openai provider."""
        os.environ["OPENAI_BASE_URL"] = "https://openai.example.com"
        config_manager.get_provider = lambda: "openai"  # Mock
        
        result = config_manager.get("base_url")
        assert result == "https://openai.example.com"

    def test_provider_default_when_nothing_set(self, config_manager):
        """Should return None when nothing is set."""
        config_manager.get_provider = lambda: "openai"  # Mock
        
        result = config_manager.get("base_url")
        assert result is None

    def test_openai_base_url_only_for_openai_provider(self, config_manager):
        """OPENAI_BASE_URL should only apply when provider is openai."""
        os.environ["OPENAI_BASE_URL"] = "https://openai.example.com"
        config_manager.get_provider = lambda: "anthropic"  # Mock
        
        result = config_manager.get("base_url")
        assert result is None


class TestGetHeaders:
    """Test get_headers() method."""

    def test_empty_headers_by_default(self, config_manager):
        """Should return empty dict when no headers configured."""
        result = config_manager.get_headers()
        assert result == {}

    def test_returns_configured_headers(self, config_manager):
        """Should return headers from config."""
        config_manager._data = {"headers": {"X-Custom": "value", "Authorization": "Bearer token"}}
        
        result = config_manager.get_headers()
        assert result == {"X-Custom": "value", "Authorization": "Bearer token"}


class TestIsConfigured:
    """Test is_configured() method."""

    def test_false_when_no_api_key(self, config_manager):
        """Should return False when no API key is available."""
        assert config_manager.is_configured() is False

    def test_true_when_api_key_available(self, config_manager):
        """Should return True when API key is available."""
        config_manager._data = {"api_key": "test-key"}
        
        assert config_manager.is_configured() is True

    def test_true_when_api_key_in_env(self, config_manager):
        """Should return True when API key is in environment."""
        os.environ["KYREX_API_KEY"] = "env-key"
        
        assert config_manager.is_configured() is True


class TestLoadAndSave:
    """Test load() and save() methods."""

    def test_load_reads_config_file(self, config_manager, temp_config_file):
        """load() should read and parse config file."""
        # Write test config
        with open(temp_config_file, 'w') as f:
            f.write('{"provider": "openai", "api_key": "test-key"}')
        
        config_manager.config_path = Path(temp_config_file)
        result = config_manager.load()
        
        assert result["provider"] == "openai"
        assert result["api_key"] == "test-key"

    def test_load_lowercases_keys(self, config_manager, temp_config_file):
        """load() should lowercase all config keys."""
        with open(temp_config_file, 'w') as f:
            f.write('{"PROVIDER": "openai", "API_KEY": "test-key"}')
        
        config_manager.config_path = Path(temp_config_file)
        config_manager.load()
        
        assert "provider" in config_manager._data
        assert "api_key" in config_manager._data
        assert "PROVIDER" not in config_manager._data

    def test_load_handles_invalid_json(self, config_manager, temp_config_file):
        """load() should return empty dict on invalid JSON."""
        with open(temp_config_file, 'w') as f:
            f.write('invalid json')
        
        config_manager.config_path = Path(temp_config_file)
        result = config_manager.load()
        
        assert result == {}

    def test_save_writes_config_file(self, config_manager, temp_config_file):
        """save() should write config to file."""
        config_manager.config_path = Path(temp_config_file)
        config_manager.save({"provider": "openai", "api_key": "test-key"})
        
        with open(temp_config_file, 'r') as f:
            saved = json.loads(f.read())
        
        assert saved["provider"] == "openai"
        assert saved["api_key"] == "test-key"

    def test_save_merges_with_existing(self, config_manager, temp_config_file):
        """save() should merge with existing config, not overwrite."""
        # Write initial config
        with open(temp_config_file, 'w') as f:
            f.write('{"provider": "openai", "model": "gpt-4"}')
        
        config_manager.config_path = Path(temp_config_file)
        config_manager.save({"api_key": "new-key"})
        
        with open(temp_config_file, 'r') as f:
            saved = json.loads(f.read())
        
        assert saved["provider"] == "openai"  # Kept
        assert saved["model"] == "gpt-4"  # Kept
        assert saved["api_key"] == "new-key"  # Added

    def test_save_creates_parent_dirs(self, config_manager, temp_config_file):
        """save() should create parent directories if needed."""
        nested_path = Path(temp_config_file).parent / "nested" / "config.json"
        config_manager.config_path = nested_path
        config_manager.save({"provider": "openai"})
        
        assert nested_path.exists()


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_strips_whitespace_from_env_values(self, config_manager):
        """Env var values should have whitespace stripped."""
        os.environ["KYREX_API_KEY"] = "  test-key  "
        
        result = config_manager.get_api_key()
        assert result == "test-key"

    def test_strips_whitespace_from_config_values(self, config_manager):
        """Config values should have whitespace stripped."""
        config_manager._data = {"api_key": "  test-key  "}
        
        result = config_manager.get_api_key()
        assert result == "test-key"

    def test_empty_string_env_var_treated_as_unset(self, config_manager):
        """Empty string env vars should be treated as unset."""
        os.environ["KYREX_API_KEY"] = ""
        config_manager._data = {"api_key": "config-key"}
        
        result = config_manager.get_api_key()
        assert result == "config-key"

    def test_get_returns_default_when_key_not_found(self, config_manager):
        """get() should return default when key not found."""
        result = config_manager.get("nonexistent", "default-val")
        assert result == "default-val"

    def test_get_returns_none_without_default(self, config_manager):
        """get() should return None when key not found and no default."""
        result = config_manager.get("nonexistent")
        assert result is None

    def test_api_key_env_returns_none_on_missing_env_var(self, config_manager):
        """Should return None when api_key_env points to unset var (no raise)."""
        config_manager._data = {"api_key_env": "MISSING_VAR"}
        
        result = config_manager.get_api_key()
        assert result is None


class TestConfigResolution:
    """Test project vs global config file resolution."""

    def test_project_config_preferred(self, tmp_path, monkeypatch):
        """Project .px/config.json should be preferred over global."""
        fake_home = tmp_path / "home"
        (fake_home / ".px").mkdir(parents=True)
        (fake_home / ".px" / "config.json").write_text('{"provider": "global-cfg", "model": "gpt-4"}')
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(fake_home) + p[1:] if p.startswith("~") else p)

        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".px").mkdir()
        (project / ".px" / "config.json").write_text('{"provider": "project-cfg", "model": "claude-3"}')

        monkeypatch.chdir(project)

        from kyrex.config import ConfigManager
        cm = ConfigManager()
        assert cm.config_path == project / ".px" / "config.json"
        assert cm._config_source == "project"
        data = cm.load()
        assert data["provider"] == "project-cfg"
        assert data["model"] == "claude-3"

    def test_global_config_fallback(self, tmp_path, monkeypatch):
        """Global ~/.px/config.json should be used when project has none."""
        fake_home = tmp_path / "home"
        (fake_home / ".px").mkdir(parents=True)
        (fake_home / ".px" / "config.json").write_text('{"provider": "global-cfg", "model": "gpt-4"}')
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(fake_home) + p[1:] if p.startswith("~") else p)

        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".px").mkdir()  # dir exists but no config.json inside

        monkeypatch.chdir(project)

        from kyrex.config import ConfigManager
        cm = ConfigManager()
        assert cm.config_path == fake_home / ".px" / "config.json"
        assert cm._config_source == "global"
        data = cm.load()
        assert data["provider"] == "global-cfg"
        assert data["model"] == "gpt-4"

    def test_neither_config_exists(self, tmp_path, monkeypatch, capsys):
        """Absence of both project and global config should be reported clearly."""
        fake_home = tmp_path / "home"
        (fake_home / ".px").mkdir(parents=True)
        # No config.json in fake_home/.px/
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(fake_home) + p[1:] if p.startswith("~") else p)

        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".px").mkdir()

        monkeypatch.chdir(project)

        from kyrex.config import ConfigManager
        cm = ConfigManager()
        assert cm.config_path == fake_home / ".px" / "config.json"
        assert cm._config_source == "missing"
        data = cm.load()
        assert data == {}

        err = capsys.readouterr().err
        assert "No config found" in err
        assert "/setup" in err
