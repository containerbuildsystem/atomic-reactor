"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
import docker
from flexmock import flexmock

from atomic_reactor.constants import DOCKER_SOCKET_PATH
from atomic_reactor.util import ImageName
from tests.constants import COMMAND

old_ope = os.path.exists

mock_containers = \
    [{'Created': 1430292310,
      'Image': 'fedora',
      'Names': ['/goofy_mayer'],
      'Command': '/bin/bash',
      'Id': 'f8ee920b2db5e802da2583a13a4edbf0523ca5fff6b6d6454c1fd6db5f38014d',
      'Status': 'Up 2 seconds'},
     {'Created': 1430293290,
      'Image': 'busybox:latest',
      'Names': ['/boring_mestorf'],
      'Id': '105026325ff668ccf4dc2bcf4f009ea35f2c6a933a778993e6fad3c50173aaab',
      'Command': COMMAND}]

mock_image = \
    {'Created': 1414577076,
     'Id': '3ab9a7ed8a169ab89b09fb3e12a14a390d3c662703b65b4541c0c7bde0ee97eb',
     'ParentId': 'a79ad4dac406fcf85b9c7315fe08de5b620c1f7a12f45c8185c843f4b4a49c4e',
     'RepoTags': ['buildroot-fedora:latest'],
     'Size': 0,
     'VirtualSize': 856564160}

mock_images = None

mock_logs = b'uid=0(root) gid=0(root) groups=10(wheel)'

mock_build_logs = \
    [b'{"stream":"Step 0 : FROM fedora:latest\\n"}\r\n',
     b'{"status":"Pulling from fedora","id":"latest"}\r\n',
     b'{"status":"Digest: sha256:c63476a082b960f6264e59ef0ff93a9169eac8daf59e24805e0382afdcc9082f"}\r\n',
     b'{"status":"Status: Image is up to date for fedora:latest"}\r\n',
     b'{"stream":"Step 1 : RUN uname -a \\u0026\\u0026 env\\n"}\r\n',
     b'{"stream":" ---\\u003e Running in 3600c91d1c40\\n"}\r\n',
     b'{"stream":"Removing intermediate container 3600c91d1c40\\n"}\r\n',
     b'{"stream":"Successfully built 1793c2380436\\n"}\r\n']

mock_build_logs_failed = mock_build_logs + \
    [b'{"errorDetail":{"code":2,"message":"The command \\u0026{[/bin/sh -c ls -lha /a/b/c]} returned a non-zero code: 2"},\
        "error":"The command \\u0026{[/bin/sh -c ls -lha /a/b/c]} returned a non-zero code: 2"}\r\n']

mock_pull_logs = \
    [b'{"stream":"Trying to pull repository localhost:5000/busybox ..."}\r\n',
     b'{"status":"Pulling image (latest) from localhost:5000/busybox","progressDetail":{},"id":"8c2e06607696"}',
     b'{"status":"Download complete","progressDetail":{},"id":"8c2e06607696"}',
     b'{"status":"Status: Image is up to date for localhost:5000/busybox:latest"}\r\n']

mock_push_logs = \
    b'{"status":"The push refers to a repository [localhost:5000/atomic-reactor-tests-b3a11e13d27c428f8fa2914c8c6a6d96] (len: 1)"}\r\n' \
    b'{"errorDetail":{"message":"Repository does not exist: localhost:5000/atomic-reactor-tests-b3a11e13d27c428f8fa2914c8c6a6d96"},' \
    b'"error":"Repository does not exist: localhost:5000/atomic-reactor-tests-b3a11e13d27c428f8fa2914c8c6a6d96"}\r\n'

def _find_image(img, ignore_registry=False):
    global mock_images

    for im in mock_images:
        im_name = im['RepoTags'][0]
        if im_name == img:
            return im
        if ignore_registry:
            im_name_wo_reg = ImageName.parse(im_name).to_str(registry=False)
            if im_name_wo_reg == img:
                return im

    return None

def _docker_exception(code=404, content='not found'):
    response = flexmock(content=content, status_code=code)
    return docker.errors.APIError(code, response)

