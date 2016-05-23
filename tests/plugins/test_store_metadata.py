"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
import json
import time
from datetime import datetime, timedelta
from copy import deepcopy

try:
    import koji as koji
except ImportError:
    import inspect
    import os
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))
finally:
    del koji
    from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin

from flexmock import flexmock
from osbs.api import OSBS
import osbs.conf
from osbs.exceptions import OsbsResponseException
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import ExitPluginsRunner
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin

from atomic_reactor.plugins.exit_store_metadata_in_osv3 import StoreMetadataInOSv3Plugin
from atomic_reactor.plugins.pre_cp_dockerfile import CpDockerfilePlugin
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.util import ImageName, LazyGit
import pytest
from tests.constants import LOCALHOST_REGISTRY, DOCKER0_REGISTRY, TEST_IMAGE, INPUT_IMAGE
from tests.util import is_string_type

DIGEST1 = "sha256:1da9b9e1c6bf6ab40f1627d76e2ad58e9b2be14351ef4ff1ed3eb4a156138189"
DIGEST2 = "sha256:0000000000000000000000000000000000000000000000000000000000000000"


class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")
    # image = ImageName.parse("test-image:unique_tag_123")


def prepare():
    def set_annotations_on_build(build_id, annotations):
        pass
    def update_labels_on_build(build_id, labels):
        pass
    new_environ = deepcopy(os.environ)
    new_environ["BUILD"] = '''
{
  "metadata": {
    "name": "asd",
    "namespace": "namespace"
  }
}
'''
    flexmock(OSBS, set_annotations_on_build=set_annotations_on_build)
    flexmock(OSBS, update_labels_on_build=update_labels_on_build)
    (flexmock(osbs.conf)
     .should_call("Configuration")
     .with_args(namespace="namespace", conf_file=None, verify_ssl=True,
                openshift_url="http://example.com/", openshift_uri="http://example.com/",
                use_auth=True))
    flexmock(os)
    os.should_receive("environ").and_return(new_environ)

    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, "test-image")

    workflow.push_conf.add_pulp_registry("test", LOCALHOST_REGISTRY)
    workflow.tag_conf.add_primary_image(TEST_IMAGE)
    workflow.tag_conf.add_unique_image("namespace/image:asd123")

    r = workflow.push_conf.add_docker_registry(DOCKER0_REGISTRY)
    r.digests[TEST_IMAGE] = DIGEST1
    r.digests["namespace/image:asd123"] = DIGEST2

    setattr(workflow, 'builder', X)
    setattr(workflow, '_base_image_inspect', {'Id': '01234567'})
    workflow.build_logs = [
        "a", "b",
    ]
    workflow.source.lg = LazyGit(None, commit="commit")
    flexmock(workflow.source.lg)
    workflow.source.lg.should_receive("_commit_id").and_return("commit")

    return workflow


def test_metadata_plugin(tmpdir):
    initial_timestamp = datetime.now()
    workflow = prepare()

    workflow.prebuild_results = {
        CpDockerfilePlugin.key: "dockerfile-content",
        DistgitFetchArtefactsPlugin.key: "artefact1\nartefact2",
    }
    workflow.postbuild_results = {
        PostBuildRPMqaPlugin.key: "rpm1\nrpm2",
    }
    workflow.plugins_timestamps = {
        CpDockerfilePlugin.key: initial_timestamp.isoformat(),
        DistgitFetchArtefactsPlugin.key: (initial_timestamp + timedelta(seconds=1)).isoformat(),
        PostBuildRPMqaPlugin.key: (initial_timestamp + timedelta(seconds=3)).isoformat(),
    }
    workflow.plugins_durations = {
        CpDockerfilePlugin.key: 1.01,
        DistgitFetchArtefactsPlugin.key: 2.02,
        PostBuildRPMqaPlugin.key: 3.03,
    }
    workflow.plugins_errors = {
        DistgitFetchArtefactsPlugin.key: 'foo'
    }

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': StoreMetadataInOSv3Plugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataInOSv3Plugin.key in output
    annotations = output[StoreMetadataInOSv3Plugin.key]["annotations"]
    assert "dockerfile" in annotations
    assert is_string_type(annotations['dockerfile'])
    assert "artefacts" in annotations
    assert is_string_type(annotations['artefacts'])
    assert "logs" in annotations
    assert is_string_type(annotations['logs'])
    assert annotations['logs'] == ''
    assert "rpm-packages" in annotations
    assert is_string_type(annotations['rpm-packages'])
    assert annotations['rpm-packages'] == ''
    assert "repositories" in annotations
    assert is_string_type(annotations['repositories'])
    assert "commit_id" in annotations
    assert is_string_type(annotations['commit_id'])
    assert "base-image-id" in annotations
    assert is_string_type(annotations['base-image-id'])
    assert "base-image-name" in annotations
    assert is_string_type(annotations['base-image-name'])
    assert "image-id" in annotations
    assert is_string_type(annotations['image-id'])

    assert "digests" in annotations
    assert is_string_type(annotations['digests'])
    digests = json.loads(annotations['digests'])
    expected = [{
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
    },{
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
    }]
    assert digests == expected or digests == reversed(expected)

    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = json.loads(annotations["plugins-metadata"])
    assert "distgit_fetch_artefacts" in plugins_metadata["errors"]

    assert "cp_dockerfile" in plugins_metadata["durations"]
    assert "distgit_fetch_artefacts" in plugins_metadata["durations"]
    assert "all_rpm_packages" in plugins_metadata["durations"]


