"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import pytest
from dockerfile_parse import DockerfileParser
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_assert_labels import AssertLabelsPlugin
from atomic_reactor.util import ImageName
from tests.constants import MOCK_SOURCE
from tests.fixtures import docker_tasker


class Y(object):
    pass


class X(object):
    image_id = "xxx"
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")

DF_CONTENT = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
DF_CONTENT_LABELS = DF_CONTENT+'\nLABEL "Name"="rainbow" "Version"="123" "Release"="1"'

@pytest.mark.parametrize('df_content, req_labels, expected', [
    (DF_CONTENT, None, PluginFailedException()),
    (DF_CONTENT_LABELS, None, None),
    (DF_CONTENT_LABELS, ['xyz'], PluginFailedException())
])
def test_assertlabels_plugin(tmpdir, docker_tasker, df_content, req_labels, expected):
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AssertLabelsPlugin.key,
            'args': {'required_labels': req_labels}
        }]
    )

    assert AssertLabelsPlugin.key is not None

    if isinstance(expected, PluginFailedException):
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()
