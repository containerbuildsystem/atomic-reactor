"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

import os
import logging
from collections import namedtuple

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.pulp_util import PulpHandler, LockedPulpRepository
from atomic_reactor.util import ImageName
from atomic_reactor.constants import LOCKEDPULPREPOSITORY_RETRIES

import pytest
import copy
import time
from flexmock import flexmock
from tests.constants import SOURCE, MOCK
from tests.stubs import StubInsideBuilder, StubTagConf, StubSource
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


def prepare(testfile="test-image", check_repo_retval=0):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, testfile)
    workflow.source = StubSource()
    workflow.builder = StubInsideBuilder()
    workflow.tag_conf = StubTagConf().set_images([
        ImageName(repo="image-name1"),
        ImageName(namespace="prefix",
                  repo="image-name2"),
        ImageName(repo="image-name3",
                  tag="asd"),
    ])

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
     .with_args(list, fields=list, distributors=True)
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


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(("auto_publish"), [
    (True),
    (False),
])
@pytest.mark.parametrize(("unsupported"), [
    (True),
    (False)
])
def test_ensure_repos(auto_publish, unsupported):
    dist_data = [
          {
            "repo_id": "redhat-myproject-hello-world",
            "auto_publish": auto_publish,
          }
        ]
    mock_get_data = [{"id": "redhat-myproject-hello-world"}]
    data_with_dist = copy.deepcopy(mock_get_data)
    data_with_dist[0]["distributors"] = dist_data

    log = logging.getLogger("tests.test_pulp_util")
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'

    _, workflow = prepare(testfile)

    if unsupported:
        (flexmock(dockpulp.Pulp)
         .should_receive('getRepos')
         .with_args(['redhat-myproject-hello-world'], fields=['id'], distributors=True)
         .and_raise(TypeError)
         .once())
    else:
        (flexmock(dockpulp.Pulp)
         .should_receive('getRepos')
         .with_args(['redhat-myproject-hello-world'], fields=['id'], distributors=True)
         .and_return(data_with_dist)
         .once())

    (flexmock(dockpulp.Pulp)
     .should_receive('getRepos')
     .with_args(['redhat-myproject-hello-world'], fields=['id'])
     .and_return(mock_get_data)
     .times(1 if unsupported else 0))

    (flexmock(dockpulp.Pulp)
     .should_receive('updateRepo')
     .with_args(data_with_dist[0]["id"], {'auto_publish': False})
     .times(1 if auto_publish and not unsupported else 0))

    image_names = [ImageName(repo="myproject-hello-world")]
    handler = PulpHandler(workflow, pulp_registry_name, log)
    handler.create_dockpulp_and_repos(image_names)


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(("unsupported"), [
    (True),
    (False)
])
def test_upload(unsupported, caplog):
    log = logging.getLogger("tests.test_pulp_util")
    caplog.set_level(logging.DEBUG, logger='tests.test_pulp_util')
    pulp_registry_name = 'registry.example.com'
    testfile = 'foo'
    upload_file = 'test_file'
    repo_id = 'redhat-myproject-hello-world'

    _, workflow = prepare(testfile)
    image_names = [ImageName(repo="myproject-hello-world")]
    handler = PulpHandler(workflow, pulp_registry_name, log)
    handler.create_dockpulp_and_repos(image_names)

    if unsupported:
        (flexmock(dockpulp.Pulp)
         .should_receive('upload')
         .with_args(upload_file, repo_id)
         .and_raise(TypeError)
         .once()
         .ordered())
        (flexmock(dockpulp.Pulp)
         .should_receive('upload')
         .with_args(upload_file)
         .and_return(True)
         .once()
         .ordered())
    else:
        (flexmock(dockpulp.Pulp)
         .should_receive('upload')
         .with_args(upload_file, repo_id)
         .and_return(False)
         .once())

    handler.upload(upload_file, repo_id)

    assert "Uploading %s to %s" % (upload_file, repo_id) in caplog.text

    if unsupported:
        assert "Falling back to uploading %s to redhat-everything repo" %\
               upload_file in caplog.text


