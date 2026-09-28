from pathlib import Path

def test_github_open_source_scaffolding_present():
    r=Path(__file__).resolve().parents[1]
    for p in ['README.md','LICENSE','CONTRIBUTING.md','SECURITY.md','.github/workflows/ci.yml','.github/workflows/release-please.yml','.github/workflows/release-assets.yml']:
        assert (r/p).exists(), p
    for p in ['workflows/implementation.yaml','workflows/review.yaml','workflows/research.yaml']:
        text=(r/p).read_text()
        assert 'execution: reference-only' in text
        assert 'intent:' in text
