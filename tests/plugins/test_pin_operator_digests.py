"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from ruamel.yaml import YAML

import pytest
import responses

from atomic_reactor.constants import (PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST)
from atomic_reactor.util import ImageName
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       ReactorConfig,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_OVERRIDE_KWARGS)
from atomic_reactor.plugins.pre_pin_operator_digest import (PinOperatorDigestsPlugin,
                                                            PullspecReplacer)

from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource, StubConfig


yaml = YAML()


def mock_dockerfile(tmpdir, base='scratch', operator_bundle_label=True):
    dockerfile = (
        'FROM {base}\n'
        'LABEL com.redhat.delivery.operator.bundle={label_value}\n'
    ).format(base=base, label_value=operator_bundle_label)

    tmpdir.join('Dockerfile').write(dockerfile)


def make_reactor_config(operators_config):
    config = {'version': 1}
    if operators_config:
        config['operator_manifests'] = operators_config
    return ReactorConfig(config)


def make_user_config(operator_config):
    config = StubConfig()
    setattr(config, 'operator_manifests', operator_config)
    return config


def mock_workflow(tmpdir, orchestrator, user_config=None, site_config=None):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE,
        source={'provider': 'git', 'uri': 'asd'}
    )
    workflow.source = StubSource()
    workflow.source.config = make_user_config(user_config)
    workflow.builder = (
        StubInsideBuilder().for_workflow(workflow).set_df_path(str(tmpdir))
    )

    if orchestrator:
        workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]

        workflow.plugin_workspace[ReactorConfigPlugin.key] = {
            WORKSPACE_CONF_KEY: make_reactor_config(site_config or {})
        }

    return workflow


def mock_env(docker_tasker, tmpdir, orchestrator,
             user_config=None, site_config=None,
             df_base='scratch', df_operator_label=True,
             replacement_pullspecs=None):
    """
    Mock environment for test

    :param docker_tasker: conftest fixture
    :param tmpdir: pylint fixture,
    :param orchestrator: is the plugin running in orchestrator?
    :param user_config: container.yaml operator_manifest config
    :param site_config: reactor-config-map operator_manifests config
    :param df_base: base image in Dockerfile, non-scratch should fail
    :param df_operator_label: presence of operator manifest bundle label
    :param replacement_pullspecs: plugin argument from osbs-client

    :return: configured plugin runner
    """
    mock_dockerfile(tmpdir, df_base, df_operator_label)
    workflow = mock_workflow(tmpdir, orchestrator,
                             user_config=user_config, site_config=site_config)

    plugin_conf = [{'name': PinOperatorDigestsPlugin.key,
                    'args': {'replacement_pullspecs': replacement_pullspecs}}]
    runner = PreBuildPluginsRunner(docker_tasker, workflow, plugin_conf)

    return runner


def mock_operator_csv(tmpdir, filename, pullspecs):
    path = tmpdir.join(filename)
    data = {
        'kind': 'ClusterServiceVersion',
        'spec': {
            # It does not really matter where in the CSV these pullspecs go
            # as long as operator_util is known to work properly
            'relatedImages': [
                {'name': 'foo-{}'.format(i + 1), 'image': image}
                for i, image in enumerate(pullspecs)
            ]
        }
    }
    with open(str(path), 'w') as f:
        yaml.dump(data, f)
    return path


def mock_digest_query(pullspec, digest):
    i = ImageName.parse(pullspec)
    url = 'https://{}/v2/{}/{}/manifests/{}'.format(i.registry, i.namespace, i.repo, i.tag)
    headers = {
        'Content-Type': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
        'Docker-Content-Digest': digest
    }
    responses.add(responses.GET, url, headers=headers)


def get_build_kwarg(workflow, k, platform=None):
    """
    Get build-kwarg override
    """
    key = OrchestrateBuildPlugin.key

    workspace = workflow.plugin_workspace.get(key, {})
    override_kwargs = workspace.get(WORKSPACE_KEY_OVERRIDE_KWARGS, {})
    return override_kwargs.get(platform, {}).get(k)


def get_site_config(allowed_registries=None, registry_post_replace=None):
    registry_post_replace = registry_post_replace or {}
    return {
        'allowed_registries': allowed_registries,
        'registry_post_replace': [
            {'old': old, 'new': new} for old, new in registry_post_replace.items()
        ]
    }


def get_user_config(manifests_dir):
    return {
        'manifests_dir': manifests_dir
    }


