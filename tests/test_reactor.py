"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import os
import re
import inspect

import pytest

from atomic_reactor import constants as dconstants

from atomic_reactor.core import DockerTasker
from atomic_reactor.outer import PrivilegedBuildManager, DockerhostBuildManager
from atomic_reactor.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT, DOCKERFILE_SUBDIR_PATH,\
        DOCKERFILE_ERROR_BUILD_PATH, TEST_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker


with_all_sources = pytest.mark.parametrize('source_params', [
    {'provider': 'git', 'uri': 'https://github.com/fedora-cloud/Fedora-Dockerfiles.git',
     'dockerfile_path': 'ssh/'},
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_SUBDIR_PATH,
     'dockerfile_path': 'ssh/'},
])


def assert_source_from_path_mounted_ok(caplog, tmpdir):
    # assert that build json has properly modified source uri
    container_uri = 'file://' + os.path.join(dconstants.CONTAINER_SHARE_PATH,
            dconstants.CONTAINER_SHARE_SOURCE_SUBDIR)
    container_uri_re = re.compile(r'build json mounted in container.*"uri": "%s"' % container_uri)

    # verify that source code was copied in - actually only verifies
    #  that source dir has been created
    source_exists = "source path is '%s'" %\
            os.path.join(tmpdir, dconstants.CONTAINER_SHARE_SOURCE_SUBDIR)
    assert any([container_uri_re.search(l.getMessage()) for l in caplog.records()])
    assert source_exists in [l.getMessage() for l in caplog.records()]

    # make sure that double source (i.e. source/source) is not created
    source_path_is_re = re.compile(r"source path is '.*/source/source'")
    assert not any([source_path_is_re.search(l.getMessage()) for l in caplog.records()])


@with_all_sources
def test_hostdocker_build(caplog, source_params):
    if MOCK:
        mock_docker()

    image_name = ImageName(repo="atomic-reactor-test-ssh-image")
    remote_image = image_name.copy()
    remote_image.registry = LOCALHOST_REGISTRY
    m = DockerhostBuildManager("buildroot-dh-fedora", {
        "source": source_params,
        "image": remote_image.to_str(),
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
    })
    results = m.build()
    dt = DockerTasker()
    dt.pull_image(remote_image, insecure=True)

    if source_params['provider'] == 'path':
        assert_source_from_path_mounted_ok(caplog, m.temp_dir)

    assert len(results.build_logs) > 0
    #assert re.search(r'build json mounted in container .+"uri": %s' %
    #        os.path.join(dconstants.CONTAINER_SHARE_PATH, 'source'))
    # assert isinstance(results.built_img_inspect, dict)
    # assert len(results.built_img_inspect.items()) > 0
    # assert isinstance(results.built_img_info, dict)
    # assert len(results.built_img_info.items()) > 0
    # assert isinstance(results.base_img_info, dict)
    # assert len(results.base_img_info.items()) > 0
    # assert len(results.base_plugins_output) > 0
    # assert len(results.built_img_plugins_output) > 0
    dt.remove_container(results.container_id)
    dt.remove_image(remote_image)


@pytest.mark.parametrize('source_params', [
    {'provider': 'git', 'uri': DOCKERFILE_GIT, 'provider_params': {'git_commit': 'error-build'}},
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_ERROR_BUILD_PATH},
])
def test_hostdocker_error_build(source_params):
    if MOCK:
        mock_docker(wait_should_fail=True)

    image_name = TEST_IMAGE
    m = DockerhostBuildManager("buildroot-dh-fedora", {
        "source": {
            "provider": "git",
            "uri": DOCKERFILE_GIT,
            "provider_params": {"git_commit": "error-build"}
        },
        "image": image_name,
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
        })
    results = m.build()
    dt = DockerTasker()
    assert len(results.build_logs) > 0
    assert results.return_code != 0
    dt.remove_container(results.container_id)


@with_all_sources
def test_privileged_gitrepo_build(caplog, source_params):
    if MOCK:
        mock_docker()

    image_name = ImageName(repo="atomic-reactor-test-ssh-image")
    remote_image = image_name.copy()
    remote_image.registry = LOCALHOST_REGISTRY
    m = PrivilegedBuildManager("buildroot-fedora", {
        "source": source_params,
        "image": remote_image.to_str(),
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
    })
    results = m.build()
    dt = DockerTasker()
    dt.pull_image(remote_image, insecure=True)

    if source_params['provider'] == 'path':
        assert_source_from_path_mounted_ok(caplog, m.temp_dir)

    assert len(results.build_logs) > 0
    # assert isinstance(results.built_img_inspect, dict)
    # assert len(results.built_img_inspect.items()) > 0
    # assert isinstance(results.built_img_info, dict)
    # assert len(results.built_img_info.items()) > 0
    # assert isinstance(results.base_img_info, dict)
    # assert len(results.base_img_info.items()) > 0
    # assert len(results.base_plugins_output) > 0
    # assert len(results.built_img_plugins_output) > 0
    dt.remove_container(results.container_id)
    dt.remove_image(remote_image)


@with_all_sources
def test_privileged_build(caplog, source_params):
    if MOCK:
        mock_docker()

    image_name = ImageName(repo=TEST_IMAGE)
    remote_image = image_name.copy()
    remote_image.registry = LOCALHOST_REGISTRY
    m = PrivilegedBuildManager("buildroot-fedora", {
        "source": source_params,
        "image": remote_image.to_str(),
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
    })
    results = m.build()
    dt = DockerTasker()
    dt.pull_image(remote_image, insecure=True)

    if source_params['provider'] == 'path':
        assert_source_from_path_mounted_ok(caplog, m.temp_dir)

    assert len(results.build_logs) > 0
    # assert isinstance(results.built_img_inspect, dict)
    # assert len(results.built_img_inspect.items()) > 0
    # assert isinstance(results.built_img_info, dict)
    # assert len(results.built_img_info.items()) > 0
    # assert isinstance(results.base_img_info, dict)
    # assert len(results.base_img_info.items()) > 0
    # assert len(results.base_plugins_output) > 0
    # assert len(results.built_img_plugins_output) > 0
    dt.remove_container(results.container_id)
    dt.remove_image(remote_image)


def test_if_all_versions_match():
    def read_version(fp, regex):
        with open(fp, "r") as fd:
            content = fd.read()
            found = re.findall(regex, content)
            if len(found) == 1:
                return found[0]
            else:
                raise Exception("Version not found!")
    import atomic_reactor
    from atomic_reactor import __version__
    fp = inspect.getfile(atomic_reactor)
    project_dir = os.path.dirname(os.path.dirname(fp))
    specfile = os.path.join(project_dir, "atomic-reactor.spec")
    setup_py = os.path.join(project_dir, "setup.py")
    spec_version = read_version(specfile, r"\nVersion:\s*(.+?)\s*\n")
    setup_py_version = read_version(setup_py, r"version=['\"](.+)['\"]")
    assert spec_version == __version__
    assert setup_py_version == __version__
