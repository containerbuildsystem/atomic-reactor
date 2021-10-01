"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
import pytest

from flexmock import flexmock
import yaml

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.pre_add_flatpak_labels import AddFlatpakLabelsPlugin

from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.source import SourceConfig
from atomic_reactor.util import df_parser
from osbs.utils import ImageName


DF_CONTENT = """FROM fedora:latest
CMD sleep 1000
"""
USER_PARAMS = {'flatpak': True}


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = "./"
        self.path = tmpdir

        self.container_yaml_path = os.path.join(tmpdir, 'container.yaml')
        self.config = None


class MockBuilder(object):
    def __init__(self):
        self.base_image = ImageName(repo="qwe", tag="asd")
        self.df_path = None
        self.image_id = "xxx"


def mock_workflow(tmpdir, container_yaml, user_params=None):
    workflow = DockerBuildWorkflow(source=None)
    if user_params is None:
        user_params = USER_PARAMS

    mock_source = MockSource(tmpdir)
    workflow.user_params = user_params
    flexmock(workflow, source=mock_source)

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(container_yaml)
    workflow.source.config = SourceConfig(str(tmpdir))

    df = df_parser(str(tmpdir))
    df.content = DF_CONTENT

    workflow.df_dir = str(tmpdir)
    flexmock(workflow, df_path=df.dockerfile_path)

    return workflow


@pytest.mark.parametrize('labels,expected', [
    (None, None),
    ({}, None),
    ({'a': 'b'}, 'LABEL "a"="b"'),
    ({'a': 'b', 'c': 'd"'}, 'LABEL "a"="b" "c"="d\\""'),
]) # noqa
def test_add_flatpak_labels(tmpdir, user_params, labels, expected):

    if labels is not None:
        data = {'flatpak': {'labels': labels}}
    else:
        data = {}
    container_yaml = yaml.dump(data)

    workflow = mock_workflow(tmpdir, container_yaml)

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': AddFlatpakLabelsPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    assert os.path.exists(workflow.df_path)
    with open(workflow.df_path) as f:
        df = f.read()

    last_line = df.strip().split('\n')[-1]

    if expected:
        assert last_line == expected
    else:
        assert last_line == "CMD sleep 1000"


def test_skip_plugin(tmpdir, caplog, user_params):
    workflow = mock_workflow(tmpdir, '', user_params={})

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': AddFlatpakLabelsPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    assert 'not flatpak build, skipping plugin' in caplog.text
