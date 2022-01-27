"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path

import pytest

from flexmock import flexmock
import yaml

from atomic_reactor.plugins.pre_add_flatpak_labels import AddFlatpakLabelsPlugin

from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.source import SourceConfig
from osbs.utils import ImageName


DF_CONTENT = """FROM fedora:latest
CMD sleep 1000
"""
USER_PARAMS = {'flatpak': True}


class MockSource(object):
    def __init__(self, source_dir: Path):
        self.dockerfile_path = "./"
        self.path = str(source_dir)

        self.container_yaml_path = str(source_dir / 'container.yaml')
        self.config = None


class MockBuilder(object):
    def __init__(self):
        self.base_image = ImageName(repo="qwe", tag="asd")
        self.df_path = None
        self.image_id = "xxx"


def mock_workflow(workflow, source_dir: Path, container_yaml, user_params=None):
    if user_params is None:
        user_params = USER_PARAMS

    if user_params is None:
        workflow.user_params.update(USER_PARAMS)
    else:
        workflow.user_params.update(user_params)

    mock_source = MockSource(source_dir)
    flexmock(workflow, source=mock_source)

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(container_yaml)
    workflow.source.config = SourceConfig(str(source_dir))

    (source_dir / "Dockerfile").write_text(DF_CONTENT)

    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)


@pytest.mark.parametrize('labels,expected', [
    (None, None),
    ({}, None),
    ({'a': 'b'}, 'LABEL "a"="b"'),
    ({'a': 'b', 'c': 'd"'}, 'LABEL "a"="b" "c"="d\\""'),
]) # noqa
def test_add_flatpak_labels(workflow, source_dir, labels, expected):

    if labels is not None:
        data = {'flatpak': {'labels': labels}}
    else:
        data = {}
    container_yaml = yaml.dump(data)

    mock_workflow(workflow, source_dir, container_yaml)

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': AddFlatpakLabelsPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    def check_last_line_in_df(build_dir):
        lines = build_dir.dockerfile_path.read_text().splitlines()
        if expected:
            assert lines[-1] == expected
        else:
            assert lines[-1] == "CMD sleep 1000"

    workflow.build_dir.for_each_platform(check_last_line_in_df)


def test_skip_plugin(workflow, source_dir, caplog, user_params):
    mock_workflow(workflow, source_dir, '', user_params={})

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': AddFlatpakLabelsPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    assert 'not flatpak build, skipping plugin' in caplog.text
