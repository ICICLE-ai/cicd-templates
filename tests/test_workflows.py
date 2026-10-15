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
                'tests/fixtures/node-frontend/build.js': True, 'tests/test_deploy_tapis.py': True,
                'scripts/deploy_tapis.py': True, 'scripts/requirements.txt': True,
                'scripts/pod_config.py': True, 'scripts/provision_tapis.py': True,
                'scripts/pod_lifecycle.py': True, 'scripts/tapis_support.py': True,
                'schema/pod-provisioning-v1.schema.json': True,
                'provisioning-example.yaml': True, 'provisioning/myservice.yaml': True,
                'icicle-service.example.yaml': True, 'VERSION': True, 'deploy.yaml': True,
                '.github/workflows/deploy-service.yaml': True, '.github/dependabot.yml': True,
                '.github/workflows/provision-pods.yaml': True, '.github/workflows/pod-lifecycle.yaml': True,
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

    def test_pod_operations_use_reviewed_catalog_and_shared_tenant_lock(self):
        groups = set()
        for filename in ('provision-pods.yaml', 'pod-lifecycle.yaml'):
            config = workflow(filename)
            with self.subTest(workflow=filename):
                self.assertEqual(set(config['on']), {'workflow_dispatch'})
                self.assertEqual(config['permissions'], {'contents': 'read'})
                self.assertEqual(len(config['jobs']), 1)
                job = next(iter(config['jobs'].values()))
                self.assertEqual(job['if'], "github.event_name == 'workflow_dispatch' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
                self.assertEqual(job['environment'], 'tapis')
                self.assertEqual(job['concurrency']['cancel-in-progress'], 'false')
                groups.add(job['concurrency']['group'])
                checkouts = [step for step in job['steps'] if step.get('uses', '').startswith('actions/checkout@')]
                self.assertEqual(len(checkouts), 1)
                self.assertEqual(checkouts[0]['with'], {'ref': '${{ github.sha }}', 'persist-credentials': 'false'})
                validation = next(step for step in job['steps'] if step.get('name') == 'Validate catalog configuration')
                self.assertIn('--catalog-root provisioning', validation['run'])
                self.assertNotIn('env', config)
                self.assertNotIn('env', job)
                for step in job['steps'][:-1]:
                    self.assertNotIn('secrets.', repr(step))
                    self.assertNotIn('secrets[', repr(step))
                credential_step = job['steps'][-1]
                self.assertEqual(credential_step['env']['TAPIS_TOKEN'], '${{ secrets.TAPIS_TOKEN }}')
                self.assertEqual(credential_step['env']['SLACK_PROVISIONING_WEBHOOK_URL'], '${{ secrets.SLACK_PROVISIONING_WEBHOOK_URL }}')
                self.assertTrue(any('pip install' in step.get('run', '') for step in job['steps'][:-1]))
                for step in job['steps']:
                    # User input is data in the environment, never shell source.
                    self.assertNotIn('${{', step.get('run', ''))
        self.assertEqual(groups, {"tapis-operations-${{ vars.TAPIS_TENANT_ID || 'icicleai' }}"})
        provision = workflow('provision-pods.yaml')
        self.assertEqual(provision['on']['workflow_dispatch']['inputs']['apply']['default'], 'false')
        self.assertEqual(provision['on']['workflow_dispatch']['inputs']['apply']['type'], 'boolean')
        self.assertEqual(set(provision['on']['workflow_dispatch']['inputs']), {'pod-name', 'apply'})
        lifecycle_inputs = workflow('pod-lifecycle.yaml')['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(lifecycle_inputs), {'pod-name', 'action', 'build-sha'})
        self.assertEqual(lifecycle_inputs['action']['options'], ['start', 'stop', 'restart'])

    def test_pod_environment_secret_uses_validated_catalog_output_only_for_provisioning(self):
        steps = workflow('provision-pods.yaml')['jobs']['provision']['steps']
        validation = next(step for step in steps if step.get('name') == 'Validate catalog configuration')
        self.assertEqual(validation['id'], 'config')
        self.assertIn('--github-output "$GITHUB_OUTPUT"', validation['run'])
        self.assertEqual(steps[-1]['env']['POD_ENV_JSON'],
                         "${{ secrets[format('POD_ENV_{0}', steps.config.outputs.pod_name)] }}")
        self.assertNotIn('POD_ENV_JSON', steps[-1]['run'])
        lifecycle = workflow('pod-lifecycle.yaml')['jobs']['lifecycle']
        self.assertNotIn('POD_ENV_', repr(lifecycle))

    def test_pod_dispatch_rejects_paths_and_shell_input_without_evaluating_it(self):
        for filename in ('provision-pods.yaml', 'pod-lifecycle.yaml'):
            job = next(iter(workflow(filename)['jobs'].values()))
            script = job['steps'][0]['run']
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'outputs'
                marker = Path(directory) / 'executed'
                env = {**os.environ, 'GITHUB_OUTPUT': str(output), 'APPLY': 'false',
                       'POD_ACTION': 'start', 'EXPECTED_BUILD_SHA': ''}
                cases = [('test', True), ('a' * 64, True), ('abc', False), ('a' * 65, False),
                         ('1test', False), ('Test', False), ('my-service', False),
                         ('my_service', False), ('my service', False), ('../test', False),
                         ('test\nconfig=other.yaml', False), (f'$(touch {marker})', False)]
                for name, expected in cases:
                    with self.subTest(workflow=filename, name=name):
                        output.unlink(missing_ok=True)
                        result = subprocess.run(['bash', '-eu', '-c', script],
                                                env={**env, 'POD_NAME': name}, capture_output=True, text=True)
                        self.assertEqual(result.returncode == 0, expected)
                        self.assertFalse(marker.exists())
                        if expected:
                            self.assertEqual(output.read_text(), f'config=provisioning/{name}.yaml\n')
                        else:
                            self.assertFalse(output.exists())
                env['POD_NAME'] = 'test'
                invalid = [{'APPLY': 'yes'}] if filename == 'provision-pods.yaml' else [
                    {'POD_ACTION': 'delete'}, {'POD_ACTION': f'start; touch {marker}'},
                    {'EXPECTED_BUILD_SHA': 'abc123'}, {'EXPECTED_BUILD_SHA': 'A' * 40},
                    {'EXPECTED_BUILD_SHA': f'$(touch {marker})'},
                ]
                for values in invalid:
                    with self.subTest(workflow=filename, values=values):
                        result = subprocess.run(['bash', '-eu', '-c', script],
                                                env={**env, **values}, capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(marker.exists())

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
