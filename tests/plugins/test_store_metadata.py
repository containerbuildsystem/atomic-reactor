"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import os
from datetime import datetime, timedelta
from textwrap import dedent

from flexmock import flexmock
import osbs.conf
from osbs.utils import ImageName

from atomic_reactor.constants import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.add_help import AddHelpPlugin
from atomic_reactor.plugins.rpmqa import RPMqaPlugin
from atomic_reactor.plugins.store_metadata import StoreMetadataPlugin
from atomic_reactor.plugins.verify_media_types import VerifyMediaTypesPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.util import ManifestDigest, DockerfileImages, RegistryClient
import pytest
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, TEST_IMAGE_NAME
from tests.mock_env import MockEnv
from tests.util import add_koji_map_in_workflow, is_string_type

DIGEST1 = "sha256:1da9b9e1c6bf6ab40f1627d76e2ad58e9b2be14351ef4ff1ed3eb4a156138189"
DIGEST2 = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
DIGEST_NOT_USED = "not-used"

MOCK_DOCKERFILE = ""

pytestmark = pytest.mark.usefixtures('user_params')


def prepare(workflow, registry=None, no_dockerfile=True, init_dirs=True):
    if not registry:
        registry = LOCALHOST_REGISTRY

    if no_dockerfile:
        os.remove(os.path.join(workflow.source.path, 'Dockerfile'))
    if init_dirs:
        workflow.build_dir.init_build_dirs(["x86_64"], workflow.source)
    config_kwargs = {
        'namespace': workflow.namespace,
        'verify_ssl': True,
        'openshift_url': 'http://example.com/',
        'use_auth': True,
        'conf_file': None,
    }
    (flexmock(osbs.conf.Configuration)
     .should_call("__init__")
     .with_args(**config_kwargs))

    openshift_map = {
        'url': 'http://example.com/',
        'insecure': False,
        'auth': {'enable': True},
    }

    rcm = {
        'version': 1,
        'openshift': openshift_map,
        'registry': {'url': registry, 'insecure': True},
    }
    workflow.conf.conf = rcm
    add_koji_map_in_workflow(workflow, hub_url='/', root_url='')

    tag_conf = workflow.data.tag_conf

    tag_conf.add_floating_image(f'{registry}/{TEST_IMAGE}')
    tag_conf.add_primary_image(f'{registry}/namespace/image:version-release')
    tag_conf.add_unique_image(f'{registry}/namespace/image:asd123')

    (flexmock(RegistryClient)
     .should_receive('get_manifest_digests')
     .with_args(image=ImageName.parse(f'{registry}/{TEST_IMAGE_NAME}'),
                versions=('v1', 'v2', 'v2_list', 'oci', 'oci_index'), require_digest=True)
     .and_return(ManifestDigest(v1=DIGEST_NOT_USED, v2=DIGEST1)))

    (flexmock(RegistryClient)
     .should_receive('get_manifest_digests')
     .with_args(image=ImageName.parse(f'{registry}/namespace/image:version-release'),
                versions=('v1', 'v2', 'v2_list', 'oci', 'oci_index'), require_digest=True)
     .and_return(None))

    (flexmock(RegistryClient)
     .should_receive('get_manifest_digests')
     .with_args(image=ImageName.parse(f'{registry}/namespace/image:asd123'),
                versions=('v1', 'v2', 'v2_list', 'oci', 'oci_index'), require_digest=True)
     .and_return(ManifestDigest(v1=DIGEST_NOT_USED, v2=DIGEST2)))

    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return({'Id': '01234567'})
    workflow.build_logs = [
        "a", "b",
    ]

    workflow.source = GitSource('git', 'git://fake-url.com/repo')
    flexmock(workflow.source).should_receive('commit_id').and_return('commit')


def mock_dockerfile(workflow: DockerBuildWorkflow, content: str) -> None:
    workflow.build_dir.any_platform.dockerfile_path.write_text(content, "utf-8")


