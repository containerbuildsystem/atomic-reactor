"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import copy
import io
import os
import pathlib

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

import pytest
import responses
from flexmock import flexmock

import atomic_reactor.util
from atomic_reactor.constants import INSPECT_CONFIG, PLUGIN_PIN_OPERATOR_DIGESTS_KEY
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_OVERRIDE_KWARGS)
from atomic_reactor.plugins.pre_pin_operator_digest import (PinOperatorDigestsPlugin,
                                                            PullspecReplacer)
from tests.util import OPERATOR_MANIFESTS_DIR

from osbs.exceptions import OsbsValidationException
from osbs.utils import ImageName

from tests.stubs import StubConfig
from tests.mock_env import MockEnv


PKG_LABEL = 'com.redhat.component'
PKG_NAME = 'test-package'


yaml = YAML()


# When defining mock configuration for source_registry/pull_registries,
# do not use auth unless you also want to mock a dockercfg file
SOURCE_REGISTRY_URI = 'registry.private.example.com'
SOURCE_REGISTRY = {
    'url': 'https://{}'.format(SOURCE_REGISTRY_URI),
}

pytestmark = pytest.mark.usefixtures('user_params')


def mock_dockerfile(repo_dir, base='scratch', operator_bundle_label=True):
    dockerfile = (
        'FROM {base}\n'
        'LABEL {component_label}={component_value}\n'
        'LABEL com.redhat.delivery.operator.bundle={label_value}\n'
    ).format(base=base, component_label=PKG_LABEL, component_value=PKG_NAME,
             label_value=operator_bundle_label)

    repo_dir.join('Dockerfile').write(dockerfile)


def make_reactor_config(operators_config):
    config = {
        'version': 1,
        'source_registry': SOURCE_REGISTRY
    }
    if operators_config:
        config['operator_manifests'] = operators_config
    return config


def make_user_config(operator_config):
    config = StubConfig()
    setattr(config, 'operator_manifests', operator_config)
    return config


def mock_env(docker_tasker, repo_dir, orchestrator,
             user_config=None, site_config=None,
             df_base='scratch', df_operator_label=True,
             replacement_pullspecs=None, add_to_config=None,
             write_container_yaml=True):
    """
    Mock environment for test

    :param docker_tasker: conftest fixture
    :param repo_dir: pylint fixture,
    :type repo_dir: py.path.LocalPath
    :param orchestrator: is the plugin running in orchestrator?
    :param user_config: container.yaml operator_manifest config
    :param site_config: reactor-config-map operator_manifests config
    :param df_base: base image in Dockerfile, non-scratch should fail
    :param df_operator_label: presence of operator manifest bundle label
    :param replacement_pullspecs: plugin argument from osbs-client

    :return: configured plugin runner
    """
    reactor_config = make_reactor_config(site_config)
    if add_to_config:
        reactor_config.update(add_to_config)
    env = (MockEnv()
           .for_plugin('prebuild',
                       PinOperatorDigestsPlugin.key,
                       {'replacement_pullspecs': replacement_pullspecs})
           .set_reactor_config(reactor_config))

    if orchestrator:
        env.make_orchestrator()

    if write_container_yaml:
        with open(str(repo_dir.join('container.yaml')), 'w') as f:
            yaml.dump({'operator_manifests': user_config}, stream=f)

    mock_dockerfile(repo_dir, df_base, df_operator_label)

    from atomic_reactor.source import PathSource
    env.workflow.source = PathSource('path', str(repo_dir))

    # NOTE: is this path correct?
    env.workflow.builder.set_df_path(str(repo_dir))

    return env.create_runner(docker_tasker)


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
        'metadata': {},
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


def mock_digest_query(image_digest_map):

    updated_map = {
        ImageName.parse(pullspec).to_str(): digest
        for pullspec, digest in image_digest_map.items()
    }

    def mocked_get_manifest_list_digest(image):
        return updated_map[image.to_str()]

    (flexmock(atomic_reactor.util.RegistryClient)
        .should_receive('get_manifest_list_digest')
        .replace_with(mocked_get_manifest_list_digest))


