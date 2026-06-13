#!/usr/bin/env python3
import re

with open('kyrex_engine/kyrex/config.py', 'r') as f:
    lines = f.readlines()

# Find the start and end of the section to replace
start_idx = None
end_idx = None

for i, line in enumerate(lines):
    if '# -- Step 1: Provider --------------------------------' in line:
        start_idx = i
    if start_idx is not None and '# -- Step 3: Authentication --------------------------' in line:
        end_idx = i
        break

if start_idx is None or end_idx is None:
    print("Error: Could not find the target section")
    exit(1)

print(f"Found section from line {start_idx+1} to {end_idx+1}")

# New content to insert
new_content = '''        # -- Step 1: Provider --------------------------------
        print(f"\\n  {C}Step 1:{N} {B}Provider{N}")
        print(f"  {W}  Choose the AI service Kyrex will use.{N}")
        print()
        print(f"  {C}  1.{N} {W}OpenCode (recommended){N}  — https://opencode.ai/zen/go/v1")
        print(f"  {C}  2.{N} {W}OpenRouter{N}           — https://openrouter.ai/api/v1")
        print(f"  {C}  3.{N} {W}OpenAI{N}               — https://api.openai.com/v1")
        print(f"  {C}  4.{N} {W}Anthropic{N}            — https://api.anthropic.com")
        print(f"  {C}  5.{N} {W}Custom{N}               — manual configuration")
        print()

        provider_choice = input(f"  {W}Select option{N} (1-5) [{C}1{N}]: ").strip() or "1"

        # Define preset configurations
        presets = {
            "1": {"label": "OpenCode (recommended)", "provider": "openai", "base_url": "https://opencode.ai/zen/go/v1"},
            "2": {"label": "OpenRouter", "provider": "openai", "base_url": "https://openrouter.ai/api/v1"},
            "3": {"label": "OpenAI", "provider": "openai", "base_url": "https://api.openai.com/v1"},
            "4": {"label": "Anthropic", "provider": "anthropic", "base_url": "https://api.anthropic.com"},
        }

        skip_base_url = False
        if provider_choice in presets:
            preset = presets[provider_choice]
            provider = preset["provider"]
            base_url = preset["base_url"]
            skip_base_url = True
            print(f"  {G}+  {N} {W}Selected: {C}{preset['label']}{N}")
            print(f"  {W}  Base URL: {C}{base_url}{N}")
        elif provider_choice == "5":
            # Custom - ask for provider type and base URL
            current_provider = self.get_provider()
            provider_raw = input(f"\\n  {W}Provider type [{C}anthropic{W}/{C}openai{W}]{N} ({C}{current_provider}{N}): ").strip().lower()
            provider = provider_raw or current_provider
            if provider not in ["openai", "anthropic"]:
                print(f"  {Y}Unknown provider '{provider}', defaulting to openai{N}")
                provider = "openai"
            skip_base_url = False
        else:
            # Default to OpenCode if invalid input
            preset = presets["1"]
            provider = preset["provider"]
            base_url = preset["base_url"]
            skip_base_url = True
            print(f"  {Y}Invalid choice, defaulting to OpenCode{N}")
            print(f"  {W}  Base URL: {C}{base_url}{N}")

        # -- Step 2: Base URL --------------------------------
        if not skip_base_url:
            print()
            print(f"  {C}Step 2:{N} {B}API Base URL{N}")
            print(f"  {W}  The endpoint for API requests.{N}")
            print(f"  {W}  Change this if you're using a proxy, local server, or alternative provider.{N}")
            print()

            # For custom, suggest the default for the chosen provider
            if provider == "anthropic":
                suggested = "https://api.anthropic.com"
            else:
                suggested = ""  # No default for custom OpenAI-compatible

            if suggested:
                print(f"  {W}  Default: {C}{suggested}{N}")
            base_url_input = input(f"  {W}Base URL{N}" + (f" (Enter for {C}{suggested}{N}): " if suggested else ": ")).strip()
            base_url = base_url_input or suggested
        else:
            # Skip message for preset providers
            print(f"\\n  {G}+  {N} {W}Base URL auto-filled, skipping manual entry.{N}")

'''

# Replace the section
new_lines = lines[:start_idx]
new_lines.append(new_content)
new_lines.extend(lines[end_idx:])

with open('kyrex_engine/kyrex/config.py', 'w') as f:
    f.writelines(new_lines)

print("Successfully updated config.py")
