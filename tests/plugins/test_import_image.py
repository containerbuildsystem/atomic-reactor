"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.post_import_image import ImportImagePlugin

import osbs.conf
from osbs.api import OSBS
from osbs.exceptions import OsbsResponseException
from flexmock import flexmock
import pytest
from tests.constants import INPUT_IMAGE, SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


TEST_IMAGESTREAM = "library-imagestream1"
TEST_REPO = "registry.example.com/library/imagestream1"


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def prepare(insecure_registry=None, retry_delay=0, import_attempts=3, namespace=None):
    """
    Boiler-plate test set-up
    """
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', 'asd123')
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    fake_conf = osbs.conf.Configuration(conf_file=None, openshift_uri='/')

    expectation = flexmock(osbs.conf).should_receive('Configuration').and_return(fake_conf)
    if namespace:
        expectation.with_args(conf_file=None, namespace=namespace,
                              verify_ssl=False, openshift_url="",
                              openshift_uri="", use_auth=False,
                              build_json_dir="")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': ImportImagePlugin.key,
        'args': {
            'imagestream': TEST_IMAGESTREAM,
            'docker_image_repo': TEST_REPO,
            'url': '',
            'build_json_dir': "",
            'verify_ssl': False,
            'use_auth': False,
            'insecure_registry': insecure_registry,
            'retry_delay': retry_delay,
            'import_attempts': import_attempts,
        }}])

    return runner


def test_bad_setup(monkeypatch):
    """
    Try all the early-fail paths.
    """

    runner = prepare()

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('import_image')
     .never())

    # No build JSON
    monkeypatch.delenv("BUILD", raising=False)
    with pytest.raises(PluginFailedException):
        runner.run()


@pytest.mark.parametrize(('insecure_registry'), [None, False, True])
@pytest.mark.parametrize(('namespace'), [None, 'my_namespace'])
def test_create_image(insecure_registry, namespace, monkeypatch):
    """
    Test that an ImageStream is created if not found
    """

    runner = prepare(insecure_registry=insecure_registry, namespace=namespace)

    kwargs = {}
    build_json = {"metadata": {}}
    if namespace is not None:
        build_json['metadata']['namespace'] = namespace

    monkeypatch.setenv("BUILD", json.dumps(build_json))

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM)
     .and_raise(OsbsResponseException('none', 404)))

    if insecure_registry is not None:
        kwargs['insecure_registry'] = insecure_registry
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM, TEST_REPO, **kwargs))
    (flexmock(OSBS)
     .should_receive('import_image')
     .never())
    runner.run()


@pytest.mark.parametrize(('namespace'), [
    ({}),
    ({'namespace': 'my_namespace'})
])
def test_import_image(namespace, monkeypatch):
    """
    Test importing tags for an existing ImageStream
    """

    runner = prepare(namespace=namespace.get('namespace'))

    build_json = {"metadata": {}}
    build_json["metadata"].update(namespace)
    monkeypatch.setenv("BUILD", json.dumps(build_json))

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM))
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('import_image')
     .once()
     .with_args(TEST_IMAGESTREAM)
     .and_return(True))
    runner.run()


@pytest.mark.parametrize(('retry_delay', 'results'), [
    (0, [False, True]),
    (0.01, [False, False, False]),
    (0.02, [True]),
])
def test_import_image_retry(retry_delay, results, monkeypatch):
    """
    Test retrying importing tags for an existing ImageStream
    """
    import_attempts = len(results)
    runner = prepare(retry_delay=retry_delay, import_attempts=import_attempts)

    build_json = {"metadata": {}}
    monkeypatch.setenv("BUILD", json.dumps(build_json))

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM))
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('import_image')
     .with_args(TEST_IMAGESTREAM)
     .times(import_attempts)
     .and_return(*results)
     .one_by_one())

    if not results[-1]:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()


@pytest.mark.parametrize(('import_attempts'), [0, 1, 2])
def test_import_image_attempts_config(import_attempts):
    """
    Test the validation of import_attempts
    """
    args = {
        "tasker": None,
        "workflow": None,
        "imagestream": None,
        "docker_image_repo": None,
        "url": None,
        "build_json_dir": None,
        "import_attempts": import_attempts,
    }

    if import_attempts > 0:
        plugin = ImportImagePlugin(**args)
        assert plugin.import_attempts == import_attempts
    else:
        with pytest.raises(ValueError) as value_error:
            ImportImagePlugin(**args)
            assert value_error.value.message == (
                "import_attempts is %d, should be at least 1" % import_attempts)


def test_exception_during_create(monkeypatch):
    """
    The plugin should fail if the ImageStream creation fails.
    """

    runner = prepare()
    monkeypatch.setenv("BUILD", json.dumps({
        "metadata": {}
    }))
    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .with_args(TEST_IMAGESTREAM)
     .and_raise(OsbsResponseException('none', 404)))
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM, TEST_REPO)
     .and_raise(RuntimeError))
    (flexmock(OSBS)
     .should_receive('import_image')
     .never())

    with pytest.raises(PluginFailedException):
        runner.run()


def test_exception_during_import(monkeypatch):
    """
    The plugin should fail if image import fails.
    """

    runner = prepare()
    monkeypatch.setenv("BUILD", json.dumps({
        "metadata": {}
    }))
    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .with_args(TEST_IMAGESTREAM)
     .and_raise(OsbsResponseException('none', 404)))
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM, TEST_REPO)
     .and_raise(RuntimeError))
    (flexmock(OSBS)
     .should_receive('import_image')
     .never())

    with pytest.raises(PluginFailedException):
        runner.run()