def mock_inspect_query(pullspec, labels, times=1):
    image = ImageName.parse(pullspec)
    inspect = {
        INSPECT_CONFIG: {
            'Labels': labels
        }
    }
    (flexmock(atomic_reactor.util.RegistryClient)
        .should_receive('get_inspect_for_image')
        .with_args(image)
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


def get_site_config(allowed_registries=None, registry_post_replace=None, repo_replacements=None,
                    skip_all_allow_list=None):
    registry_post_replace = registry_post_replace or {}
    repo_replacements = repo_replacements or {}
    skip_allow_list = skip_all_allow_list or []
    return {
        'allowed_registries': allowed_registries,
        'registry_post_replace': [
            {'old': old, 'new': new} for old, new in registry_post_replace.items()
        ],
        'repo_replacements': [
            {'registry': registry, 'package_mappings_url': path}
            for registry, path in repo_replacements.items()
        ],
        'skip_all_allow_list': [package for package in skip_allow_list]
    }


def get_user_config(manifests_dir, repo_replacements=None, enable_digest_pinning=True,
                    enable_repo_replacements=True, enable_registry_replacements=True,
                    skip_all=False):
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
        'skip_all': skip_all,
    }


class TestPinOperatorDigest(object):
    def _get_worker_arg(self, workflow):
        return get_build_kwarg(workflow, "operator_bundle_replacement_pullspecs")

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_run_only_for_operator_bundle_label(self, orchestrator,
                                                docker_tasker, tmpdir, caplog):
        runner = mock_env(docker_tasker, tmpdir,
                          orchestrator=orchestrator,
                          df_operator_label=False,
                          write_container_yaml=False)
        runner.run()
        assert "Not an operator manifest bundle build, skipping plugin" in caplog.text

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_missing_site_config(self, orchestrator, docker_tasker, tmpdir, caplog):
        runner = mock_env(docker_tasker, tmpdir,
                          orchestrator=orchestrator,
                          write_container_yaml=False)
        runner.run()

        msg = "operator_manifests configuration missing in reactor config map, aborting"
        assert msg in caplog.text
        assert "Looking for operator CSV files" not in caplog.text

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_missing_user_config(self, orchestrator, docker_tasker, tmpdir):
        # make sure plugin is not skipped because of missing site config
        site_config = get_site_config()

        runner = mock_env(docker_tasker, tmpdir,
                          orchestrator=orchestrator,
                          site_config=site_config,
                          write_container_yaml=False)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "operator_manifests configuration missing in container.yaml"
        assert msg in str(exc_info.value)

    # FIXME: !!!
    @pytest.mark.parametrize('orchestrator', [True, False])
    @pytest.mark.parametrize('manifests_dir, symlinks', [
        ('foo', {'foo': '/tmp/foo'}),
    ])
    def test_manifests_dir_not_subdir_of_repo(self, manifests_dir, symlinks,
                                              orchestrator, docker_tasker, tmpdir):
        # make sure plugin is not skipped because of missing site config
        site_config = get_site_config()
        user_config = get_user_config(manifests_dir)
        runner = mock_env(docker_tasker, tmpdir, orchestrator,
                          site_config=site_config,
                          user_config=user_config)

        # make symlinks
        for rel_dest, src in (symlinks or {}).items():
            dest = os.path.join(runner.workflow.source.path, rel_dest)
            pathlib.Path(src).mkdir(exist_ok=True)
            os.symlink(src, str(dest))

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        assert "manifests_dir points outside of cloned repo" in str(exc_info.value)

    @pytest.mark.parametrize('filepaths', [
        ['csv1.yaml'],
        ['csv2.yaml'],
        ['csv1.yaml', 'csv2.yaml']
    ])
    @pytest.mark.parametrize('skip_all', [True, False])
    def test_orchestrator_no_pullspecs(self, docker_tasker, tmpdir, caplog, filepaths, skip_all):
        manifests_dir = tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir()
        for path in filepaths:
            mock_operator_csv(manifests_dir, path, [])

        user_config = get_user_config(OPERATOR_MANIFESTS_DIR, skip_all=skip_all)
        site_config = get_site_config(skip_all_allow_list=[PKG_NAME])

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)

        if len(filepaths) > 1:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()
            msg = "Operator bundle may contain only 1 CSV file, but contains more:"
            assert msg in str(exc_info.value)
            return

        result = runner.run()

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        source_path = runner.workflow.source.path
        assert f"Looking for operator CSV files in {source_path}" in caplog_text
        assert "Found operator CSV file:" in caplog_text
        csv_files = [
            os.path.join(runner.workflow.source.manifests_dir, path)
            for path in filepaths
        ]
        for f in csv_files:
            assert str(f) in caplog_text
        assert "No pullspecs found" in caplog_text
        assert self._get_worker_arg(runner.workflow) is None

        expected = {
            'related_images': {
                'pullspecs': [],
                'created_by_osbs': False,
            }
        }
        assert result['pin_operator_digest'] == expected

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_fail_without_csv(self, docker_tasker, tmpdir, orchestrator):
        """CSV file is mandatory part of operator, fail if it's not present"""
        tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir()
        user_config = get_user_config(OPERATOR_MANIFESTS_DIR)
        site_config = get_site_config()

        runner = mock_env(docker_tasker, tmpdir, orchestrator=orchestrator,
                          user_config=user_config, site_config=site_config)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        assert "Missing ClusterServiceVersion in operator manifests" in str(exc_info.value)

    def test_orchestrator_disallowed_registry(self, docker_tasker, tmpdir):
        # TODO: ImageName parses x/y as namespace/repo and not registry/repo - does it matter?
        pullspecs = ['allowed-registry/ns/foo:1', 'disallowed-registry/ns/bar:2']
        mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(), 'csv.yaml', pullspecs)

        user_config = get_user_config(OPERATOR_MANIFESTS_DIR)
        site_config = get_site_config(allowed_registries=['allowed-registry'])

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "Registry not allowed: disallowed-registry (in disallowed-registry/ns/bar:2)"
        assert msg in str(exc_info.value)

    def test_orchestrator_raise_error_if_csv_has_both_related_images_and_related_env_vars(
        self, docker_tasker, tmpdir, caplog
    ):
        csv = mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(),
                                'csv.yaml', ['foo'],
                                with_related_images=True,
                                with_related_image_envs=True)

        user_config = get_user_config(OPERATOR_MANIFESTS_DIR)
        site_config = get_site_config()

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)

        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        csv = os.path.join(runner.workflow.source.manifests_dir, csv.basename)
        expected = (
            f"Both relatedImages and RELATED_IMAGE_* env vars present in {csv}. "
            f"Please remove the relatedImages section, it will be reconstructed "
            f"automatically."
        )
        assert expected in str(exc_info.value)

    @responses.activate
    def test_orchestrator(self, docker_tasker, tmpdir, caplog):
        pullspecs = [
            # registry.private.example.com: do not replace registry or repos
            'registry.private.example.com/ns/foo@sha256:1',  # -> no change
            'registry.private.example.com/ns/foo:1',
            # -> registry.private.example.com/ns/foo@sha256:1

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

        mock_digest_query({
            'registry.private.example.com/ns/foo:1': 'sha256:1',
            'weird-registry/ns/bar:1': 'sha256:2',
            'private-registry/ns/baz:1': 'sha256:3',
            'old-registry/ns/spam:1': 'sha256:4',
        })
        # there should be no queries for the pullspecs which already contain a digest

        # images should be inspected after their digests are pinned
        mock_inspect_query('weird-registry/ns/bar@sha256:2', {PKG_LABEL: 'bar-package'}, times=2)
        mock_inspect_query('old-registry/ns/spam@sha256:4', {PKG_LABEL: 'spam-package'}, times=2)

        f = mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(), 'csv.yaml', pullspecs)
        pre_content = f.read()

        mock_package_mapping_files(site_replace_repos)

        user_config = get_user_config(manifests_dir=OPERATOR_MANIFESTS_DIR,
                                      repo_replacements=user_replace_repos)
        site_config = get_site_config(registry_post_replace=replacement_registries,
                                      repo_replacements=site_replace_repos)

        pull_registries = {'pull_registries': [
            {'url': 'https://old-registry'},
            {'url': 'https://private-registry'},
            {'url': 'https://weird-registry'},
        ]}

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config,
                          add_to_config=pull_registries)

        result = runner.run()

        post_content = f.read()
        assert pre_content == post_content  # worker does the replacement, not orchestrator

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        # pullspecs are logged in alphabetical order, if tag is missing, :latest is added
        pullspecs_log = (
            'Found pullspecs:\n'
            'old-registry/ns/spam:1\n'
            'old-registry/ns/spam@sha256:4\n'
            'private-registry/ns/baz:1\n'
            'private-registry/ns/baz@sha256:3\n'
            'registry.private.example.com/ns/foo:1\n'
            'registry.private.example.com/ns/foo@sha256:1\n'
            'weird-registry/ns/bar:1\n'
            'weird-registry/ns/bar@sha256:2'
        )
        assert pullspecs_log in caplog_text

        assert "Computing replacement pullspecs" in caplog_text

        # replacements are logged in alphabetical order (ordered by the original pullspec)
        replacements_log = (
            'To be replaced:\n'
            'old-registry/ns/spam:1 -> new-registry/new-ns/new-spam@sha256:4\n'
            'old-registry/ns/spam@sha256:4 -> new-registry/new-ns/new-spam@sha256:4\n'
            'private-registry/ns/baz:1 -> public-registry/ns/baz@sha256:3\n'
            'private-registry/ns/baz@sha256:3 -> public-registry/ns/baz@sha256:3\n'
            'registry.private.example.com/ns/foo:1 -> '
            'registry.private.example.com/ns/foo@sha256:1\n'
            'registry.private.example.com/ns/foo@sha256:1 - no change\n'
            'weird-registry/ns/bar:1 -> weird-registry/new-bar@sha256:2\n'
            'weird-registry/ns/bar@sha256:2 -> weird-registry/new-bar@sha256:2'
        )
        assert replacements_log in caplog_text

        replacement_pullspecs = {
            'registry.private.example.com/ns/foo:1': 'registry.private.example.com/ns/foo@sha256:1',
            # registry.private.example.com/ns/foo@sha256:1 - no change
            'weird-registry/ns/bar@sha256:2': 'weird-registry/new-bar@sha256:2',
            'weird-registry/ns/bar:1': 'weird-registry/new-bar@sha256:2',
            'private-registry/ns/baz@sha256:3': 'public-registry/ns/baz@sha256:3',
            'private-registry/ns/baz:1': 'public-registry/ns/baz@sha256:3',
            'old-registry/ns/spam@sha256:4': 'new-registry/new-ns/new-spam@sha256:4',
            'old-registry/ns/spam:1': 'new-registry/new-ns/new-spam@sha256:4',
        }
        assert self._get_worker_arg(runner.workflow) == replacement_pullspecs

        expected_result = {
            'related_images': {
                'pullspecs': [
                    {
                        'original': ImageName.parse('old-registry/ns/spam:1'),
                        'new': ImageName.parse('new-registry/new-ns/new-spam@sha256:4'),
                        'pinned': True,
                        'replaced': True
                    }, {
                        'original': ImageName.parse('old-registry/ns/spam@sha256:4'),
                        'new': ImageName.parse('new-registry/new-ns/new-spam@sha256:4'),
                        'pinned': False,
                        'replaced': True
                    }, {
                        'original': ImageName.parse('private-registry/ns/baz:1'),
                        'new': ImageName.parse('public-registry/ns/baz@sha256:3'),
                        'pinned': True,
                        'replaced': True
                    }, {
                        'original': ImageName.parse('private-registry/ns/baz@sha256:3'),
                        'new': ImageName.parse('public-registry/ns/baz@sha256:3'),
                        'pinned': False,
                        'replaced': True
                    }, {
                        'original': ImageName.parse('registry.private.example.com/ns/foo:1'),
                        'new': ImageName.parse('registry.private.example.com/ns/foo@sha256:1'),
                        'pinned': True,
                        'replaced': True
                    }, {
                        'original': ImageName.parse('registry.private.example.com/ns/foo@sha256:1'),
                        'new': ImageName.parse('registry.private.example.com/ns/foo@sha256:1'),
                        'pinned': False,
                        'replaced': False
                    }, {
                        'original': ImageName.parse('weird-registry/ns/bar:1'),
                        'new': ImageName(
                            registry='weird-registry', repo='new-bar', tag='sha256:2'),
                        'pinned': True,
                        'replaced': True
                    }, {
                        'original': ImageName.parse('weird-registry/ns/bar@sha256:2'),
                        'new': ImageName(
                            registry='weird-registry', repo='new-bar', tag='sha256:2'),
                        'pinned': False,
                        'replaced': True
                    },
                ],
                'created_by_osbs': True,
            }
        }

        assert result['pin_operator_digest'] == expected_result

    @pytest.mark.parametrize('pin_digest', [True, False])
    @pytest.mark.parametrize('replace_repo', [True, False])
    @pytest.mark.parametrize('replace_registry', [True, False])
    def test_orchestrator_replacement_opt_out(self, pin_digest, replace_repo, replace_registry,
                                              docker_tasker, tmpdir, caplog):
        original = '{}/ns/foo:1'.format(SOURCE_REGISTRY_URI)
        replaced = ImageName.parse(original)

        mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(), 'csv.yaml', [original])

        if pin_digest:
            replaced.tag = 'sha256:123456'
            mock_digest_query({original: 'sha256:123456'})

        if replace_repo:
            replaced.namespace = 'new-ns'
            replaced.repo = 'new-foo'
            user_replace_repos = {
                SOURCE_REGISTRY_URI: {
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
            registry_post_replace = {SOURCE_REGISTRY_URI: 'new-registry'}
        else:
            registry_post_replace = None

        user_config = get_user_config(manifests_dir=OPERATOR_MANIFESTS_DIR,
                                      repo_replacements=user_replace_repos,
                                      enable_digest_pinning=pin_digest,
                                      enable_repo_replacements=replace_repo,
                                      enable_registry_replacements=replace_registry)
        site_config = get_site_config(registry_post_replace=registry_post_replace)

        runner = mock_env(docker_tasker, tmpdir, orchestrator=True,
                          user_config=user_config, site_config=site_config)
        result = runner.run()

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
            assert "All replacement features disabled" in caplog.text
            assert self._get_worker_arg(runner.workflow) == {}
        else:
            assert self._get_worker_arg(runner.workflow) == {original: replaced.to_str()}

        # plugin must always retun pullspecs
        assert result['pin_operator_digest']['related_images']['pullspecs']

    @pytest.mark.parametrize('has_envs', [True, False])
    def test_worker_exclude_csvs(self, docker_tasker, tmpdir, caplog, has_envs):
        # Worker does not care if there is a conflict between relatedImages
        # and RELATED_IMAGE_* env vars, orchestrator should have caught this already
        csv = mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(), 'csv.yaml', ['foo'],
                                with_related_images=True,
                                with_related_image_envs=has_envs)
        original_content = csv.read()

        user_config = get_user_config(OPERATOR_MANIFESTS_DIR)

        runner = mock_env(docker_tasker, tmpdir, orchestrator=False,
                          site_config=get_site_config(), user_config=user_config)
        runner.run()

        assert "Replacing pullspecs" not in caplog.text
        assert "Creating relatedImages section" not in caplog.text
        assert csv.read() == original_content

    @pytest.mark.parametrize('ocp_44', [True, False])
    def test_worker(self, ocp_44, docker_tasker, tmpdir, caplog):
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

        # manifests_dir = tmpdir.mkdir('manifests')
        mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(),
                          'csv1.yaml', pullspecs,
                          for_ocp_44=ocp_44)

        # this a reference file, make sure it does not get touched by putting it in parent dir
        reference = mock_operator_csv(tmpdir, 'csv2.yaml', replaced_pullspecs,
                                      for_ocp_44=ocp_44, with_related_images=True)

        user_config = get_user_config(OPERATOR_MANIFESTS_DIR)

        runner = mock_env(docker_tasker, tmpdir, orchestrator=False, site_config=get_site_config(),
                          user_config=user_config, replacement_pullspecs=replacement_pullspecs)
        runner.run()

        replaced_csv = os.path.join(runner.workflow.source.manifests_dir, 'csv1.yaml')
        with open(replaced_csv, 'r') as f:
            assert f.read() == reference.read()

        caplog_text = "\n".join(rec.message for rec in caplog.records)

        assert f'Found operator CSV file: {replaced_csv}' in caplog_text
        assert str(reference) not in caplog_text

        assert f'Replacing pullspecs in {replaced_csv}' in caplog_text
        assert f'Creating relatedImages section in {replaced_csv}' in caplog_text

        assert 'Replacing pullspecs in {}'.format(reference) not in caplog_text
        assert 'Creating relatedImages section in {}'.format(reference) not in caplog_text

    def test_return_pullspecs_in_related_images(self, docker_tasker, tmpdir):
        """
        Ensure the pullspecs listed in spec.relatedImages are returned if a CSV
        file has such a section
        """
        pullspecs = [
            'registry.r.c/project/foo@sha256:123456',
            # Whatever the pullspec includes digest or tag, the pullspec inside spec.relatedImages
            # should be returned directly without any change.
            'registry.r.c/project/bar:20200901',
        ]
        mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(), 'csv1.yaml', pullspecs,
                          with_related_images=True)

        runner = mock_env(docker_tasker, tmpdir,
                          orchestrator=True,
                          user_config=get_user_config(OPERATOR_MANIFESTS_DIR),
                          site_config=get_site_config())
        result = runner.run()

        expected_result = [
            {
                'original': ImageName.parse(item),
                'new': ImageName.parse(item),
                'pinned': False,
                'replaced': False
            }
            for item in pullspecs
        ]

        got_pullspecs_metadata = result[PLUGIN_PIN_OPERATOR_DIGESTS_KEY]['related_images']

        assert not got_pullspecs_metadata['created_by_osbs'], \
            'Returning pullspecs inlcuded in spec.relatedImages directly. ' \
            'Expected created_by_osbs is False.'

        assert (
            sorted(expected_result, key=str) ==
            sorted(got_pullspecs_metadata['pullspecs'], key=str)
        )

    @pytest.mark.parametrize('orchestrator', [True, False])
    @pytest.mark.parametrize('has_related_images', [True, False])
    @pytest.mark.parametrize('pull_specs, has_related_image_envs', [
        ([], False),
        (['foo'], True),
        (['foo'], False),
    ])
    @pytest.mark.parametrize('skip_all_allow_list', [None, [PKG_NAME]])
    def test_skip_all(self, docker_tasker, tmpdir, caplog, orchestrator, has_related_images,
                      pull_specs, has_related_image_envs, skip_all_allow_list):
        mock_operator_csv(tmpdir.join(OPERATOR_MANIFESTS_DIR).mkdir(), 'csv.yaml', pull_specs,
                          with_related_images=has_related_images,
                          with_related_image_envs=has_related_image_envs)

        user_config = get_user_config(OPERATOR_MANIFESTS_DIR, skip_all=True)

        runner = mock_env(docker_tasker, tmpdir, orchestrator=orchestrator,
                          site_config=get_site_config(skip_all_allow_list=skip_all_allow_list),
                          user_config=user_config)

        has_skip_log_entry = True

        if not skip_all_allow_list or (not has_related_images and pull_specs and orchestrator):

            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()

            if not skip_all_allow_list:
                exc_msg = "Koji package: {} isn't allowed to use skip_all for " \
                          "operator bundles".format(PKG_NAME)
                has_skip_log_entry = False
            else:
                exc_msg = "skip_all defined but relatedImages section doesn't exist"
            assert exc_msg in str(exc_info.value)
        else:
            runner.run()

        if has_skip_log_entry:
            if orchestrator:
                assert "skip_all defined for operator manifests" in caplog.text
            else:
                assert "skip_all defined, not running on worker" in caplog.text


