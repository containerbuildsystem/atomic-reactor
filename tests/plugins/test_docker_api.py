"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from dockerfile_parse import DockerfileParser

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.build import InsideBuilder, BuildResult
from atomic_reactor.util import ImageName, CommandResult
from atomic_reactor.inner import DockerBuildWorkflow

from tests.docker_mock import mock_docker
from flexmock import flexmock
import pytest
from tests.constants import MOCK_SOURCE


class MockDockerTasker(object):
    def inspect_image(self, name):
        return {}

    def build_image_from_path(self):
        return True

class X(object):
    pass


class MockInsideBuilder(object):
    def __init__(self, failed=False):
        self.tasker = MockDockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'asd'
        self.image = 'image'
        self.failed = failed
        self.df_path = 'some'
        self.df_dir = 'some'

        def simplegen(x, y):
            yield "some\u2018".encode('utf-8')
        flexmock(self.tasker, build_image_from_path=simplegen)

    @property
    def source(self):
        result = X()
        setattr(result, 'dockerfile_path', '/')
        setattr(result, 'path', '/tmp')
        return result

    def pull_base_image(self, source_registry, insecure=False):
        pass

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return None

    def ensure_not_built(self):
        pass

@pytest.mark.parametrize('is_failed', [
    True,
    False,
])
def test_build(is_failed):
    """
    tests docker build api plugin working
    """
    flexmock(DockerfileParser, content='df_content')
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    flexmock(CommandResult).should_receive('is_failed').and_return(is_failed)
    error_detail = 'error detail'
    if is_failed:
        flexmock(CommandResult, error_detail=error_detail)
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()
    else:
        workflow.build_docker_image()
   
    assert isinstance(workflow.buildstep_result['docker_api'], BuildResult)
    assert workflow.build_result == workflow.buildstep_result['docker_api']
    assert workflow.build_result.is_failed() == is_failed

    if is_failed:
        assert workflow.build_result.fail_reason == error_detail
        assert error_detail in workflow.plugins_errors['docker_api']
