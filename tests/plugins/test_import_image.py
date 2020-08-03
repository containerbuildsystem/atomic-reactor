"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import json

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.exit_import_image import ImportImagePlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.build import BuildResult
from atomic_reactor.plugins import pre_reactor_config

import osbs.conf
from osbs.api import OSBS
from osbs.exceptions import OsbsResponseException
from osbs.utils import ImageName
from flexmock import flexmock
import pytest
from tests.constants import INPUT_IMAGE, SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


TEST_IMAGESTREAM = "library-imagestream1"
TEST_REGISTRY = "registry.example.com"
TEST_NAME_LABEL = "library/imagestream1"
TEST_REPO = 'imagestream1'
TEST_REPO_WITH_REGISTRY = '{}/{}'.format(TEST_REGISTRY, TEST_REPO)


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


class ImageStreamResponse(object):
    '''
    Mocks a get_image_stream response
    '''
    def __init__(self):
        self.json = lambda: {'hello': 'howdy'}


DEFAULT_TAGS_AMOUNT = 6


def prepare(tmpdir, insecure_registry=None, namespace=None,
            primary_images_tag_conf=DEFAULT_TAGS_AMOUNT,
            build_process_failed=False,
            organization=None, reactor_config_map=False, imagestream_name=TEST_IMAGESTREAM):
    """
    Boiler-plate test set-up
    """
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(source=SOURCE)
    setattr(workflow, 'builder', X())
    flexmock(workflow, build_process_failed=build_process_failed)
    setattr(workflow.builder, 'image_id', 'asd123')
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)

    df = tmpdir.join('Dockerfile')
    df.write('FROM base\n')
    df.write('LABEL name={}'.format(TEST_NAME_LABEL))
    setattr(workflow.builder, 'df_path', str(df))

    build_result = BuildResult(image_id='foo')
    setattr(workflow, 'build_result', build_result)

    if primary_images_tag_conf:
        floating_images = [
            '{}:tag_conf_{}'.format(TEST_REPO, x)
            for x in range(primary_images_tag_conf)
        ]
        workflow.tag_conf.add_primary_image('{}:version-release'.format(TEST_REPO))
        workflow.tag_conf.add_floating_images(floating_images)

    fake_conf = osbs.conf.Configuration(conf_file=None, openshift_url='/')

    expectation = flexmock(osbs.conf).should_receive('Configuration').and_return(fake_conf)
    if namespace:
        expectation.with_args(conf_file=None, namespace=namespace,
                              verify_ssl=not insecure_registry, openshift_url="/",
                              use_auth=False, build_json_dir="/var/json_dir")

    plugin_args = {'imagestream': imagestream_name}

    if reactor_config_map:
        openshift_map = {
            'url': '/',
            'auth': {'enable': False},
            'insecure': insecure_registry,
            'build_json_dir': '/var/json_dir',
        }
        source_registry_map = {
            'url': TEST_REGISTRY,
            'insecure': insecure_registry
        }
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({
                'version': 1,
                'openshift': openshift_map,
                'source_registry': source_registry_map,
                'registries_organization': organization,
            })
    else:
        plugin_args.update({
            'docker_image_repo': TEST_REPO_WITH_REGISTRY,
            'url': '/',
            'build_json_dir': "/var/json_dir",
            'verify_ssl': not insecure_registry,
            'use_auth': False,
            'insecure_registry': insecure_registry,
        })

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': ImportImagePlugin.key,
        'args': plugin_args
    }])

    def mocked_import_image_tags(**kwargs):
        return

    if not hasattr(OSBS, 'import_image_tags'):
        setattr(OSBS, 'import_image_tags', mocked_import_image_tags)

    return runner


def test_bad_setup(tmpdir, caplog, monkeypatch, reactor_config_map, user_params):  # noqa
    """
    Try all the early-fail paths.
    """

    runner = prepare(tmpdir, primary_images_tag_conf=0,
                     reactor_config_map=reactor_config_map)

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .never())
    (flexmock(OSBS)
     .should_receive('import_image_tags')
     .never())

    # No build JSON
    monkeypatch.delenv("BUILD", raising=False)
    runner.run()
    assert 'No floating tags to import, skipping import_image' in caplog.text