class TestPullspecReplacer(object):
    def mock_workflow(self, site_config):
        return MockEnv().set_reactor_config(make_reactor_config(site_config)).workflow

    @pytest.mark.parametrize('allowed_registries, image, allowed', [
        (None, 'registry/ns/foo', True),
        (['registry'], 'registry/ns/foo', True),
        ([], 'registry/ns/foo', False),  # not actually allowed in schema, but sensible
        (['other-registry'], 'registry/ns/foo', False),
    ])
    def test_registry_is_allowed(self, allowed_registries, image, allowed):
        site_config = get_site_config(allowed_registries=allowed_registries)
        replacer = PullspecReplacer(user_config={}, workflow=self.mock_workflow(site_config))
        image = ImageName.parse(image)
        assert replacer.registry_is_allowed(image) == allowed

    @pytest.mark.parametrize('pullspec, should_query, digest', [
        ('{}/ns/foo'.format(SOURCE_REGISTRY_URI), True, 'sha256:123456'),
        ('{}/ns/bar@sha256:654321'.format(SOURCE_REGISTRY_URI), False, 'sha256:654321'),
    ])
    def test_pin_digest(self, pullspec, should_query, digest, caplog):
        if should_query:
            mock_digest_query({pullspec: digest})

        image = ImageName.parse(pullspec)
        site_config = get_site_config()
        replacer = PullspecReplacer(user_config={}, workflow=self.mock_workflow(site_config))
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
        replacer = PullspecReplacer(user_config={}, workflow=self.mock_workflow(site_config))

        assert replacer.replace_registry(image) == replaced

        if image.registry not in replacement_registries:
            msg = "registry_post_replace not configured for {}".format(image.registry)
            assert msg in caplog.text

    @pytest.mark.parametrize('image,site_replacements,user_replacements,replaced,should_query', [
        # can replace repo if only 1 option in site config
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {'foo-package': ['y/bar']}},
         None,
         '{}/y/bar:1'.format(SOURCE_REGISTRY_URI),
         True),
        # user can define replacement if package not in site config
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         None,
         {SOURCE_REGISTRY_URI: {'foo-package': 'y/bar'}},
         '{}/y/bar:1'.format(SOURCE_REGISTRY_URI),
         True),
        # user can choose one of the options in site config
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {'foo-package': ['y/bar', 'y/baz']}},
         {SOURCE_REGISTRY_URI: {'foo-package': 'y/baz'}},
         '{}/y/baz:1'.format(SOURCE_REGISTRY_URI),
         True),
        # replacement can be just repo
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {'foo-package': ['bar']}},
         None,
         ImageName(registry=SOURCE_REGISTRY_URI, repo='bar', tag='1'),
         True),
        # no config, no replacement
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         None,
         None,
         '{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         False),
        # missing registry, no replacement
        ('foo:1',
         {SOURCE_REGISTRY_URI: {'foo-package': ['y/bar']}},
         {SOURCE_REGISTRY_URI: {'foo-package': 'y/bar'}},
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

        replacer = PullspecReplacer(user_config=user_config,
                                    workflow=self.mock_workflow(site_config))

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
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {}},
         None,
         {PKG_LABEL: 'foo-package'},
         'Replacement not configured for package foo-package (from {}/x/foo:1). '
         'Please specify replacement in container.yaml'.format(SOURCE_REGISTRY_URI)),
        # replacements configured in user config, repo missing
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         None,
         {SOURCE_REGISTRY_URI: {}},
         {PKG_LABEL: 'foo-package'},
         'Replacement not configured for package foo-package (from {}/x/foo:1). '
         'Please specify replacement in container.yaml'.format(SOURCE_REGISTRY_URI)),
        # multiple options for replacement in site config
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {'foo-package': ['bar', 'baz']}},
         None,
         {PKG_LABEL: 'foo-package'},
         'Multiple replacements for package foo-package (from {}/x/foo:1): bar, baz. '
         'Please specify replacement in container.yaml'.format(SOURCE_REGISTRY_URI)),
        # user tried to override with an invalid replacement
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {'foo-package': ['bar', 'baz']}},
         {SOURCE_REGISTRY_URI: {'foo-package': 'spam'}},
         {PKG_LABEL: 'foo-package'},
         'Invalid replacement for package foo-package: spam (choices: bar, baz)'),
        # replacements configured, image has no component label
        ('{}/x/foo:1'.format(SOURCE_REGISTRY_URI),
         {SOURCE_REGISTRY_URI: {}},
         None,
         {},
         'Image has no component label: {}/x/foo:1'.format(SOURCE_REGISTRY_URI)),
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

        replacer = PullspecReplacer(user_config=user_config,
                                    workflow=self.mock_workflow(site_config))

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

        replacer = PullspecReplacer(user_config={}, workflow=self.mock_workflow(site_config))

        with pytest.raises(OsbsValidationException) as exc_info:
            replacer.replace_repo(image)

        assert exc_msg in str(exc_info.value)
