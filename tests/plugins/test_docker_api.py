"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import docker
import requests

from dockerfile_parse import DockerfileParser

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.build import InsideBuilder, BuildResult
from atomic_reactor.util import ImageName, CommandResult
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS

from tests.docker_mock import mock_docker
from flexmock import flexmock
import pytest
from tests.constants import MOCK_SOURCE


def mock_docker_tasker(docker_tasker):
    def simplegen(x, y):
        yield "some\u2018".encode('utf-8')

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('inspect_image')
     .and_return({}))

    flexmock(docker_tasker.tasker, build_image_from_path=simplegen)

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('history')
     .and_return([]))


class MockInsideBuilder(object):
    def __init__(self, failed=False, image_id=None):
        self.tasker = None
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = image_id or 'asd'
        self.image = 'image'
        self.failed = failed
        self.df_path = 'some'
        self.df_dir = 'some'

    @property
    def source(self):
        return flexmock(
            dockerfile_path='/',
            path='/tmp',
            config=flexmock(image_build_method='docker_api'),
        )

    def pull_base_image(self, source_registry, insecure=False):
        pass

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return {INSPECT_ROOTFS: {INSPECT_ROOTFS_LAYERS: []}}

    def ensure_not_built(self):
        pass


@pytest.mark.parametrize('is_failed', [
    True,
    False,
])
@pytest.mark.parametrize('image_id', ['sha256:12345', '12345'])
def test_build(docker_tasker, is_failed, image_id):
    """
    tests docker build api plugin working
    """
    flexmock(DockerfileParser, content='df_content')
    mock_docker()
    fake_builder = MockInsideBuilder(image_id=image_id)
    fake_builder.tasker = docker_tasker
    mock_docker_tasker(docker_tasker)
    flexmock(InsideBuilder).new_instances(fake_builder)

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    flexmock(CommandResult).should_receive('is_failed').and_return(is_failed)
    error = "error message"
    error_detail = "{u'message': u\"%s\"}" % error
    if is_failed:
        flexmock(CommandResult, error=error, error_detail=error_detail)
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()
    else:
        workflow.build_docker_image()

    assert isinstance(workflow.buildstep_result['docker_api'], BuildResult)
    assert workflow.build_result == workflow.buildstep_result['docker_api']
    assert workflow.build_result.is_failed() == is_failed

    if is_failed:
        assert workflow.build_result.fail_reason == error
        assert '\\' not in workflow.plugins_errors['docker_api']
        assert error in workflow.plugins_errors['docker_api']
    else:
        assert workflow.build_result.image_id.startswith('sha256:')
        assert workflow.build_result.image_id.count(':') == 1


def test_syntax_error(docker_tasker):
    """
    tests reporting of syntax errors
    """
    flexmock(DockerfileParser, content='df_content')
    mock_docker()
    fake_builder = MockInsideBuilder()
    fake_builder.tasker = docker_tasker
    mock_docker_tasker(docker_tasker)

    def raise_exc(*args, **kwargs):
        explanation = ("Syntax error - can't find = in \"CMD\". "
                       "Must be of the form: name=value")
        http_error = requests.HTTPError('500 Server Error')
        raise docker.errors.APIError(message='foo',
                                     response=http_error,
                                     explanation=explanation)
        yield {}    # pylint: disable=unreachable; this needs to be a generator

    fake_builder.tasker.build_image_from_path = raise_exc
    flexmock(InsideBuilder).new_instances(fake_builder)
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    with pytest.raises(PluginFailedException):
        workflow.build_docker_image()

    assert isinstance(workflow.buildstep_result['docker_api'], BuildResult)
    assert workflow.build_result == workflow.buildstep_result['docker_api']
    assert workflow.build_result.is_failed()
    assert "Syntax error" in workflow.build_result.fail_reason
