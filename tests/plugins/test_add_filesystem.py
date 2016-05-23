"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
from textwrap import dedent
from flexmock import flexmock

import re
import json
import pytest
import os.path

try:
    import koji
except ImportError:
    import inspect
    import sys

    # Find out mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji

from dockerfile_parse import DockerfileParser
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.source import VcsInfo
from atomic_reactor import koji_util
from atomic_reactor.plugins import pre_add_filesystem
from tests.constants import (MOCK_SOURCE, DOCKERFILE_GIT, DOCKERFILE_SHA1,
                             MOCK, IMPORTED_IMAGE_ID)
from tests.fixtures import docker_tasker
if MOCK:
    from tests.docker_mock import mock_docker

KOJI_HUB = 'https://koji-hub.com'


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_dockerfile_path(self):
        return self.dockerfile_path, self.path


class X(object):
    image_id = "xxx"
    base_image = ImageName.parse("koji/image-build")
    set_base_image = flexmock()


def mock_koji_session(koji_proxyuser=None, koji_ssl_certs_dir=None,
                      koji_krb_principal=None, koji_krb_keytab=None):
    session = flexmock()
    session.should_receive('buildImageOz').and_return(1234567)
    session.should_receive('taskFinished').and_return(True)
    session.should_receive('getTaskInfo').and_return({
        'state': koji_util.koji.TASK_STATES['CLOSED']
    })
    session.should_receive('listTaskOutput').and_return([
        'fedora-23-1.0.tar.gz',
    ])
    session.should_receive('getTaskChildren').and_return([
        {'id': 1234568},
    ])
    session.should_receive('downloadTaskOutput').and_return('tarball-contents')
    koji_auth_info = {
        'proxyuser': koji_proxyuser,
        'ssl_certs_dir': koji_ssl_certs_dir,
        'krb_principal': koji_krb_principal,
        'krb_keytab': koji_krb_keytab,
    }
    session.should_receive('krb_login').and_return(True)

    (flexmock(koji)
        .should_receive('ClientSession')
        .once()
        .and_return(session))


def mock_image_build_file(tmpdir):
    file_path = os.path.join(tmpdir, 'image-build.conf')
    with open(file_path, 'w') as f:
        f.write(dedent("""\
            [image-build]
            name = fedora-23
            version = 1.0
            target = guest-fedora-23-docker
            install_tree = http://install-tree.com/fedora23/
            arches = x86_64

            format = docker
            distro = Fedora-23
            repo = http://repo.com/fedora/x86_64/os/

            ksurl = git+http://ksrul.com/git/spin-kickstarts.git?fedora23#b232f73e
            ksversion = FEDORA23
            kickstart = fedora-23.ks

            [factory-parameters]
            create_docker_metadata = False

            [ova-options]
            ova_option_1 = ova_option_1_value
            """))

    return file_path


def mock_workflow(tmpdir, dockerfile):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', X)
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)

    df = DockerfileParser(str(tmpdir))
    df.content = dockerfile
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    return workflow


def create_plugin_instance(tmpdir, kwargs=None):
    tasker = flexmock()
    workflow = flexmock()
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', X)
    workflow.builder.source = mock_source
    workflow.source = mock_source

    if kwargs is None:
        kwargs = {}

    return AddFilesystemPlugin(tasker, workflow, KOJI_HUB, **kwargs)


def test_add_filesystem_plugin_generated(tmpdir, docker_tasker):
    if MOCK:
        mock_docker()

    dockerfile = dedent("""\
        FROM koji/image-build
        RUN dnf install -y python-django
        """)
    workflow = mock_workflow(tmpdir, dockerfile)
    mock_koji_session()
    mock_image_build_file(str(tmpdir))

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddFilesystemPlugin.key,
            'args': {'koji_hub': KOJI_HUB}
        }]
    )

    results = runner.run()
    plugin_result = results[AddFilesystemPlugin.key]
    assert 'base-image-id' in plugin_result
    assert plugin_result['base-image-id'] == IMPORTED_IMAGE_ID
    assert 'filesystem-koji-task-id' in plugin_result


@pytest.mark.parametrize(('base_image', 'type_match'), [
    ('koji/image-build', True),
    ('KoJi/ImAgE-bUiLd  \n', True),
    ('spam/bacon', False),
    ('SpAm/BaCon  \n', False),
])
def test_base_image_type(tmpdir, base_image, type_match):
    plugin = create_plugin_instance(tmpdir)
    assert plugin.is_image_build_type(base_image) == type_match


def test_image_build_file_parse(tmpdir):
    plugin = create_plugin_instance(tmpdir)
    file_name = mock_image_build_file(str(tmpdir))
    image_name, config, opts = plugin.parse_image_build_config(file_name)
    assert image_name == 'fedora-23'
    assert config == [
        'fedora-23',
        '1.0',
        ['x86_64'],
        'guest-fedora-23-docker',
        'http://install-tree.com/fedora23/'
    ]
    assert opts['opts'] == {
        'disk_size': 10,
        'distro': 'Fedora-23',
        'factory_parameter': [('create_docker_metadata', 'False')],
        'ova_option': ['ova_option_1=ova_option_1_value'],
        'format': ['docker'],
        'kickstart': 'fedora-23.ks',
        'ksurl': 'git+http://ksrul.com/git/spin-kickstarts.git?fedora23#b232f73e',
        'ksversion': 'FEDORA23',
        'repo': ['http://repo.com/fedora/x86_64/os/'],
    }


def test_build_filesystem_missing_conf(tmpdir):
    plugin = create_plugin_instance(tmpdir)
    with pytest.raises(RuntimeError) as exc:
        plugin.build_filesystem('image-build.conf')
    assert 'Image build configuration file not found' in str(exc)


@pytest.mark.parametrize('pattern', [
    'fedora-23-spam-.tar',
    'fedora-23-spam-.tar.gz',
    'fedora-23-spam-.tar.bz2',
    'fedora-23-spam-.tar.xz',
])
def test_build_filesystem_from_task_id(tmpdir, pattern):
    task_id = 987654321
    plugin = create_plugin_instance(tmpdir, {'from_task_id': task_id})
    plugin.session = flexmock()
    file_name = mock_image_build_file(str(tmpdir))
    task_id, filesystem_regex = plugin.build_filesystem('image-build.conf')
    assert task_id == task_id
    match = filesystem_regex.match(pattern)
    assert match is not None
    assert match.group(0) == pattern
