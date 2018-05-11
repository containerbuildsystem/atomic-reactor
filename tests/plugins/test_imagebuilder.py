"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import subprocess
import time
from dockerfile_parse import DockerfileParser

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.build_imagebuilder import ImagebuilderPlugin
from atomic_reactor.build import InsideBuilder, BuildResult
from atomic_reactor.util import ImageName
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS

from flexmock import flexmock
from six import StringIO
import pytest
from tests.constants import MOCK_SOURCE


class MockDocker(object):
    def history(self, name):
        return []


class MockDockerTasker(object):
    def __init__(self):
        self.d = MockDocker()

    def inspect_image(self, name):
        return {}

    def build_image_from_path(self):
        return True


class MockInsideBuilder(object):
    def __init__(self, failed=False, image_id=None):
        self.tasker = MockDockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = image_id or 'asd'
        self.image = ImageName.parse('image')
        self.failed = failed
        self.df_path = 'some'
        self.df_dir = 'some'

        def simplegen(x, y):
            yield "some\u2018".encode('utf-8')
        flexmock(self.tasker, build_image_from_path=simplegen)

    @property
    def source(self):
        return flexmock(
            dockerfile_path='/',
            path='/tmp',
            config=flexmock(override_image_build='imagebuilder'),
        )

    def pull_base_image(self, source_registry, insecure=False):
        pass

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return {INSPECT_ROOTFS: {INSPECT_ROOTFS_LAYERS: []}}

    def ensure_not_built(self):
        pass


@pytest.mark.parametrize('image_id', ['sha256:12345', '12345'])
def test_popen_cmd(image_id):
    """
    tests imagebuilder build plugin working
    """
    flexmock(DockerfileParser, content='df_content')
    fake_builder = MockInsideBuilder(image_id=image_id)
    flexmock(InsideBuilder).new_instances(fake_builder)

    cmd_output = "spam spam spam spam spam spam spam baked beans spam spam spam and spam"
    real_popen = subprocess.Popen
    flexmock(subprocess, Popen=lambda *_, **kw: real_popen(['echo', '-n', cmd_output], **kw))
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.build_docker_image()

    assert isinstance(workflow.buildstep_result['imagebuilder'], BuildResult)
    assert workflow.build_result == workflow.buildstep_result['imagebuilder']
    assert not workflow.build_result.is_failed()
    assert workflow.build_result.image_id.startswith('sha256:')
    assert workflow.build_result.image_id.count(':') == 1
    assert workflow.build_result.skip_layer_squash
    assert cmd_output in workflow.build_result.logs


def test_failed_build():
    cmd_output = "spam spam spam spam spam spam spam baked beans spam spam spam and spam"
    cmd_error = "Nobody expects the Spanish Inquisition!"
    ib_process = flexmock(
        stdout=StringIO(cmd_output),
        stderr=StringIO(cmd_error),
        poll=lambda: True,
        returncode=1,
    )
    flexmock(subprocess).should_receive('Popen').and_return(ib_process)

    flexmock(DockerfileParser, content='df_content')
    fake_builder = MockInsideBuilder(image_id='abcde')
    flexmock(InsideBuilder).new_instances(fake_builder)
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    with pytest.raises(PluginFailedException):
        workflow.build_docker_image()

    assert isinstance(workflow.build_result, BuildResult)
    assert workflow.build_result.is_failed()
    assert cmd_output in workflow.build_result.logs
    assert cmd_error in workflow.build_result.logs
    assert cmd_error in workflow.build_result.fail_reason
    assert workflow.build_result.skip_layer_squash is False


def test_sleep_await_output():
    ib_process = flexmock(
        stdout=StringIO(""),
        stderr=StringIO(""),
        poll=lambda: None,
        returncode=1,
    )
    flexmock(subprocess).should_receive('Popen').and_return(ib_process)
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockInsideBuilder(image_id='abcde')

    class Spam(Exception):
        pass

    flexmock(time).should_receive('sleep').with_args(0.1).and_raise(Spam)
    with pytest.raises(Spam):
        ImagebuilderPlugin(None, workflow).run()