@pytest.mark.parametrize('failed', (True, False))
@pytest.mark.parametrize('init_dirs', (True, False))
@pytest.mark.parametrize(('help_results', 'expected_help_results', 'base_from_scratch'), (
    (None, False, False),
    ({
        'help_file': None,
        'status': AddHelpPlugin.NO_HELP_FILE_FOUND,
    }, None, False),
    ({
        'help_file': 'help.md',
        'status': AddHelpPlugin.HELP_GENERATED,
    }, 'help.md', True),
))
@pytest.mark.parametrize(('verify_media_results', 'expected_media_results'), (
    ([], False),
    (["application/vnd.docker.distribution.manifest.v1+json"],
     ["application/vnd.docker.distribution.manifest.v1+json"]),
))
def test_metadata_plugin(workflow, source_dir, tmpdir, failed, init_dirs,
                         help_results, expected_help_results, base_from_scratch,
                         verify_media_results, expected_media_results):
    if base_from_scratch:
        df_content = dedent("""\
            FROM fedora
            RUN yum install -y python-django
            CMD blabla
            FROM scratch
            RUN yum install -y python
            """)
        all_parents = ['fedora', 'scratch']
    else:
        df_content = dedent("""\
            FROM fedora
            RUN yum install -y python-django
            CMD blabla
            """)
        all_parents = ['fedora']

    prepare(workflow, init_dirs=init_dirs)
    workflow.annotations_result = tmpdir / 'annotations_result'
    if init_dirs:
        mock_dockerfile(workflow, df_content)

        dockerfile = workflow.build_dir.any_platform.dockerfile_with_parent_env(
            workflow.imageutil.base_image_inspect()
        )

        df_images = DockerfileImages(dockerfile.parent_images)
        for parent in dockerfile.parent_images:
            if parent != 'scratch':
                df_images[parent] = "sha256:spamneggs"
    else:
        df_images = DockerfileImages(all_parents)
        df_images['fedora'] = "sha256:spamneggs"

    env = (MockEnv(workflow)
           .for_plugin(StoreMetadataPlugin.key)
           .set_plugin_args({"url": "http://example.com/"})
           .set_dockerfile_images(df_images)
           .set_plugin_result(RPMqaPlugin.key, "rpm1\nrpm2")
           .set_plugin_result(VerifyMediaTypesPlugin.key, verify_media_results)
           .set_plugin_result(AddHelpPlugin.key, help_results)
           .mock_build_outcome(failed=failed))

    if help_results is not None:
        workflow.data.annotations['help_file'] = help_results['help_file']

    workflow.fs_watcher._data = dict(fs_data=None)

    initial_timestamp = datetime.now()
    timestamp = (initial_timestamp + timedelta(seconds=3)).isoformat()
    workflow.data.plugins_timestamps = {
        RPMqaPlugin.key: timestamp,
    }
    workflow.data.plugins_durations = {
        RPMqaPlugin.key: 3.03,
    }
    workflow.data.plugins_errors = {}

    output = env.create_runner().run()

    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert "dockerfile" in annotations
    assert is_string_type(annotations['dockerfile'])
    if init_dirs:
        assert annotations['dockerfile'] == df_content
    else:
        assert annotations['dockerfile'] == ''
    assert "commit_id" in annotations
    assert is_string_type(annotations['commit_id'])
    assert annotations['commit_id'] == 'commit'

    assert "base-image-name" in annotations
    assert is_string_type(annotations['base-image-name'])
    assert "parent_images" in annotations

    if base_from_scratch:
        assert annotations["base-image-name"] == ""
        assert 'scratch' in annotations['parent_images']
        assert annotations['parent_images']['scratch'] == 'scratch'
    else:
        assert annotations["base-image-name"] ==\
               workflow.data.dockerfile_images.original_base_image
        assert 'fedora:latest' in annotations['parent_images']
        assert annotations['parent_images']['fedora:latest'] ==\
               workflow.data.dockerfile_images.base_image.to_str()

    assert "filesystem" in annotations
    assert "fs_data" in annotations['filesystem']

    assert "digests" in annotations
    digests = annotations['digests']
    expected = [{
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
        "version": "v2"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
        "version": "v2"
    }]
    if failed:
        assert digests == []
    else:
        assert all(digest in expected for digest in digests)
        assert all(digest in digests for digest in expected)

    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = annotations["plugins-metadata"]
    assert "all_rpm_packages" in plugins_metadata["durations"]

    if expected_help_results is False:
        assert 'help_file' not in annotations
    else:
        assert annotations['help_file'] == expected_help_results

    if expected_media_results:
        media_types = expected_media_results
        assert sorted(annotations['media-types']) == sorted(list(set(media_types)))
    else:
        assert 'media-types' not in annotations

    with open(workflow.annotations_result) as f:
        annotations_result = json.loads(f.read())

    # in annotations result are only errors
    annotations['plugins-metadata'].pop('durations')
    annotations['plugins-metadata'].pop('timestamps')
    assert annotations['plugins-metadata'] == annotations_result['plugins-metadata']


