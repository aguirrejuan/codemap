import shutil
import subprocess
from pathlib import Path

import pytest

from codemap import cli, diff, parse, render

FIXTURE = Path(__file__).parent / 'fixtures' / 'fraudsvc'


@pytest.fixture(scope='module')
def ix():
    return parse.build(FIXTURE)


def edge(ix, src, dst, kind='calls'):
    return ix.edges.get((src, dst, kind), 0)


def test_kinds_form_a_tree(ix):
    kinds = {n['id']: n['kind'] for n in ix.nodes.values()}
    assert kinds['api'] == 'package'
    assert kinds['api.routes'] == 'module'
    assert kinds['rules.engine.Rule'] == 'class'
    assert kinds['rules.engine.Rule.match'] == 'method'
    assert kinds['features.compute.velocity'] == 'function'
    assert ix.nodes['rules.engine.Rule.match']['parent'] == 'rules.engine.Rule'
    assert ix.nodes['features.store']['parent'] == 'features'


def test_calls_resolve_through_imports(ix):
    assert edge(ix, 'api.routes.score_endpoint', 'rules.engine.evaluate')
    # `from features import get_features` is a re-export in features/__init__.py
    assert edge(ix, 'api.routes.score_endpoint', 'features.store.get_features')
    # `store.get_features(...)` through a module alias
    assert edge(ix, 'rules.engine.evaluate', 'features.store.get_features')
    # relative import inside the features package
    assert edge(ix, 'features.store.get_features', 'features.compute.velocity')


def test_calls_resolve_through_types(ix):
    # `fs = FeatureStore(); fs.load(...)`
    assert edge(ix, 'features.store.get_features', 'features.store.FeatureStore.load')
    # `rule = VelocityRule(...); rule.match(...)` finds the inherited method
    assert edge(ix, 'rules.engine.evaluate', 'rules.engine.Rule.match')
    assert edge(ix, 'rules.engine.evaluate', 'rules.engine.VelocityRule')
    # `self._over(...)`
    assert edge(ix, 'rules.engine.Rule.match', 'rules.engine.Rule._over')


def test_function_passed_as_value_is_a_reference(ix):
    assert edge(ix, 'dags.daily_features.build_dag', 'features.compute.compute_all', 'references')


def test_imports_link_modules(ix):
    assert edge(ix, 'dags.daily_features', 'features.compute', 'imports')
    assert edge(ix, 'features.store', 'features.compute', 'imports')


def test_third_party_calls_count_as_unresolved(ix):
    assert ix.unresolved['dags.daily_features'] >= 1  # PythonOperator


@pytest.fixture
def repo(tmp_path):
    dst = tmp_path / 'fraudsvc'
    shutil.copytree(FIXTURE, dst)
    git = lambda *a: subprocess.run(['git', *a], cwd=dst, check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'chore: seed fixture')
    return dst


def test_diff_marks_the_innermost_symbol_and_its_callers(repo):
    path = repo / 'features' / 'compute.py'
    path.write_text(path.read_text().replace('return len(raw)', 'return len(raw) * 2'))
    ix = parse.build(repo)
    changed = diff.changed_symbols(ix, diff.changed_ranges(repo, 'main'))
    assert changed == {'features.compute.velocity'}
    hops = diff.blast_radius(ix, changed, depth=2)
    assert hops['features.store.get_features'] == 1
    assert hops['features.compute.compute_all'] == 1
    assert hops['api.routes.score_endpoint'] == 2
    assert hops['rules.engine.evaluate'] == 2
    assert hops['dags.daily_features.build_dag'] == 2
    assert 'rules.engine.Rule.match' not in hops


def test_module_level_change_reaches_importers(repo):
    path = repo / 'features' / 'compute.py'
    path.write_text('THRESHOLD = 3\n\n\n' + path.read_text())
    ix = parse.build(repo)
    changed = diff.changed_symbols(ix, diff.changed_ranges(repo, 'main'))
    assert changed == {'features.compute'}
    hops = diff.blast_radius(ix, changed, depth=1)
    assert hops['features.store'] == 1
    assert hops['dags.daily_features'] == 1


def test_mermaid_nests_modules_in_packages(repo):
    path = repo / 'features' / 'compute.py'
    path.write_text(path.read_text().replace('return len(raw)', 'return len(raw) * 2'))
    ix = parse.build(repo)
    hops = diff.blast_radius(ix, diff.changed_symbols(ix, diff.changed_ranges(repo, 'main')), 2)
    out = render.mermaid(ix, hops, 'summary')
    assert 'subgraph n_features["features/"]' in out
    assert 'compute.py<br/><small>changed</small>' in out
    assert 'n_features_store -->|2| n_features_compute' in out


def test_page_carries_source_and_changed_lines(repo, tmp_path):
    path = repo / 'features' / 'compute.py'
    path.write_text(path.read_text().replace('return len(raw)', 'return len(raw) * 2'))
    out = tmp_path / 'map.html'
    cli.main([str(repo), '--diff', 'main', '--out', str(out)])
    page = out.read_text()
    assert '/*DATA*/null' not in page
    assert 'return len(raw) * 2' in page
    assert '"features/compute.py":[[2,2]]' in page


def test_source_cannot_close_the_script_tag(tmp_path):
    out = tmp_path / 'map.html'
    render.html({'sources': {'a.py': 's = "</script><!--"'}}, str(out))
    script = out.read_text().split('const D = ', 1)[1]
    assert '</script><!--' not in script.split(';\n', 1)[0]


def test_unresolved_imports_become_external_packages(ix):
    ext = {n['label']: n for n in ix.nodes.values() if n['kind'] == 'external'}
    assert 'airflow' in ext and not ext['airflow']['stdlib']
    assert edge(ix, 'dags.daily_features', 'ext:airflow', 'imports')
    # relative imports and the repo's own packages never count as external
    assert 'features' not in ext and 'compute' not in ext


def write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def test_cycles_follow_imports_that_run_at_load_time(tmp_path):
    write(tmp_path, {
        'app/__init__.py': '',
        'app/a.py': 'from app import b\n',
        'app/b.py': 'from . import c\nimport json\n',
        'app/c.py': 'import app.a\n',
        # each of these closes a loop only lazily, so neither is a cycle
        'app/d.py': 'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app import e\n',
        'app/e.py': 'def f():\n    from app import d\n',
    })
    ix = parse.build(tmp_path)
    assert ix.cycles == [{'modules': ['app.a', 'app.b', 'app.c'],
                          'edges': [['app.a', 'app.b'], ['app.b', 'app.c'], ['app.c', 'app.a']]}]
    assert edge(ix, 'app.e', 'app.d', 'imports')  # still drawn, just not a cycle
    assert ix.nodes['ext:json']['stdlib']
    base, _ = cli.summarise(ix, None)
    assert '1 import cycle, the largest through 3 modules' in base
