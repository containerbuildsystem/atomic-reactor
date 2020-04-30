"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

from functools import partial

from flexmock import flexmock
import pytest

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_check_user_settings import CheckUserSettingsPlugin

from tests.stubs import StubInsideBuilder, StubSource


def mock_dockerfile(tmpdir, labels, from_scratch=False):
    base = 'FROM scratch' if from_scratch else 'FROM fedora:30'
    cmd = 'CMD /bin/cowsay moo'
    extra_labels = [
        'LABEL {}'.format(label)
        for label in labels

    ]
    data = '\n'.join([base] + extra_labels + [cmd])
    tmpdir.join('Dockerfile').write(data)


def mock_dockerfile_multistage(tmpdir, labels, from_scratch=False):
    data = """\
    FROM scratch
    RUN echo *

    {data_from}
    {extra_labels}
    CMD /bin/cowsay moo
    """
    extra_labels = '\n'.join(
        'LABEL {}'.format(label)
        for label in labels
    )
    data_from = 'FROM scratch' if from_scratch else 'FROM fedora:30'
    tmpdir.join('Dockerfile').write(
        data.format(data_from=data_from, extra_labels=extra_labels)
    )


def mock_workflow(tmpdir, workflow):
    workflow.source = StubSource()
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    builder.base_from_scratch = True
    workflow.builder = flexmock(builder)

    return workflow


def mock_env(tmpdir, docker_tasker, workflow, labels=(), flatpak=False,
             dockerfile_f=mock_dockerfile):
    if not flatpak:
        # flatpak build has no Dockefile
        dockerfile_f(tmpdir, labels)
    workflow = mock_workflow(tmpdir, workflow)
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
    def test_mutual_exclusivity_of_labels(self, tmpdir, docker_tasker, workflow,
                                          labels, expected_fail):
        """Appregistry and operator.bundle labels are mutually exclusive"""
        runner = mock_env(tmpdir, docker_tasker, workflow, labels=labels)
        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert 'only one of labels' in str(e.value)
        else:
            runner.run()

    @pytest.mark.parametrize('from_scratch,multistage,labels,expected_fail', (
        [True, False, ['com.redhat.delivery.operator.bundle=true'], False],
        [False, False, ['com.redhat.delivery.operator.bundle=true'], True],
        [True, True, ['com.redhat.delivery.operator.bundle=true'], True],
        [False, True, ['com.redhat.delivery.operator.bundle=true'], True],
        [True, False, [], False],
        [False, False, [], False],
        [True, True, [], False],
    ))
    def test_operator_bundle_from_scratch(
        self, tmpdir, docker_tasker, workflow, from_scratch, multistage, labels, expected_fail
    ):
        """Operator bundle can be only single stage and FROM scratch"""
        if multistage:
            dockerfile_f = mock_dockerfile_multistage
        else:
            dockerfile_f = mock_dockerfile

        dockerfile_f = partial(dockerfile_f, from_scratch=from_scratch)

        runner = mock_env(
            tmpdir, docker_tasker, workflow,
            dockerfile_f=dockerfile_f, labels=labels
        )
        runner.workflow.builder.base_from_scratch = from_scratch
        runner.workflow.builder.parents_ordered = (
            ['scratch', 'scratch'] if multistage else ['scratch']
        )

        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert 'Operator bundle build can be only' in str(e.value)
        else:
            runner.run()

    def test_flatpak_skip_dockerfile_check(self, tmpdir, docker_tasker, workflow, caplog):
        """Flatpak builds have no user Dockerfiles, dockefile check must be skipped"""
        runner = mock_env(tmpdir, docker_tasker, workflow, flatpak=True)
        runner.run()

        assert 'Skipping Dockerfile checks' in caplog.text
