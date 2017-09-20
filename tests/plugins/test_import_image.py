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
from atomic_reactor.build import BuildResult

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


class ImageStreamResponse:
    '''
    Mocks a get_image_stream response
    '''
    def __init__(self):
        self.json = lambda: {'hello': 'howdy'}


DEFAULT_TAGS_AMOUNT = 6


def prepare(insecure_registry=None, namespace=None, primary_images_tag_conf=DEFAULT_TAGS_AMOUNT,
            primary_images_annotations=DEFAULT_TAGS_AMOUNT):
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

    annotations = None
    if primary_images_annotations:
        annotations = {
            'repositories': {
                'primary': [
                    'registry.example.com/fedora:annotation_{}'.format(x)
                    for x in range(primary_images_annotations)
                ]}}
    build_result = BuildResult(annotations=annotations, image_id='foo')
    setattr(workflow, 'build_result', build_result)

    if primary_images_tag_conf:
        workflow.tag_conf.add_primary_images([
            'registry.example.com/fedora:tag_conf_{}'.format(x)
            for x in range(primary_images_tag_conf)
        ])

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
     .with_args(TEST_IMAGESTREAM, TEST_REPO, **kwargs)
     .and_return(ImageStreamResponse()))
    (flexmock(OSBS)
     .should_receive('ensure_image_stream_tag')
     .times(DEFAULT_TAGS_AMOUNT))
    (flexmock(OSBS)
     .should_receive('import_image')
     .once()
     .and_return(True))
    runner.run()


@pytest.mark.parametrize(('tag_conf', 'annotations', 'tag_prefix'), (
    (DEFAULT_TAGS_AMOUNT, 0, 'tag_conf_'),
    (DEFAULT_TAGS_AMOUNT, DEFAULT_TAGS_AMOUNT, 'tag_conf_'),
    (0, DEFAULT_TAGS_AMOUNT, 'annotation_'),
))
@pytest.mark.parametrize(('osbs_error'), [True, False])
def test_ensure_primary(monkeypatch, osbs_error, tag_conf, annotations, tag_prefix):
    """
    Test that primary image tags are ensured
    """

    runner = prepare(primary_images_annotations=annotations, primary_images_tag_conf=tag_conf)

    monkeypatch.setenv("BUILD", json.dumps({
        "metadata": {}
    }))

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM)
     .and_return(ImageStreamResponse()))

    for x in range(DEFAULT_TAGS_AMOUNT):
        expectation = (
            flexmock(OSBS)
            .should_receive('ensure_image_stream_tag')
            .with_args(dict, tag_prefix + str(x))
            .once()
            .ordered()
        )
        if osbs_error:
            expectation.and_raise(OsbsResponseException('None', 500))

    (flexmock(OSBS)
     .should_receive('import_image')
     .with_args(TEST_IMAGESTREAM)
     .times(0 if osbs_error else 1)
     .and_return(True))

    if osbs_error:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
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
     .with_args(TEST_IMAGESTREAM)
     .and_return(ImageStreamResponse()))
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('ensure_image_stream_tag')
     .times(DEFAULT_TAGS_AMOUNT))
    (flexmock(OSBS)
     .should_receive('import_image')
     .once()
     .with_args(TEST_IMAGESTREAM)
     .and_return(True))
    runner.run()


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