def _mock_pull(repo, tag='latest', **kwargs):
    repotag = '%s:%s' % (repo, tag)
    if _find_image(repotag) is None:
        new_image = mock_image.copy()
        new_image['RepoTags'] = [repotag]
        mock_images.append(new_image)

    return iter(mock_pull_logs)

def _mock_remove_image(img, **kwargs):
    i = _find_image(img)
    if i is not None:
        mock_images.remove(i)
        return None

    raise _docker_exception()

def _mock_inspect(img, **kwargs):
    # real 'docker inspect busybox' returns info even there's only localhost:5000/busybox
    i = _find_image(img, ignore_registry=True)
    if i is not None:
        return i

    raise _docker_exception()

def _mock_tag(src_img, dest_repo, dest_tag='latest', **kwargs):
    i = _find_image(src_img)
    if i is None:
        raise _docker_exception()

    dst_img = "%s:%s" % (dest_repo, dest_tag)
    i = _find_image(dst_img)
    if i is None:
        new_image = mock_image.copy()
        new_image['RepoTags'] = [dst_img]
        mock_images.append(new_image)

    return True

def mock_docker(build_should_fail=False,
                inspect_should_fail=False,
                wait_should_fail=False,
                provided_image_repotags=None,
                should_raise_error={},
                remember_images=False):
    """
    mock all used docker.Client methods

    :param build_should_fail: True == build() log will contain error
    :param inspect_should_fail: True == inspect_image() will return None
    :param wait_should_fail: True == wait() will return 1 instead of 0
    :param provided_image_repotags: images() will contain provided image
    :param should_raise_error: methods (with args) to raise docker.errors.APIError
    :param remember_images: keep track of available image tags
    """
    if provided_image_repotags:
        mock_image['RepoTags'] = provided_image_repotags
    build_result = iter(mock_build_logs_failed) if build_should_fail else iter(mock_build_logs)
    inspect_result = None if inspect_should_fail else mock_image

    flexmock(docker.Client, build=lambda **kwargs: build_result)
    flexmock(docker.Client, commit=lambda cid, **kwargs: mock_containers[0])
    flexmock(docker.Client, containers=lambda **kwargs: mock_containers)
    flexmock(docker.Client, create_container=lambda img, **kwargs: mock_containers[0])
    flexmock(docker.Client, images=lambda **kwargs: [mock_image])
    flexmock(docker.Client, inspect_image=lambda im_id: inspect_result)
    flexmock(docker.Client, logs=lambda cid, **kwargs: iter([mock_logs]) if kwargs.get('stream') else mock_logs)
    flexmock(docker.Client, pull=lambda img, **kwargs: iter(mock_pull_logs))
    flexmock(docker.Client, push=lambda iid, **kwargs: mock_push_logs)
    flexmock(docker.Client, remove_container=lambda cid, **kwargs: None)
    flexmock(docker.Client, remove_image=lambda iid, **kwargs: None)
    flexmock(docker.Client, start=lambda cid, **kwargs: None)
    flexmock(docker.Client, tag=lambda img, rep, **kwargs: True)
    flexmock(docker.Client, wait=lambda cid: 1 if wait_should_fail else 0)
    flexmock(docker.Client, get_image=lambda img, **kwargs: open("/dev/null",
                                                                 "rb"))
    flexmock(os.path, exists=lambda p: True if p == DOCKER_SOCKET_PATH else old_ope(p))

    for method, args in should_raise_error.items():
        response = flexmock(content="abc", status_code=123)
        if args:
            flexmock(docker.Client).should_receive(method).with_args(*args).and_raise(docker.errors.APIError, "xyz", response)
        else:
            flexmock(docker.Client).should_receive(method).and_raise(docker.errors.APIError, "xyz", response)

    if remember_images:
        global mock_images
        mock_images = [mock_image]

        flexmock(docker.Client, inspect_image=_mock_inspect)
        flexmock(docker.Client, pull=_mock_pull)
        flexmock(docker.Client, remove_image=_mock_remove_image)
        flexmock(docker.Client, tag=_mock_tag)
