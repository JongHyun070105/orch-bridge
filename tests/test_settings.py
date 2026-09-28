import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
p=ROOT/'app/settings_cli.py'; spec=importlib.util.spec_from_file_location('settings_cli',p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def test_default_models_and_providers_are_portable():
    d=m.defaults(); assert set(d['providers'])=={'codex','claude','agy','commandcode'}
    assert d['models']['claude_sonnet']=='sonnet'
    assert d['models']['commandcode']=='xiaomi/mimo-v2.5-pro'

def test_auto_provider_only_enables_installed_binary():
    d=m.defaults()
    with patch.object(m.shutil,'which',return_value=None): assert m.effective_enabled(d,'codex') is False
    with patch.object(m.shutil,'which',return_value='/usr/bin/codex'): assert m.effective_enabled(d,'codex') is True

def test_missing_optional_providers_are_reported_without_breaking_auto_mode():
    with patch.object(m.shutil,'which',return_value=None):
        detected=m.detect()
        defaults=m.defaults()
        assert all(item['installed'] is False for item in detected.values())
        assert all(m.effective_enabled(defaults,name) is False for name in m.PROVIDER_BINARIES)

def test_model_values_can_be_changed_through_settings_command(tmp_path, monkeypatch):
    config_dir=tmp_path/'.config/orchbridge'
    config_path=config_dir/'config.json'
    monkeypatch.setattr(m,'CONFIG_DIR',config_dir)
    monkeypatch.setattr(m,'CONFIG_PATH',config_path)
    monkeypatch.setattr(sys,'argv',['orch settings','model','commandcode','xiaomi/custom-model'])
    assert m.main()==0
    saved=json.loads(config_path.read_text())
    assert saved['models']['commandcode']=='xiaomi/custom-model'