class TestPinOperatorDigest(object):
    def _get_worker_arg(self, workflow):
        return get_build_kwarg(workflow, "operator_bundle_replacement_pullspecs")

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_run_only_for_operator_bundle_label(self, orchestrator,
                                                docker_tasker, tmpdir, caplog):
        runner = mock_env(docker_tasker, tmpdir,
                          orchestrator=orchestrator, df_operator_label=False)
        runner.run()
        assert "Not an operator manifest bundle build, skipping plugin" in caplog.text

    def test_missing_orchestrator_config(self, docker_tasker, tmpdir, caplog):
        runner = mock_env(docker_tasker, tmpdir, orchestrator=True)
        runner.run()

        msg = "operator_manifests configuration missing in reactor config map, aborting"
        assert msg in caplog.text
        assert "Looking for operator CSV files" not in caplog.text

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_missing_user_config(self, orchestrator, docker_tasker, tmpdir):
        # make sure operator run does not fail because of missing site config
        site_config = get_site_config()
        # make sure worker run is not skipped because of missing replacements
        replacement_pullspecs = {'a': 'b'}

        runner = mock_env(docker_tasker, tmpdir, orchestrator=orchestrator,
                          site_config=site_config, replacement_pullspecs=replacement_pullspecs)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "operator_manifests configuration missing in container.yaml"
        assert msg in str(exc_info.value)

    @pytest.mark.parametrize('replacements', [None, {}])
    def test_skip_worker_with_empty_replacements(self, replacements,
                                                 docker_tasker, tmpdir, caplog):
        runner = mock_env(docker_tasker, tmpdir, orchestrator=False,
                          replacement_pullspecs=replacements)
        runner.run()
        assert "No pullspecs need to be replaced" in caplog.text
        assert "Looking for operator CSV files" not in caplog.text

    @pytest.mark.parametrize('filepaths', [
        [],
        ['csv1.yaml', 'csv2.yaml']
    ])
    def test_orchestrator_no_pullspecs(self, filepaths, docker_tasker, tmpdir, caplog):
        files = [mock_operator_csv(tmpdir, path, []) for path in filepaths]

        user_config = get_user_config(manifests_dir=str(tmpdir))
        site_config = get_site_config()

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)
        runner.run()

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        assert "Looking for operator CSV files in {}".format(tmpdir) in caplog_text
        if files:
            assert "Found operator CSV files:" in caplog_text
            for f in files:
                assert str(f) in caplog_text
        else:
            assert "No operator CSV files found" in caplog_text
        assert "No pullspecs found" in caplog_text
        assert self._get_worker_arg(runner.workflow) is None

    def test_orchestrator_disallowed_registry(self, docker_tasker, tmpdir):
        # TODO: ImageName parses x/y as namespace/repo and not registry/repo - does it matter?
        pullspecs = ['allowed-registry/ns/foo:1', 'disallowed-registry/ns/bar:2']
        mock_operator_csv(tmpdir, 'csv.yaml', pullspecs)

        user_config = get_user_config(manifests_dir=str(tmpdir))
        site_config = get_site_config(allowed_registries=['allowed-registry'])

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "Registry not allowed: disallowed-registry (in disallowed-registry/ns/bar:2)"
        assert msg in str(exc_info.value)

    @responses.activate
    def test_orchestrator(self, docker_tasker, tmpdir, caplog):
        pullspecs = [
            'keep-registry/ns/foo',
            'replace-registry/ns/bar:1',
            'keep-registry/ns/spam@sha256:123456',
            'replace-registry/ns/eggs@sha256:654321',
        ]
        replacement_registries = {
            'replace-registry': 'new-registry'
        }

        mock_digest_query('keep-registry/ns/foo:latest', 'sha256:abcdef')
        mock_digest_query('replace-registry/ns/bar:1', 'sha256:fedcba')
        # there should be no queries for the pullspecs which already contain a digest

        f = mock_operator_csv(tmpdir, 'csv.yaml', pullspecs)
        pre_content = f.read()

        user_config = get_user_config(manifests_dir=str(tmpdir))
        site_config = get_site_config(registry_post_replace=replacement_registries)

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)
        runner.run()

        post_content = f.read()
        assert pre_content == post_content  # worker does the replacement, not orchestrator

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        # pullspecs are logged in alphabetical order, if tag is missing, :latest is added
        pullspecs_log = (
            'Found pullspecs:\n'
            'keep-registry/ns/foo:latest\n'
            'keep-registry/ns/spam@sha256:123456\n'
            'replace-registry/ns/bar:1\n'
            'replace-registry/ns/eggs@sha256:654321'
        )
        assert pullspecs_log in caplog_text

        assert "Computing replacement pullspecs" in caplog_text

        # replacements are logged in alphabetical order (ordered by the original pullspec)
        replacements_log = (
            'To be replaced:\n'
            'keep-registry/ns/foo:latest -> keep-registry/ns/foo@sha256:abcdef\n'
            'keep-registry/ns/spam@sha256:123456 - no change\n'
            'replace-registry/ns/bar:1 -> new-registry/ns/bar@sha256:fedcba\n'
            'replace-registry/ns/eggs@sha256:654321 -> new-registry/ns/eggs@sha256:654321'
        )
        assert replacements_log in caplog_text

        replacement_pullspecs = {
            'keep-registry/ns/foo:latest': 'keep-registry/ns/foo@sha256:abcdef',
            'replace-registry/ns/bar:1': 'new-registry/ns/bar@sha256:fedcba',
            'replace-registry/ns/eggs@sha256:654321': 'new-registry/ns/eggs@sha256:654321',
        }
        assert self._get_worker_arg(runner.workflow) == replacement_pullspecs

    def test_worker(self, docker_tasker, tmpdir, caplog):
        pullspecs = [
            'keep-registry/ns/foo',
            'replace-registry/ns/bar:1',
            'keep-registry/ns/spam@sha256:123456',
            'replace-registry/ns/eggs@sha256:654321',
        ]
        replacement_pullspecs = {
            'keep-registry/ns/foo:latest': 'keep-registry/ns/foo@sha256:abcdef',
            'replace-registry/ns/bar:1': 'new-registry/ns/bar@sha256:fedcba',
            'replace-registry/ns/eggs@sha256:654321': 'new-registry/ns/eggs@sha256:654321',
        }
        replaced_pullspecs = [
            'keep-registry/ns/foo@sha256:abcdef',
            'new-registry/ns/bar@sha256:fedcba',
            'keep-registry/ns/spam@sha256:123456',
            'new-registry/ns/eggs@sha256:654321',
        ]

        manifests_dir = tmpdir.mkdir('manifests')
        gets_replaced = mock_operator_csv(manifests_dir, 'csv1.yaml', pullspecs)
        # this a reference file, make sure it does not get touched by putting it in parent dir
        reference = mock_operator_csv(tmpdir, 'csv2.yaml', replaced_pullspecs)

        user_config = get_user_config(manifests_dir=str(manifests_dir))

        runner = mock_env(docker_tasker, tmpdir, orchestrator=False,
                          user_config=user_config, replacement_pullspecs=replacement_pullspecs)
        runner.run()

        assert gets_replaced.read() == reference.read()

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        assert 'Found operator CSV files:\n{}'.format(gets_replaced) in caplog_text
        assert str(reference) not in caplog_text

        assert 'Replacing pullspecs in {}'.format(gets_replaced) in caplog_text
        assert 'Replacing pullspecs in {}'.format(reference) not in caplog_text