class TestLockedPulpRepository(object):
    def test_lock_bad_params(self):
        """
        Should fail if 'prefix' is not a string
        """
        pulp = flexmock()
        pulp.should_receive('createRepo').never()
        pulp.should_receive('deleteRepo').never()
        with pytest.raises(Exception):
            with LockedPulpRepository(pulp, 'redhat-repo', prefix=None):
                pass

    @pytest.mark.parametrize(('repo_id', 'kwargs', 'expected_repo_id'), [
        ('redhat-repo', {}, 'lock-redhat-repo'),
        ('redhat-repo', {'prefix': 'locked-'}, 'locked-redhat-repo'),
    ])
    def test_lock_no_errors(self, repo_id, kwargs, expected_repo_id):
        pulp = flexmock()

        # First it should call createRepo()
        createRepo_kwargs = {
            'registry_id': object,  # don't care what this is
            'is_origin': True,
            'prefix_with': kwargs.get('prefix', 'lock-'),
        }

        (pulp
         .should_receive('createRepo')
         .with_args(repo_id, object, **createRepo_kwargs)
         .once()
         .ordered())

        with LockedPulpRepository(pulp, repo_id, **kwargs) as lock:
            assert isinstance(lock, LockedPulpRepository)

            # Next, deleteRepo() -- but with the full repository id.
            # Yes, dockpulp really works like this.
            (pulp
             .should_receive('deleteRepo')
             .with_args(expected_repo_id)
             .once()
             .ordered())

    @pytest.mark.skipif(dockpulp is None,
                        reason='dockpulp module not available')
    def test_lock_exception_create(self):
        pulp = flexmock()
        pulp.should_receive('createRepo').and_raise(RuntimeError).once()

        with pytest.raises(RuntimeError):
            with LockedPulpRepository(pulp, 'redhat-repo'):
                assert False, "Should not get here"

    @pytest.mark.skipif(dockpulp is None,
                        reason='dockpulp module not available')
    def test_lock_exception_delete_ignored(self):
        """
        dockpulp.errors.DockPulpError should be ignored when exiting the
        context manager.
        """
        pulp = flexmock()
        pulp.should_receive('createRepo').once().ordered()
        (pulp
         .should_receive('deleteRepo')
         .and_raise(dockpulp.errors.DockPulpError('error deleting'))
         .once()
         .ordered())

        with LockedPulpRepository(pulp, 'redhat-repo'):
            pass

    @pytest.mark.skipif(dockpulp is None,
                        reason='dockpulp module not available')
    def test_lock_exception_delete_raise(self):
        """
        Other exceptions from deleting should be propagated when exiting
        the context manager.
        """
        pulp = flexmock()
        pulp.should_receive('createRepo').once().ordered()
        (pulp
         .should_receive('deleteRepo')
         .and_raise(RuntimeError)
         .once()
         .ordered())

        entered = False
        with pytest.raises(RuntimeError):
            with LockedPulpRepository(pulp, 'redhat-repo'):
                entered = True

        assert entered

    @pytest.mark.skipif(dockpulp is None,
                        reason='dockpulp module not available')
    def test_lock_exception_delete_propagate(self):
        """
        dockpulp.errors.DockPulpError raised within the context should be
        propagated up the call stack.
        """
        pulp = flexmock()
        pulp.should_receive('createRepo').once().ordered()
        pulp.should_receive('deleteRepo').once().ordered()
        entered = False
        with pytest.raises(dockpulp.errors.DockPulpError):
            with LockedPulpRepository(pulp, 'redhat-repo'):
                entered = True
                raise dockpulp.errors.DockPulpError('random error')

        assert entered

    @pytest.mark.skipif(dockpulp is None,
                        reason='dockpulp module not available')
    @pytest.mark.parametrize('failures', [2, 9])
    def test_lock_retry(self, failures):
        # Don't actually wait when retrying
        flexmock(time).should_receive('sleep')

        pulp = flexmock()
        expectation = pulp.should_receive('createRepo').times(failures + 1)
        exc = dockpulp.errors.DockPulpError('error creating')
        for i in range(failures):
            expectation = expectation.and_raise(exc).ordered()

        expectation.and_return(None).ordered

        pulp.should_receive('deleteRepo').once().ordered()

        with LockedPulpRepository(pulp, 'redhat-repo'):
            pass

    @pytest.mark.skipif(dockpulp is None,
                        reason='dockpulp module not available')
    def test_lock_break(self):
        class Elapsed(object):
            def __init__(self):
                self.seconds = 0

            def sleep(self, s):
                self.seconds += s

        # Don't actually wait when retrying, just measure time
        elapsed = Elapsed()
        flexmock(time).should_receive('sleep').replace_with(elapsed.sleep)

        pulp = flexmock()
        (pulp
         .should_receive('createRepo')
         .times(LOCKEDPULPREPOSITORY_RETRIES + 1)
         .and_raise(dockpulp.errors.DockPulpError('error creating')))

        pulp.should_receive('deleteRepo').once().ordered()

        with LockedPulpRepository(pulp, 'redhat-repo'):
            # Should wait at least an hour before breaking the lock
            assert elapsed.seconds > 60 * 60
