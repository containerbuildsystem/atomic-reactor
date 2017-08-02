"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import os
import logging
from collections import namedtuple

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.pulp_util import PulpHandler
from atomic_reactor.util import ImageName

import pytest
from flexmock import flexmock
from tests.constants import INPUT_IMAGE, SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker

try:
    import sys
    if sys.version_info.major > 2:
        # importing dockpulp in Python 3 causes SyntaxError
        raise ImportError

    import dockpulp
except (ImportError):
    dockpulp = None


PulpRepo = namedtuple('PulpRepo', ['registry_id', 'tags'])


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def prepare(testfile="test-image", check_repo_retval=0):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, testfile)
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', X())
    setattr(workflow.tag_conf, 'images', [ImageName(repo="image-name1"),
                                          ImageName(namespace="prefix",
                                                    repo="image-name2"),
                                          ImageName(repo="image-name3", tag="asd")])

    # Mock dockpulp and docker
    dockpulp.Pulp = flexmock(dockpulp.Pulp)
    dockpulp.Pulp.registry = 'registry.example.com'
    (flexmock(dockpulp.imgutils).should_receive('get_metadata')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_manifest')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_versions')
     .with_args(object)
     .and_return({'id': '1.6.0'}))
    (flexmock(dockpulp.imgutils).should_receive('check_repo')
     .and_return(check_repo_retval))
    (flexmock(dockpulp.Pulp)
     .should_receive('set_certs')
     .with_args(object, object))
    (flexmock(dockpulp.Pulp)
     .should_receive('login')
     .with_args(object, object))
    (flexmock(dockpulp.Pulp)
     .should_receive('getRepos')
     .with_args(list, fields=list)
     .and_return([
         {"id": "redhat-image-name1"},
         {"id": "redhat-prefix-image-name2"}
      ]))
    (flexmock(dockpulp.Pulp)
     .should_receive('createRepo'))

    mock_docker()
    return tasker, workflow


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_get_tar_metadata():
    log = logging.getLogger("tests.test_pulp_util")
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'

    tasker, workflow = prepare(testfile)
    handler = PulpHandler(workflow, pulp_registry_name, log)

    expected = ("foo", ["foo"])
    assert handler.get_tar_metadata(testfile) == expected


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(("check_repo_retval", "should_raise"), [
    (3, True),
    (2, True),
    (1, True),
    (0, False),
])
def test_check_file(tmpdir, check_repo_retval, should_raise):
    log = logging.getLogger("tests.test_pulp_util")
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'

    _, workflow = prepare(testfile, check_repo_retval)
    handler = PulpHandler(workflow, pulp_registry_name, log)

    if should_raise:
        with pytest.raises(Exception):
            handler.check_file(testfile)
        return
    handler.check_file(testfile)


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(("user", "secret_path", "secret_env", "invalid"), [
    (None, None, False, False),
    ("user", None, False, False),
    (None, None, True, False),
    (None, True, None, False),
    (None, None, True, True),
    (None, True, None, True),
])
def test_create_dockpulp_and_repos(tmpdir, user, secret_path, secret_env, invalid,
                                   monkeypatch):
    log = logging.getLogger("tests.test_pulp_util")
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'

    if secret_env:
        monkeypatch.setenv('SOURCE_SECRET_PATH', str(tmpdir))
    if secret_path:
        secret_path = str(tmpdir)
    if not invalid:
        with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
            cer.write("pulp certificate\n")
        with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
            key.write("pulp key\n")

    _, workflow = prepare(testfile)
    image_names = workflow.tag_conf.images[:]
    handler = PulpHandler(workflow, pulp_registry_name, log, pulp_secret_path=secret_path,
                          username=user, password=user)

    if invalid:
        with pytest.raises(Exception):
            handler.create_dockpulp_and_repos(image_names)
        return

    expected = {
        'redhat-image-name1': PulpRepo(registry_id='image-name1', tags=['latest']),
        'redhat-image-name3': PulpRepo(registry_id='image-name3', tags=['asd']),
        'redhat-prefix-image-name2': PulpRepo(registry_id='prefix/image-name2', tags=['latest'])
    }

    assert handler.create_dockpulp_and_repos(image_names) == expected


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_get_registry_hostname():
    log = logging.getLogger("tests.test_pulp_util")
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'

    _, workflow = prepare(testfile)
    handler = PulpHandler(workflow, pulp_registry_name, log)
    handler.create_dockpulp_and_repos([])
    assert handler.get_registry_hostname() == pulp_registry_name


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_get_pulp_instance():
    log = logging.getLogger("tests.test_pulp_util")
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'

    _, workflow = prepare(testfile)
    handler = PulpHandler(workflow, pulp_registry_name, log)
    assert handler.get_pulp_instance() == pulp_registry_name
