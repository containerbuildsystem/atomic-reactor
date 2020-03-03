"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

from flexmock import flexmock
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_check_user_settings import CheckUserSettingsPlugin

from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource


def mock_dockerfile(tmpdir, labels):
    base = 'From fedora:30'
    cmd = 'CMD /bin/cowsay moo'
    extra_labels = [
        'LABEL {}'.format(label)
        for label in labels

    ]
    data = '\n'.join([base] + extra_labels + [cmd])
    tmpdir.join('Dockerfile').write(data)


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE,
        source={"provider": "git", "uri": "asd"}
    )
    workflow.source = StubSource()
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    return workflow


def mock_env(tmpdir, docker_tasker, labels=(), flatpak=False):
    if not flatpak:
        # flatpak build has no Dockefile
        mock_dockerfile(tmpdir, labels)
    workflow = mock_workflow(tmpdir)
    plugin_conf = [{'name': CheckUserSettingsPlugin.key,
                    'args': {'flatpak': flatpak}}]

    runner = PreBuildPluginsRunner(docker_tasker, workflow, plugin_conf)

    return runner


class TestDockerfileChecks(object):
    """
    Test checks related to Dockerfile
    """

    @pytest.mark.parametrize('labels,expected_fail', (
        (['com.redhat.delivery.appregistry=true',
          'com.redhat.delivery.operator.bundle=true'],
         True),
        (['com.redhat.delivery.appregistry=true',
          'com.redhat.delivery.operator.bundle=false'],
         False),
        (['com.redhat.delivery.appregistry=true'], False),
        (['com.redhat.delivery.operator.bundle=true'], False),
    ))
    def test_mutual_exclusivity_of_labels(self, tmpdir, docker_tasker, labels, expected_fail):
        """Appregistry and operator.bundle labels are mutually exclusive"""
        runner = mock_env(tmpdir, docker_tasker, labels=labels)
        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert 'only one of labels' in str(e.value)
        else:
            runner.run()

    def test_flatpak_skip_dockerfile_check(self, tmpdir, docker_tasker, caplog):
        """Flatpak builds have no user Dockerfiles, dockefile check must be skipped"""
        runner = mock_env(tmpdir, docker_tasker, flatpak=True)
        runner.run()

        assert 'Skipping Dockerfile checks' in caplog.text
