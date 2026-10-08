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
    path.write_text('import json\n\n\n' + path.read_text())
    ix = parse.build(repo)
    changed = diff.changed_symbols(ix, diff.changed_ranges(repo, 'main'))
    assert changed == {'features.compute'}
    hops = diff.blast_radius(ix, changed, depth=1)
    assert hops['features.store'] == 1
    assert hops['dags.daily_features'] == 1


def test_constant_change_reaches_its_readers(tmp_path):
    write(tmp_path, {
        'app/__init__.py': '',
        'app/conf.py': (
            'FEED = "s3://old"\n'
            'GRAMMAR = {"feed": FEED}\n'
            'log = None\n'
            'def check(cfg):\n    return cfg == GRAMMAR\n'
            'def load():\n    return check({})\n'
        ),
        'app/job.py': 'from app.conf import FEED\nimport app.conf as conf\n'
                      'def run():\n    return FEED\ndef other():\n    return conf.GRAMMAR\n'
                      '@mark(1)\ndef decorated():\n    return 1\n',
    })
    git = lambda *a: subprocess.run(['git', *a], cwd=tmp_path, check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'chore: seed')
    job = tmp_path / 'app' / 'job.py'
    job.write_text(job.read_text().replace('@mark(1)', '@mark(2)'))
    conf = tmp_path / 'app' / 'conf.py'
    conf.write_text('# the feed moved\n' + conf.read_text().replace('s3://old', 's3://new'))
    ix = parse.build(tmp_path)
    assert ix.nodes['app.conf.FEED']['kind'] == 'constant'
    assert 'app.conf.log' not in ix.nodes  # lower case is a variable, not a constant
    assert edge(ix, 'app.conf.GRAMMAR', 'app.conf.FEED', 'reads')
    assert edge(ix, 'app.conf.check', 'app.conf.GRAMMAR', 'reads')
    assert edge(ix, 'app.job.other', 'app.conf.GRAMMAR', 'reads')
    assert ix.nodes['app.conf.check']['fan_out'] == 0  # a read is not a call
    changed = diff.changed_symbols(ix, diff.changed_ranges(tmp_path, 'main'))
    # the new comment marks nothing; the decorator belongs to its function
    assert changed == {'app.conf.FEED', 'app.job.decorated'}
    changed.discard('app.job.decorated')
    hops = diff.blast_radius(ix, changed, depth=3)
    assert hops == {'app.conf.FEED': 0, 'app.job.run': 1, 'app.conf.GRAMMAR': 1,
                    'app.conf.check': 2, 'app.job.other': 2, 'app.conf.load': 3}


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


def test_fan_counts_and_uncalled_functions(tmp_path):
    write(tmp_path, {
        'app/__init__.py': '',
        'app/core.py': (
            'import ast\n'
            'def used():\n    return helper() + helper()\n'
            'def helper():\n    return 1\n'
            'def orphan():\n    return used()\n'
            'def __getattr__(name):\n    return name\n'
            '@app.route("/")\n'
            'def view():\n    return 1\n'
            'def HOOK():\n    return 1\n'
            '@staticmethod\n'
            'def plain():\n    return 1\n'
            'class Base:\n'
            '    def run(self):\n        return 1\n'
            '    def lonely(self):\n        return 1\n'
            'class Child(Base):\n'
            '    def run(self):\n        return 2\n'
            'class Visitor(ast.NodeVisitor):\n'
            '    def visit_Name(self, node):\n        return node\n'
            'def poke(obj):\n    obj.poked()\n'
            'class Thing:\n'
            '    def poked(self):\n        return 1\n'
        ),
        'app/test_core.py': 'def test_it():\n    pass\n',
    })
    ix = parse.build(tmp_path)
    n = ix.nodes
    assert (n['app.core.helper']['fan_in'], n['app.core.used']['fan_out']) == (1, 1)
    assert n['app.core.used']['fan_in'] == 1
    uncalled = {k for k, v in n.items() if v.get('uncalled')}
    # Base.run and Child.run dispatch to each other, visit_Name is a hook of an outside
    # base, and `obj.poked()` on an unknown object may reach Thing.poked.
    assert uncalled == {'app.core.orphan', 'app.core.plain', 'app.core.Base.lonely', 'app.core.poke'}
    base, _ = cli.summarise(ix, None)
    assert '4 functions with no callers found' in base


def test_head_maps_a_commit_and_leaves_the_working_tree_alone(repo, tmp_path):
    path = repo / 'features' / 'compute.py'
    path.write_text(path.read_text().replace('return len(raw)', 'return len(raw) * 2'))
    git = lambda *a: subprocess.run(['git', *a], cwd=repo, check=True, capture_output=True)
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'feat: double it')
    path.write_text(path.read_text().replace('* 2', '* 3'))  # uncommitted, so ignored
    out = tmp_path / 'map.html'
    cli.main([str(repo), '--diff', 'main~1', '--head', 'main', '--out', str(out)])
    page = out.read_text()
    assert 'return len(raw) * 2' in page and '* 3' not in page
    assert '"repo":"fraudsvc"' in page
    worktrees = subprocess.run(['git', 'worktree', 'list'], cwd=repo, capture_output=True, text=True).stdout
    assert len(worktrees.splitlines()) == 1


def test_pr_links_name_the_repo_and_number():
    m = diff.PR_URL.match('https://github.com/acme/billing/pull/30/files')
    assert m.groups() == ('acme', 'billing', '30')
    assert not diff.PR_URL.match('/Users/me/code/repo')