@pytest.mark.parametrize(('insecure_registry'), [None, False, True])
@pytest.mark.parametrize(('namespace'), [None, 'my_namespace'])
@pytest.mark.parametrize(('organization'), [None, 'my_organization'])
def test_create_image(tmpdir, insecure_registry, namespace, organization,
                      monkeypatch, reactor_config_map, user_params):
    """
    Test that an ImageStream is created if not found
    """

    runner = prepare(tmpdir, insecure_registry=insecure_registry, namespace=namespace,
                     organization=organization, reactor_config_map=reactor_config_map)

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

    enclose_repo = ImageName.parse(TEST_REPO_WITH_REGISTRY)
    if reactor_config_map and organization:
        enclose_repo.enclose(organization)
    (flexmock(OSBS)
     .should_receive('create_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM)
     .and_return(ImageStreamResponse()))
    (flexmock(OSBS)
     .should_receive('import_image_tags')
     .once()
     .and_return(True))
    runner.run()


@pytest.mark.parametrize(('osbs_error'), [True, False])
def test_ensure_primary(tmpdir, monkeypatch, osbs_error, reactor_config_map, user_params):
    """
    Test that primary image tags are ensured
    """

    runner = prepare(tmpdir, primary_images_tag_conf=DEFAULT_TAGS_AMOUNT,
                     reactor_config_map=reactor_config_map)

    monkeypatch.setenv("BUILD", json.dumps({
        "metadata": {}
    }))
    tags = []
    floating_images = runner.workflow.tag_conf.floating_images
    if not floating_images:
        floating_images = [
            ImageName.parse(floating) for floating in
            runner.workflow.build_result.annotations['repositories']['floating']]

    for floating_image in floating_images:
        tag = floating_image.tag
        tags.append(tag)

    (flexmock(OSBS)
     .should_receive('get_image_stream')
     .once()
     .with_args(TEST_IMAGESTREAM)
     .and_return(ImageStreamResponse()))

    repository = '{}/{}'.format(TEST_REGISTRY, TEST_REPO)
    (flexmock(OSBS)
     .should_receive('import_image_tags')
     .with_args(TEST_IMAGESTREAM, tags, repository, insecure=None)
     .times(0 if osbs_error else 1)
     .and_return(True))

    if osbs_error:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()


@pytest.mark.parametrize('build_process_failed', [True, False])
@pytest.mark.parametrize(('namespace'), [
    ({}),
    ({'namespace': 'my_namespace'})
])
def test_import_image(tmpdir, build_process_failed, namespace,
                      monkeypatch, reactor_config_map, user_params):
    """
    Test importing tags for an existing ImageStream
    """

    runner = prepare(tmpdir, namespace=namespace.get('namespace'),
                     build_process_failed=build_process_failed,
                     reactor_config_map=reactor_config_map)

    build_json = {"metadata": {}}
    build_json["metadata"].update(namespace)
    monkeypatch.setenv("BUILD", json.dumps(build_json))

    tags = []
    for floating_image in runner.workflow.tag_conf.floating_images:
        tag = floating_image.tag
        tags.append(tag)

    if build_process_failed:
        (flexmock(pre_reactor_config)
         .should_receive('get_openshift_session')
         .never())
        (flexmock(ImportImagePlugin)
         .should_receive('get_or_create_imagestream')
         .never())
        (flexmock(OSBS)
         .should_receive('import_image_tags')
         .never())
    else:
        (flexmock(OSBS)
         .should_receive('get_image_stream')
         .once()
         .with_args(TEST_IMAGESTREAM)
         .and_return(ImageStreamResponse()))
        (flexmock(OSBS)
         .should_receive('create_image_stream')
         .never())

        repository = '{}/{}'.format(TEST_REGISTRY, TEST_REPO)
        (flexmock(OSBS)
         .should_receive('import_image_tags')
         .once()
         .with_args(TEST_IMAGESTREAM, tags, repository, insecure=None)
         .and_return(True))

    runner.run()


def test_exception_during_create(tmpdir, monkeypatch, reactor_config_map, user_params):  # noqa
    """
    The plugin should fail if the ImageStream creation fails.
    """

    runner = prepare(tmpdir, reactor_config_map=reactor_config_map)
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
     .with_args(TEST_IMAGESTREAM)
     .and_raise(RuntimeError))
    (flexmock(OSBS)
     .should_receive('import_image_tags')
     .never())

    with pytest.raises(PluginFailedException):
        runner.run()


def test_exception_during_import(tmpdir, monkeypatch, reactor_config_map, user_params):  # noqa
    """
    The plugin should fail if image import fails.
    """

    runner = prepare(tmpdir, reactor_config_map=reactor_config_map)
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
     .with_args(TEST_IMAGESTREAM)
     .and_raise(RuntimeError))
    (flexmock(OSBS)
     .should_receive('import_image_tags')
     .never())

    with pytest.raises(PluginFailedException):
        runner.run()


@pytest.mark.parametrize('imagestream, scratch', [
    (None, False),
    ('', False),
    ('some', True),
    ('', True),
    (None, True),
])
def test_skip_plugin(tmpdir, caplog, monkeypatch, reactor_config_map, user_params,
                     imagestream, scratch):  # noqa
    runner = prepare(tmpdir, reactor_config_map=reactor_config_map, imagestream_name=imagestream)
    runner.workflow.user_params['scratch'] = scratch
    monkeypatch.setenv("BUILD", json.dumps({
        "metadata": {}
    }))

    runner.run()

    if scratch:
        log_msg = 'scratch build, skipping plugin'
    elif not imagestream:
        log_msg = 'no imagestream provided, skipping plugin'

    assert log_msg in caplog.text