def test_metadata_plugin_rpmqa_failure(tmpdir):
    initial_timestamp = datetime.now()
    workflow = prepare()

    workflow.prebuild_results = {
        CpDockerfilePlugin.key: "dockerfile-content",
        DistgitFetchArtefactsPlugin.key: "artefact1\nartefact2",
    }
    workflow.postbuild_results = {
        PostBuildRPMqaPlugin.key: RuntimeError(),
    }
    workflow.plugins_timestamps = {
        CpDockerfilePlugin.key: initial_timestamp.isoformat(),
        DistgitFetchArtefactsPlugin.key: (initial_timestamp + timedelta(seconds=1)).isoformat(),
        PostBuildRPMqaPlugin.key: (initial_timestamp + timedelta(seconds=3)).isoformat(),
    }
    workflow.plugins_durations = {
        CpDockerfilePlugin.key: 1.01,
        DistgitFetchArtefactsPlugin.key: 2.02,
        PostBuildRPMqaPlugin.key: 3.03,
    }
    workflow.plugins_errors = {
        PostBuildRPMqaPlugin.key: 'foo'
    }

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': StoreMetadataInOSv3Plugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataInOSv3Plugin.key in output
    annotations = output[StoreMetadataInOSv3Plugin.key]["annotations"]
    assert "dockerfile" in annotations
    assert "artefacts" in annotations
    assert "logs" in annotations
    assert "rpm-packages" in annotations
    assert "repositories" in annotations
    assert "commit_id" in annotations
    assert "base-image-id" in annotations
    assert "base-image-name" in annotations
    assert "image-id" in annotations

    # On rpmqa failure, rpm-packages should be empty
    assert len(annotations["rpm-packages"]) == 0

    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = json.loads(annotations["plugins-metadata"])
    assert "all_rpm_packages" in plugins_metadata["errors"]

    assert "cp_dockerfile" in plugins_metadata["durations"]
    assert "distgit_fetch_artefacts" in plugins_metadata["durations"]
    assert "all_rpm_packages" in plugins_metadata["durations"]

def test_labels_metadata_plugin(tmpdir):

    koji_build_id = 1234
    workflow = prepare()

    workflow.exit_results = {
        KojiPromotePlugin.key: koji_build_id,
    }

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': StoreMetadataInOSv3Plugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataInOSv3Plugin.key in output
    labels = output[StoreMetadataInOSv3Plugin.key]["labels"]
    assert "koji-build-id" in labels
    assert is_string_type(labels["koji-build-id"])
    assert int(labels["koji-build-id"]) == koji_build_id

def test_missing_koji_build_id(tmpdir):
    workflow = prepare()

    workflow.exit_results = {}

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': StoreMetadataInOSv3Plugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataInOSv3Plugin.key in output
    labels = output[StoreMetadataInOSv3Plugin.key]["labels"]
    assert "koji-build-id" not in labels


def test_store_metadata_fail_update_annotations(tmpdir, caplog):
    workflow = prepare()

    workflow.exit_results = {}

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': StoreMetadataInOSv3Plugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    (flexmock(OSBS)
        .should_receive('set_annotations_on_build')
        .and_raise(OsbsResponseException('/', 'failed', 0)))
    output = runner.run()
    assert 'annotations:' in caplog.text()


def test_store_metadata_fail_update_labels(tmpdir, caplog):
    workflow = prepare()

    workflow.exit_results = {
        KojiPromotePlugin.key: 1234,
    }

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': StoreMetadataInOSv3Plugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    (flexmock(OSBS)
        .should_receive('update_labels_on_build')
        .and_raise(OsbsResponseException('/', 'failed', 0)))
    output = runner.run()
    assert 'labels:' in caplog.text()
