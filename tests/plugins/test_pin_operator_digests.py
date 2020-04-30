"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import copy
import io
import os

import jsonschema
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

import pytest
import responses
from flexmock import flexmock

import atomic_reactor.util
from atomic_reactor.constants import (PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      INSPECT_CONFIG)
from atomic_reactor.util import ImageName
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       ReactorConfig,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_OVERRIDE_KWARGS)
from atomic_reactor.plugins.pre_pin_operator_digest import (PinOperatorDigestsPlugin,
                                                            PullspecReplacer)

from tests.stubs import StubInsideBuilder, StubSource, StubConfig


PKG_LABEL = 'com.redhat.component'


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


def mock_workflow(tmpdir, workflow, orchestrator, user_config=None, site_config=None):
    workflow.source = StubSource()
    workflow.source.path = str(tmpdir)
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


def mock_env(docker_tasker, workflow, tmpdir, orchestrator,
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
    workflow = mock_workflow(tmpdir, workflow, orchestrator,
                             user_config=user_config, site_config=site_config)

    plugin_conf = [{'name': PinOperatorDigestsPlugin.key,
                    'args': {'replacement_pullspecs': replacement_pullspecs}}]
    runner = PreBuildPluginsRunner(docker_tasker, workflow, plugin_conf)

    return runner


def mock_operator_csv(tmpdir, filename, pullspecs, for_ocp_44=False,
                      with_related_images=False, with_related_image_envs=False):
    path = tmpdir.join(filename)
    containers = [
        # utils.operator adds related images as ordered dicts
        # ("name" first, "image" second) - make sure order matches here
        CommentedMap([('name', 'foo-{}'.format(i + 1)), ('image', image)])
        for i, image in enumerate(pullspecs)
    ]
    # Add a random RELATED_IMAGE env var, only for testing
    # relatedImages vs. RELATED_IMAGE_* conflicts
    if with_related_image_envs:
        containers[0]['env'] = [{'name': 'RELATED_IMAGE_XYZ', 'value': 'xyz'}]
    data = {
        'kind': 'ClusterServiceVersion',
        'spec': {
            'relatedImages': [],
            # It does not really matter where in the CSV these pullspecs go
            # as long as utils.operator is known to work properly, just do not
            # put them in relatedImages because those get special handling
            'install': {
                'spec': {
                    'deployments': [
                        {
                            'spec': {
                                'template': {
                                    'spec': {
                                        'containers': containers
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    # To test OCP 4.4 workaround, also add pullspecs under a random key which
    # is not normally considered a pullspec location
    if for_ocp_44:
        data['foo'] = pullspecs
    # To mock what the file should look like after relatedImages are updated,
    # add pullspecs also under .spec.relatedImages
    if with_related_images:
        # deepcopy the containers list to prevent ruamel.yaml from being overly
        # clever and using YAML anchors to refer to the same objects
        data['spec']['relatedImages'] = copy.deepcopy(containers)

    with open(str(path), 'w') as f:
        yaml.dump(data, f)
    return path


def mock_package_mapping_files(repo_replacements):
    repo_replacements = repo_replacements or {}

    # create unique url for each registry, mock responses, update mapping to point to urls
    for registry, mapping in repo_replacements.items():
        url = 'https://somewhere.net/mapping-{}.yaml'.format(registry)

        # ruamel.yaml does not support dumping to str, use an io stream
        # on python2, it also does not support writing to a StringIO stream, use BytesIO
        f = io.BytesIO()
        yaml.dump(mapping, f)
        f.seek(0)

        responses.add(responses.GET, url, body=f.read().decode('utf-8'))
        repo_replacements[registry] = url

    return repo_replacements


def mock_digest_query(pullspec, digest):
    i = ImageName.parse(pullspec)
    url = 'https://{}/v2/{}/{}/manifests/{}'.format(i.registry, i.namespace, i.repo, i.tag)
    headers = {
        'Content-Type': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
        'Docker-Content-Digest': digest
    }
    responses.add(responses.GET, url, headers=headers)


def mock_inspect_query(pullspec, labels, times=1):
    image = ImageName.parse(pullspec)
    inspect = {
        INSPECT_CONFIG: {
            'Labels': labels
        }
    }
    (flexmock(atomic_reactor.util)
        .should_receive('get_inspect_for_image')
        .with_args(image, image.registry)
        .and_return(inspect)
        .times(times))


def get_build_kwarg(workflow, k, platform=None):
    """
    Get build-kwarg override
    """
    key = OrchestrateBuildPlugin.key

    workspace = workflow.plugin_workspace.get(key, {})
    override_kwargs = workspace.get(WORKSPACE_KEY_OVERRIDE_KWARGS, {})
    return override_kwargs.get(platform, {}).get(k)


def get_site_config(allowed_registries=None, registry_post_replace=None, repo_replacements=None):
    registry_post_replace = registry_post_replace or {}
    repo_replacements = repo_replacements or {}
    return {
        'allowed_registries': allowed_registries,
        'registry_post_replace': [
            {'old': old, 'new': new} for old, new in registry_post_replace.items()
        ],
        'repo_replacements': [
            {'registry': registry, 'package_mappings_url': path}
            for registry, path in repo_replacements.items()
        ]
    }


def get_user_config(manifests_dir, repo_replacements=None, enable_digest_pinning=True,
                    enable_repo_replacements=True, enable_registry_replacements=True):
    repo_replacements = repo_replacements or {}
    return {
        'manifests_dir': manifests_dir,
        'repo_replacements': [
            {'registry': registry, 'package_mappings': mapping}
            for registry, mapping in repo_replacements.items()
        ],
        'enable_digest_pinning': enable_digest_pinning,
        'enable_repo_replacements': enable_repo_replacements,
        'enable_registry_replacements': enable_registry_replacements,
    }


class TestPinOperatorDigest(object):
    def _get_worker_arg(self, workflow):
        return get_build_kwarg(workflow, "operator_bundle_replacement_pullspecs")

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_run_only_for_operator_bundle_label(self, orchestrator,
                                                docker_tasker, workflow, tmpdir, caplog):
        runner = mock_env(docker_tasker, workflow, tmpdir,
                          orchestrator=orchestrator, df_operator_label=False)
        runner.run()
        assert "Not an operator manifest bundle build, skipping plugin" in caplog.text

    def test_missing_orchestrator_config(self, docker_tasker, workflow, tmpdir, caplog):
        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=True)
        runner.run()

        msg = "operator_manifests configuration missing in reactor config map, aborting"
        assert msg in caplog.text
        assert "Looking for operator CSV files" not in caplog.text

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_missing_user_config(self, orchestrator, docker_tasker, workflow, tmpdir):
        # make sure operator run does not fail because of missing site config
        site_config = get_site_config()
        # make sure worker run is not skipped because of missing replacements
        replacement_pullspecs = {'a': 'b'}

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=orchestrator,
                          site_config=site_config, replacement_pullspecs=replacement_pullspecs)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "operator_manifests configuration missing in container.yaml"
        assert msg in str(exc_info.value)

    @pytest.mark.parametrize('orchestrator', [True, False])
    @pytest.mark.parametrize('manifests_dir, symlinks', [
        ('/foo/bar', None),
        ('../foo', None),
        ('foo/../../bar', None),
        ('foo', {'foo': '../..'}),
    ])
    def test_manifests_dir_not_subdir_of_repo(self, manifests_dir, symlinks,
                                              orchestrator, docker_tasker, workflow, tmpdir):
        # make sure operator run does not fail because of missing site config
        site_config = get_site_config()
        # make sure worker run is not skipped because of missing replacements
        replacement_pullspecs = {'a': 'b'}

        # make symlinks
        for rel_dest, src in (symlinks or {}).items():
            dest = tmpdir.join(rel_dest)
            os.symlink(src, str(dest))

        user_config = get_user_config(manifests_dir)

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator, site_config=site_config,
                          user_config=user_config, replacement_pullspecs=replacement_pullspecs)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        assert "manifests_dir points outside of cloned repo" in str(exc_info.value)

    @pytest.mark.parametrize('filepaths', [
        [],
        ['csv1.yaml', 'csv2.yaml']
    ])
    def test_orchestrator_no_pullspecs(self, filepaths, docker_tasker, workflow, tmpdir, caplog):
        files = [mock_operator_csv(tmpdir, path, []) for path in filepaths]

        user_config = get_user_config(manifests_dir=str(tmpdir))
        site_config = get_site_config()

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=True,
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

    def test_orchestrator_disallowed_registry(self, docker_tasker, workflow, tmpdir):
        # TODO: ImageName parses x/y as namespace/repo and not registry/repo - does it matter?
        pullspecs = ['allowed-registry/ns/foo:1', 'disallowed-registry/ns/bar:2']
        mock_operator_csv(tmpdir, 'csv.yaml', pullspecs)

        user_config = get_user_config(manifests_dir=str(tmpdir))
        site_config = get_site_config(allowed_registries=['allowed-registry'])

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "Registry not allowed: disallowed-registry (in disallowed-registry/ns/bar:2)"
        assert msg in str(exc_info.value)

    @pytest.mark.parametrize('has_envs, raises', [
        (False, False),
        (True, True)
    ])
    def test_orchestrator_exclude_csvs(self, docker_tasker, workflow, tmpdir, caplog,
                                       has_envs, raises):
        csv = mock_operator_csv(tmpdir, 'csv.yaml', ['foo'], with_related_images=True,
                                with_related_image_envs=has_envs)

        user_config = get_user_config(str(tmpdir))
        site_config = get_site_config()

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)

        if raises:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()
            expected = ("Both relatedImages and RELATED_IMAGE_* env vars present in {}. "
                        "Please remove the relatedImages section, it will be reconstructed "
                        "automatically.".format(csv))
            assert expected in str(exc_info.value)
        else:
            runner.run()
            assert "{} has a relatedImages section, skipping".format(csv) in caplog.text

    @responses.activate
    def test_orchestrator(self, docker_tasker, workflow, tmpdir, caplog):
        pullspecs = [
            # final-registry: do not replace registry or repos
            'final-registry/ns/foo@sha256:1',  # -> no change
            'final-registry/ns/foo:1',  # -> final-registry/ns/foo@sha256:1

            # weird-registry: keep registry but replace repos
            'weird-registry/ns/bar@sha256:2',  # -> weird-registry/new-bar@sha256:2
            'weird-registry/ns/bar:1',  # -> weird-registry/new-bar@sha256:2

            # private-registry: replace registry but keep repos
            'private-registry/ns/baz@sha256:3',  # -> public-registry/ns/baz@sha256:3
            'private-registry/ns/baz:1',  # -> public-registry/ns/baz@sha256:3

            # old-registry: replace everything
            'old-registry/ns/spam@sha256:4',  # -> new-registry/new-ns/new-spam@sha256:4
            'old-registry/ns/spam:1',  # -> new-registry/new-ns/new-spam@sha256:4
        ]
        replacement_registries = {
            'private-registry': 'public-registry',
            'old-registry': 'new-registry',
        }
        site_replace_repos = {
            'old-registry': {
                'spam-package': ['new-ns/new-spam']
            }
        }
        user_replace_repos = {
            'weird-registry': {
                'bar-package': 'new-bar'
            }
        }

        mock_digest_query('final-registry/ns/foo:1', 'sha256:1')
        mock_digest_query('weird-registry/ns/bar:1', 'sha256:2')
        mock_digest_query('private-registry/ns/baz:1', 'sha256:3')
        mock_digest_query('old-registry/ns/spam:1', 'sha256:4')
        # there should be no queries for the pullspecs which already contain a digest

        # images should be inspected after their digests are pinned
        mock_inspect_query('weird-registry/ns/bar@sha256:2', {PKG_LABEL: 'bar-package'}, times=2)
        mock_inspect_query('old-registry/ns/spam@sha256:4', {PKG_LABEL: 'spam-package'}, times=2)

        f = mock_operator_csv(tmpdir, 'csv.yaml', pullspecs)
        pre_content = f.read()

        mock_package_mapping_files(site_replace_repos)

        user_config = get_user_config(manifests_dir=str(tmpdir),
                                      repo_replacements=user_replace_repos)
        site_config = get_site_config(registry_post_replace=replacement_registries,
                                      repo_replacements=site_replace_repos)

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)
        runner.run()

        post_content = f.read()
        assert pre_content == post_content  # worker does the replacement, not orchestrator

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        # pullspecs are logged in alphabetical order, if tag is missing, :latest is added
        pullspecs_log = (
            'Found pullspecs:\n'
            'final-registry/ns/foo:1\n'
            'final-registry/ns/foo@sha256:1\n'
            'old-registry/ns/spam:1\n'
            'old-registry/ns/spam@sha256:4\n'
            'private-registry/ns/baz:1\n'
            'private-registry/ns/baz@sha256:3\n'
            'weird-registry/ns/bar:1\n'
            'weird-registry/ns/bar@sha256:2'
        )
        assert pullspecs_log in caplog_text

        assert "Computing replacement pullspecs" in caplog_text

        # replacements are logged in alphabetical order (ordered by the original pullspec)
        replacements_log = (
            'To be replaced:\n'
            'final-registry/ns/foo:1 -> final-registry/ns/foo@sha256:1\n'
            'final-registry/ns/foo@sha256:1 - no change\n'
            'old-registry/ns/spam:1 -> new-registry/new-ns/new-spam@sha256:4\n'
            'old-registry/ns/spam@sha256:4 -> new-registry/new-ns/new-spam@sha256:4\n'
            'private-registry/ns/baz:1 -> public-registry/ns/baz@sha256:3\n'
            'private-registry/ns/baz@sha256:3 -> public-registry/ns/baz@sha256:3\n'
            'weird-registry/ns/bar:1 -> weird-registry/new-bar@sha256:2\n'
            'weird-registry/ns/bar@sha256:2 -> weird-registry/new-bar@sha256:2'
        )
        assert replacements_log in caplog_text

        replacement_pullspecs = {
            'final-registry/ns/foo:1': 'final-registry/ns/foo@sha256:1',
            # final-registry/ns/foo@sha256:1 - no change
            'weird-registry/ns/bar@sha256:2': 'weird-registry/new-bar@sha256:2',
            'weird-registry/ns/bar:1': 'weird-registry/new-bar@sha256:2',
            'private-registry/ns/baz@sha256:3': 'public-registry/ns/baz@sha256:3',
            'private-registry/ns/baz:1': 'public-registry/ns/baz@sha256:3',
            'old-registry/ns/spam@sha256:4': 'new-registry/new-ns/new-spam@sha256:4',
            'old-registry/ns/spam:1': 'new-registry/new-ns/new-spam@sha256:4',
        }
        assert self._get_worker_arg(runner.workflow) == replacement_pullspecs

    @pytest.mark.parametrize('pin_digest', [True, False])
    @pytest.mark.parametrize('replace_repo', [True, False])
    @pytest.mark.parametrize('replace_registry', [True, False])
    @responses.activate
    def test_orchestrator_replacement_opt_out(self, pin_digest, replace_repo, replace_registry,
                                              docker_tasker, workflow, tmpdir, caplog):
        original = 'registry/ns/foo:1'
        replaced = ImageName.parse(original)

        mock_operator_csv(tmpdir, 'csv.yaml', [original])

        if pin_digest:
            replaced.tag = 'sha256:123456'
            mock_digest_query(original, 'sha256:123456')

        if replace_repo:
            replaced.namespace = 'new-ns'
            replaced.repo = 'new-foo'
            user_replace_repos = {
                'registry': {
                    'foo-package': 'new-ns/new-foo'
                }
            }
            query_image = ImageName.parse(original)
            if pin_digest:
                # inspect query is done after pinning digest
                query_image.tag = 'sha256:123456'
            mock_inspect_query(query_image, {PKG_LABEL: 'foo-package'})
        else:
            user_replace_repos = None

        if replace_registry:
            replaced.registry = 'new-registry'
            registry_post_replace = {'registry': 'new-registry'}
        else:
            registry_post_replace = None

        user_config = get_user_config(manifests_dir=str(tmpdir),
                                      repo_replacements=user_replace_repos,
                                      enable_digest_pinning=pin_digest,
                                      enable_repo_replacements=replace_repo,
                                      enable_registry_replacements=replace_registry)
        site_config = get_site_config(registry_post_replace=registry_post_replace)

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)
        runner.run()

        if not pin_digest:
            assert "User disabled digest pinning" in caplog.text
            assert "Making sure tag is manifest list digest" not in caplog.text
        if not replace_repo:
            assert "User disabled repo replacements" in caplog.text
            assert "Replacing namespace/repo" not in caplog.text
        if not replace_registry:
            assert "User disabled registry replacements" in caplog.text
            assert "Replacing registry" not in caplog.text

        if not any([pin_digest, replace_repo, replace_registry]):
            assert "All replacement features disabled, skipping" in caplog.text
            assert self._get_worker_arg(runner.workflow) == {}
        else:
            assert self._get_worker_arg(runner.workflow) == {original: replaced.to_str()}

    @pytest.mark.parametrize('has_envs', [True, False])
    def test_worker_exclude_csvs(self, docker_tasker, workflow, tmpdir, caplog, has_envs):
        # Worker does not care if there is a conflict between relatedImages
        # and RELATED_IMAGE_* env vars, orchestrator should have caught this already
        csv = mock_operator_csv(tmpdir, 'csv.yaml', ['foo'], with_related_images=True,
                                with_related_image_envs=has_envs)
        original_content = csv.read()

        user_config = get_user_config(str(tmpdir))

        runner = mock_env(docker_tasker, workflow, tmpdir,
                          orchestrator=False, user_config=user_config)
        runner.run()

        assert "Replacing pullspecs" not in caplog.text
        assert "Creating relatedImages section" not in caplog.text
        assert csv.read() == original_content

    @pytest.mark.parametrize('ocp_44', [True, False])
    def test_worker(self, ocp_44, docker_tasker, workflow, tmpdir, caplog):
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
        gets_replaced = mock_operator_csv(manifests_dir, 'csv1.yaml', pullspecs, for_ocp_44=ocp_44)
        # this a reference file, make sure it does not get touched by putting it in parent dir
        reference = mock_operator_csv(tmpdir, 'csv2.yaml', replaced_pullspecs,
                                      for_ocp_44=ocp_44, with_related_images=True)

        user_config = get_user_config(manifests_dir=str(manifests_dir))

        runner = mock_env(docker_tasker, workflow, tmpdir, orchestrator=False,
                          user_config=user_config, replacement_pullspecs=replacement_pullspecs)
        runner.run()

        assert gets_replaced.read() == reference.read()

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        assert 'Found operator CSV files:\n{}'.format(gets_replaced) in caplog_text
        assert str(reference) not in caplog_text

        assert 'Replacing pullspecs in {}'.format(gets_replaced) in caplog_text
        assert 'Creating relatedImages section in {}'.format(gets_replaced) in caplog_text

        assert 'Replacing pullspecs in {}'.format(reference) not in caplog_text
        assert 'Creating relatedImages section in {}'.format(reference) not in caplog_text


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
    def test_pin_digest(self, image, should_query, digest, caplog):
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

        if should_query:
            assert "Querying {} for manifest list digest".format(image.registry) in caplog.text
        else:
            assert "{} looks like a digest, skipping query".format(digest) in caplog.text

    @pytest.mark.parametrize('image, replacement_registries, replaced', [
        ('old-registry/ns/foo', {'old-registry': 'new-registry'}, 'new-registry/ns/foo'),
        ('registry/ns/foo', {}, 'registry/ns/foo'),
    ])
    def test_replace_registry(self, image, replacement_registries, replaced, caplog):
        image = ImageName.parse(image)
        replaced = ImageName.parse(replaced)

        site_config = get_site_config(registry_post_replace=replacement_registries)
        replacer = PullspecReplacer(user_config={}, site_config=site_config)

        assert replacer.replace_registry(image) == replaced

        if image.registry not in replacement_registries:
            msg = "registry_post_replace not configured for {}".format(image.registry)
            assert msg in caplog.text

    @pytest.mark.parametrize('image,site_replacements,user_replacements,replaced,should_query', [
        # can replace repo if only 1 option in site config
        ('a/x/foo:1',
         {'a': {'foo-package': ['y/bar']}},
         None,
         'a/y/bar:1',
         True),
        # user can define replacement if package not in site config
        ('a/x/foo:1',
         None,
         {'a': {'foo-package': 'y/bar'}},
         'a/y/bar:1',
         True),
        # user can choose one of the options in site config
        ('a/x/foo:1',
         {'a': {'foo-package': ['y/bar', 'y/baz']}},
         {'a': {'foo-package': 'y/baz'}},
         'a/y/baz:1',
         True),
        # replacement can be just repo
        ('a/x/foo:1',
         {'a': {'foo-package': ['bar']}},
         None,
         ImageName(registry='a', repo='bar', tag='1'),
         True),
        # no config, no replacement
        ('a/x/foo:1',
         None,
         None,
         'a/x/foo:1',
         False),
        # missing registry, no replacement
        ('foo:1',
         {'a': {'foo-package': ['y/bar']}},
         {'a': {'foo-package': 'y/bar'}},
         'foo:1',
         False),
    ])
    @responses.activate
    def test_replace_repo(self, image, site_replacements, user_replacements,
                          replaced, should_query, tmpdir, caplog):
        image = ImageName.parse(image)
        replaced = ImageName.parse(replaced)

        mock_package_mapping_files(site_replacements)
        mock_inspect_query(image,
                           {PKG_LABEL: '{}-package'.format(image.repo)},
                           times=1 if should_query else 0)

        site_config = get_site_config(repo_replacements=site_replacements)
        user_config = get_user_config(manifests_dir=str(tmpdir),
                                      repo_replacements=user_replacements)

        replacer = PullspecReplacer(user_config=user_config, site_config=site_config)

        assert replacer.replace_repo(image) == replaced

        if site_replacements and image.registry in site_replacements:
            assert "Downloading mapping file for {}".format(image.registry) in caplog.text

        if should_query:
            assert "Querying {} for image labels".format(image.registry) in caplog.text
            assert "Resolved package name" in caplog.text
            assert "Replacement for package" in caplog.text
        else:
            assert "repo_replacements not configured for {}".format(image.registry) in caplog.text

    @pytest.mark.parametrize('image,site_replacements,user_replacements,inspect_labels,exc_msg', [
        # replacements configured in site config, repo missing
        ('a/x/foo:1',
         {'a': {}},
         None,
         {PKG_LABEL: 'foo-package'},
         'Replacement not configured for package foo-package (from a/x/foo:1). '
         'Please specify replacement in container.yaml'),
        # replacements configured in user config, repo missing
        ('a/x/foo:1',
         None,
         {'a': {}},
         {PKG_LABEL: 'foo-package'},
         'Replacement not configured for package foo-package (from a/x/foo:1). '
         'Please specify replacement in container.yaml'),
        # multiple options for replacement in site config
        ('a/x/foo:1',
         {'a': {'foo-package': ['bar', 'baz']}},
         None,
         {PKG_LABEL: 'foo-package'},
         'Multiple replacements for package foo-package (from a/x/foo:1): bar, baz. '
         'Please specify replacement in container.yaml'),
        # user tried to override with an invalid replacement
        ('a/x/foo:1',
         {'a': {'foo-package': ['bar', 'baz']}},
         {'a': {'foo-package': 'spam'}},
         {PKG_LABEL: 'foo-package'},
         'Invalid replacement for package foo-package: spam (choices: bar, baz)'),
        # replacements configured, image has no component label
        ('a/x/foo:1',
         {'a': {}},
         None,
         {},
         'Image has no component label: a/x/foo:1'),
    ])
    @responses.activate
    def test_replace_repo_failure(self, image, site_replacements, user_replacements,
                                  inspect_labels, exc_msg, tmpdir):
        image = ImageName.parse(image)

        mock_package_mapping_files(site_replacements)
        mock_inspect_query(image, inspect_labels)

        site_config = get_site_config(repo_replacements=site_replacements)
        user_config = get_user_config(manifests_dir=str(tmpdir),
                                      repo_replacements=user_replacements)

        replacer = PullspecReplacer(user_config=user_config, site_config=site_config)

        with pytest.raises(RuntimeError) as exc_info:
            replacer.replace_repo(image)

        assert str(exc_info.value) == exc_msg

    @pytest.mark.parametrize('site_replacements, exc_msg', [
        # replacement is not a list
        ({'a': {'foo-package': 'bar'}},
         'is not of type {!r}'.format('array')),
        # replacement is an empty list
        ({'a': {'foo-package': []}},
         '[] is too short'),
    ])
    @responses.activate
    def test_replace_repo_schema_validation(self, site_replacements, exc_msg):
        image = ImageName.parse('a/x/foo')

        mock_package_mapping_files(site_replacements)
        mock_inspect_query(image, {}, times=0)

        site_config = get_site_config(repo_replacements=site_replacements)

        replacer = PullspecReplacer(user_config={}, site_config=site_config)

        with pytest.raises(jsonschema.ValidationError) as exc_info:
            replacer.replace_repo(image)

        assert exc_msg in str(exc_info.value)