@pytest.mark.parametrize('failed', (True, False))
@pytest.mark.parametrize(('verify_media_results', 'expected_media_results'), (
    ([], False),
    (["application/vnd.docker.distribution.manifest.v1+json"],
     ["application/vnd.docker.distribution.manifest.v1+json"]),
))
def test_metadata_plugin_source(failed, verify_media_results, expected_media_results,
                                workflow, tmpdir):
    sources_for_nvr = 'image_build'
    sources_for_koji_build_id = '12345'

    fetch_sources_result = {
        'sources_for_koji_build_id': sources_for_koji_build_id,
        'sources_for_nvr': sources_for_nvr,
        'image_sources_dir': 'source_dir',
    }

    env = (MockEnv(workflow)
           .for_plugin(StoreMetadataPlugin.key)
           .set_plugin_args({"url": "http://example.com/"})
           .set_plugin_result(PLUGIN_FETCH_SOURCES_KEY, fetch_sources_result)
           .set_plugin_result(VerifyMediaTypesPlugin.key, verify_media_results)
           .mock_build_outcome(failed=failed))
    prepare(workflow)

    workflow.annotations_result = tmpdir / 'annotations_result'
    workflow.fs_watcher._data = dict(fs_data=None)

    initial_timestamp = datetime.now()
    timestamp = (initial_timestamp + timedelta(seconds=3)).isoformat()
    workflow.data.plugins_timestamps = {
        PLUGIN_FETCH_SOURCES_KEY: timestamp,
    }
    workflow.data.plugins_durations = {
        PLUGIN_FETCH_SOURCES_KEY: 3.03,
    }
    workflow.data.plugins_errors = {}

    output = env.create_runner().run()

    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert "filesystem" in annotations
    assert "fs_data" in annotations['filesystem']
    assert "digests" in annotations
    digests = annotations['digests']
    expected = [{
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
        "version": "v2"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
        "version": "v2"
    }]

    if failed:
        assert digests == []
    else:
        assert all(digest in expected for digest in digests)
        assert all(digest in digests for digest in expected)

    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = annotations["plugins-metadata"]
    assert PLUGIN_FETCH_SOURCES_KEY in plugins_metadata["durations"]

    if expected_media_results:
        media_types = expected_media_results
        assert sorted(annotations['media-types']) == sorted(list(set(media_types)))
    else:
        assert 'media-types' not in annotations

    with open(workflow.annotations_result) as f:
        annotations_result = json.loads(f.read())

    # in annotations result are only errors
    annotations['plugins-metadata'].pop('durations')
    annotations['plugins-metadata'].pop('timestamps')
    assert annotations['plugins-metadata'] == annotations_result['plugins-metadata']


def test_exit_before_dockerfile_created(workflow, source_dir):
    env = (MockEnv(workflow)
           .for_plugin(StoreMetadataPlugin.key)
           .set_plugin_args({"url": "http://example.com/"})
           .mock_build_outcome(failed=False))
    prepare(workflow, no_dockerfile=True)
    workflow.data.plugins_results = {}

    output = env.create_runner().run()
    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert annotations["base-image-name"] == ""
    assert annotations["dockerfile"] == ""


def test_plugin_annotations(workflow):
    env = (MockEnv(workflow)
           .for_plugin(StoreMetadataPlugin.key)
           .set_plugin_args({"url": "http://example.com/"})
           .mock_build_outcome(failed=False))
    prepare(workflow)
    workflow.data.annotations = {'foo': {'bar': 'baz'}, 'spam': ['eggs']}

    output = env.create_runner().run()
    annotations = output[StoreMetadataPlugin.key]["annotations"]

    assert annotations['foo'] == {"bar": "baz"}
    assert annotations['spam'] == ["eggs"]