class TestPullspecReplacer(object):
    @pytest.mark.parametrize('allowed_registries, image, allowed', [
        (None, 'registry/ns/foo', True),
        (['registry'], 'registry/ns/foo', True),
        ([], 'registry/ns/foo', False),  # not actually allowed in schema, but sensible
        (['other-registry'], 'registry/ns/foo', False),
    ])
    def test_registry_is_allowed(self, allowed_registries, image, allowed):
        site_config = get_site_config(allowed_registries=allowed_registries)
        replacer = PullspecReplacer(user_config={}, site_config=site_config)
        image = ImageName.parse(image)
        assert replacer.registry_is_allowed(image) == allowed

    @pytest.mark.parametrize('image, should_query, digest', [
        ('registry/ns/foo', True, 'sha256:123456'),
        ('registry/ns/bar@sha256:654321', False, 'sha256:654321'),
    ])
    @responses.activate
    def test_pin_digest(self, image, should_query, digest):
        image = ImageName.parse(image)
        if should_query:
            mock_digest_query(image, digest)

        site_config = get_site_config()
        replacer = PullspecReplacer(user_config={}, site_config=site_config)
        replaced = replacer.pin_digest(image)

        assert replaced.registry == image.registry
        assert replaced.namespace == image.namespace
        assert replaced.repo == image.repo
        assert replaced.tag == digest

    @pytest.mark.parametrize('image, replacement_registries, replaced', [
        ('old-registry/ns/foo', {'old-registry': 'new-registry'}, 'new-registry/ns/foo'),
        ('registry/ns/foo', {}, 'registry/ns/foo'),
    ])
    def test_replace_registry(self, image, replacement_registries, replaced):
        image = ImageName.parse(image)
        replaced = ImageName.parse(replaced)

        site_config = get_site_config(registry_post_replace=replacement_registries)
        replacer = PullspecReplacer(user_config={}, site_config=site_config)

        assert replacer.replace_registry(image) == replaced
