"""Exercise workflow change selection and gate/credential decisions offline."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


class WorkflowTests(unittest.TestCase):
    def test_change_selection_covers_runtime_inputs_but_skips_docs_and_renderer(self):
        script = workflow('test-templates.yaml')['jobs']['changes']['steps'][1]['run']
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', '-c', 'commit.gpgsign=false', *args], cwd=directory, stderr=subprocess.DEVNULL).decode().strip()
            git('init')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            cases = {
                'README.md': False, 'node-frontend/README.md': False,
                'scripts/render_wiki.py': False, 'tests/test_render_wiki.py': False,
                'python/Dockerfile': True, 'node-backend/Dockerfile': True,
                'node-frontend/entrypoint.sh': True, 'schema/icicle-service.schema.json': True,
                'schema/icicle-service-v3.schema.json': True, 'icicle-service-v3-example.yaml': True,
                'tests/fixtures/node-frontend/build.js': True, 'tests/test_deploy_tapis.py': True,
                'scripts/deploy_tapis.py': True, 'scripts/requirements.txt': True,
                'icicle-service.example.yaml': True, 'VERSION': True, 'deploy.yaml': True,
                '.github/workflows/deploy-service.yaml': True, '.github/dependabot.yml': True,
            }
            for path, expected in cases.items():
                with self.subTest(path=path):
                    git('reset', '--hard', base)
                    file = Path(directory) / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text('changed\n')
                    git('add', path)
                    git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'change')
                    output = Path(directory) / 'output'
                    output.unlink(missing_ok=True)
                    env = {**os.environ, 'EVENT_NAME': 'push', 'BASE_SHA': base, 'GITHUB_OUTPUT': str(output)}
                    subprocess.run(['bash', '-eu', '-c', script], cwd=directory, env=env, check=True)
                    self.assertEqual(output.read_text().strip(), f'code={str(expected).lower()}')
            for event, base_sha in [('workflow_dispatch', base), ('push', '0' * 40), ('push', 'f' * 40)]:
                with self.subTest(event=event, base_sha=base_sha):
                    output.unlink(missing_ok=True)
                    env.update(EVENT_NAME=event, BASE_SHA=base_sha)
                    subprocess.run(['bash', '-eu', '-c', script], cwd=directory, env=env, check=True)
                    self.assertEqual(output.read_text().strip(), 'code=true')

    def test_required_gate_accepts_only_successful_selected_checks(self):
        config = workflow('test-templates.yaml')
        self.assertEqual(set(config['on']), {'push', 'pull_request', 'workflow_dispatch'})
        self.assertEqual(config['on']['push'], {'branches': ['main']})
        self.assertFalse(config['on']['pull_request'])
        self.assertFalse(config['on']['workflow_dispatch'])
        gate = config['jobs']['checks']
        self.assertEqual(gate['name'], 'Template checks')
        self.assertEqual(gate['if'], 'always()')
        script = gate['steps'][0]['run']
        good = {'CHANGES_RESULT': 'success', 'DOCS_RESULT': 'success', 'CODE_CHANGED': 'true',
                'RUNTIME_RESULT': 'success', 'IMAGES_RESULT': 'success'}
        cases = [(good, True), ({**good, 'CODE_CHANGED': 'false', 'RUNTIME_RESULT': 'skipped', 'IMAGES_RESULT': 'skipped'}, True)]
        for key in ['CHANGES_RESULT', 'DOCS_RESULT', 'RUNTIME_RESULT', 'IMAGES_RESULT']:
            for result in ['failure', 'cancelled', 'skipped']:
                cases.append(({**good, key: result}, False))
        cases.append(({**good, 'CODE_CHANGED': ''}, False))
        for values, expected in cases:
            with self.subTest(values=values):
                result = subprocess.run(['bash', '-eu', '-c', script], env={**os.environ, **values})
                self.assertEqual(result.returncode == 0, expected)

    def test_registry_explicit_credentials_must_be_a_pair(self):
        steps = workflow('deploy-service.yaml')['jobs']['deploy']['steps']
        script = next(step['run'] for step in steps if step.get('name') == 'Validate registry credentials')
        for username, password, expected in [('', '', True), ('user', 'token', True), ('user', '', False), ('', 'token', False)]:
            with self.subTest(username_present=bool(username), password_present=bool(password)):
                result = subprocess.run(['bash', '-eu', '-c', script], env={**os.environ, 'REGISTRY_USERNAME': username,
                                         'REGISTRY_PASSWORD': password}, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, expected)
                self.assertNotIn('token\n', result.stderr)

    def test_official_actions_are_sha_pinned_and_only_wiki_checkout_persists_credentials(self):
        for path in (ROOT / '.github/workflows').glob('*.yaml'):
            config = workflow(path.name)
            for job in config['jobs'].values():
                for step in job.get('steps', []):
                    action = step.get('uses', '')
                    if action.startswith(('actions/', 'docker/')):
                        with self.subTest(workflow=path.name, action=action):
                            self.assertRegex(action, r'@[0-9a-f]{40}$')
                    if action.startswith('actions/checkout@'):
                        inputs = step.get('with', {})
                        if inputs.get('path') == 'wiki':
                            self.assertEqual(inputs['ref'], 'master')
                        else:
                            self.assertEqual(inputs.get('persist-credentials'), 'false')


if __name__ == '__main__':
    unittest.main()
