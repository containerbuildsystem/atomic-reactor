# -*- coding: utf-8 -*-

"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import subprocess
from dockerfile_parse import DockerfileParser

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.build import InsideBuilder, BuildResult
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS
from osbs.utils import ImageName

from flexmock import flexmock
from six import StringIO
import pytest


def mock_docker_tasker(docker_tasker):
    def simplegen(x, y, buildargs=None):
        yield "some\u2018".encode('utf-8')

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('inspect_image')
     .and_return({}))

    flexmock(docker_tasker.tasker, build_image_from_path=simplegen)

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('history')
     .and_return([]))

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('get_image')
     .and_return(flexmock(data="image data")))


class MockInsideBuilder(object):
    def __init__(self, failed=False, image_id=None):
        self.tasker = None
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = image_id or 'asd'
        self.image = ImageName.parse('image')
        self.failed = failed
        self.df_path = 'some'
        self.df_dir = 'some'
        self.buildargs = {'arg1': 'argval1'}

    @property
    def source(self):
        return flexmock(
            dockerfile_path='/',
            path='/tmp',
            config=flexmock(image_build_method='imagebuilder'),
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
def test_popen_cmd(docker_tasker, workflow, image_id):
    """
    tests imagebuilder build plugin working
    """
    flexmock(DockerfileParser, content='df_content')
    fake_builder = MockInsideBuilder(image_id=image_id)
    fake_builder.tasker = docker_tasker
    mock_docker_tasker(docker_tasker)
    flexmock(InsideBuilder).new_instances(fake_builder)

    real_popen = subprocess.Popen

    process_args = ['imagebuilder', '-t', fake_builder.image.to_str()]
    for argname, argval in fake_builder.buildargs.items():
        process_args.append('--build-arg')
        process_args.append('%s=%s' % (argname, argval))
    process_args.append(fake_builder.df_dir)

    flexmock(subprocess, Popen=lambda *args, **kw: real_popen(['echo', '-n', str(args)], **kw))
    workflow.build_docker_image()

    assert isinstance(workflow.buildstep_result['imagebuilder'], BuildResult)
    assert workflow.build_result == workflow.buildstep_result['imagebuilder']
    assert not workflow.build_result.is_failed()
    assert workflow.build_result.image_id.startswith('sha256:')
    assert workflow.build_result.image_id.count(':') == 1
    assert workflow.build_result.skip_layer_squash
    assert len(workflow.exported_image_sequence) == 1
    assert str((process_args, )) in workflow.build_result.logs


def test_failed_build(workflow):
    cmd_output = "spam spam spam spam spam spam spam baked beans spam spam spam and spam\n"
    cmd_error = "Nobody expects the Spanish Inquisition!\n"
    ib_process = flexmock(
        stdout=StringIO(cmd_output + cmd_error),
        poll=lambda: True,
        returncode=1,
    )
    flexmock(subprocess).should_receive('Popen').and_return(ib_process)

    flexmock(DockerfileParser, content='df_content')
    fake_builder = MockInsideBuilder(image_id='abcde')
    flexmock(InsideBuilder).new_instances(fake_builder)
    with pytest.raises(PluginFailedException):
        workflow.build_docker_image()

    assert isinstance(workflow.build_result, BuildResult)
    assert workflow.build_result.is_failed()
    assert cmd_output in workflow.build_result.logs
    assert cmd_error in workflow.build_result.logs
    assert cmd_error in workflow.build_result.fail_reason
    assert workflow.build_result.skip_layer_squash is False
