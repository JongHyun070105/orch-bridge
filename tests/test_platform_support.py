import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    p=ROOT/'app'/f'{name}.py'; spec=importlib.util.spec_from_file_location(name,p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_linux_clipboard_backends():
    m=load('platform_support')
    with patch.object(m,'system_name',return_value='Linux'), patch.object(m.shutil,'which',side_effect=lambda x:'/usr/bin/wl-copy' if x=='wl-copy' else None):
        argv,backend=m.clipboard_backend(); assert backend=='wl-copy'; assert argv==['wl-copy']

def test_linux_notification_backend():
    m=load('desktop_notify')
    with patch.object(m.platform,'system',return_value='Linux'), patch.object(m.shutil,'which',side_effect=lambda x:'/usr/bin/notify-send' if x=='notify-send' else None):
        assert m.backend_status()=='notify-send'

def test_shell_uses_environment_when_present(tmp_path):
    m=load('platform_support')
    shell=tmp_path/'bash'; shell.write_text(''); shell.chmod(0o755)
    with patch.dict(m.os.environ,{'SHELL':str(shell)},clear=False):
        assert str(shell) in m.login_shell_command()
