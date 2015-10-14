"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
import json
from copy import deepcopy

from flexmock import flexmock
from osbs.api import OSBS
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import ExitPluginsRunner
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin

from atomic_reactor.plugins.exit_store_metadata_in_osv3 import StoreMetadataInOSv3Plugin
from atomic_reactor.plugins.pre_cp_dockerfile import CpDockerfilePlugin
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.util import ImageName, LazyGit
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
    def set_annotations_on_build(build_id, labels, namespace='default'):
        assert namespace == 'namespace'
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
    workflow.build_logs = ["a", "b"]
    workflow.source.lg = LazyGit(None, commit="commit")
    flexmock(workflow.source.lg)
    workflow.source.lg.should_receive("_commit_id").and_return("commit")

    return workflow


def test_metadata_plugin(tmpdir):
    workflow = prepare()

    workflow.prebuild_results = {
        CpDockerfilePlugin.key: "dockerfile-content",
        DistgitFetchArtefactsPlugin.key: "artefact1\nartefact2",
    }
    workflow.postbuild_results = {
        PostBuildRPMqaPlugin.key: "rpm1\nrpm2",
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
    labels = output[StoreMetadataInOSv3Plugin.key]
    assert "dockerfile" in labels
    assert is_string_type(labels['dockerfile'])
    assert "artefacts" in labels
    assert is_string_type(labels['artefacts'])
    assert "logs" in labels
    assert is_string_type(labels['logs'])
    assert "rpm-packages" in labels
    assert is_string_type(labels['rpm-packages'])
    assert "repositories" in labels
    assert is_string_type(labels['repositories'])
    assert "commit_id" in labels
    assert is_string_type(labels['commit_id'])
    assert "base-image-id" in labels
    assert is_string_type(labels['base-image-id'])
    assert "base-image-name" in labels
    assert is_string_type(labels['base-image-name'])
    assert "image-id" in labels
    assert is_string_type(labels['image-id'])

    assert "digests" in labels
    assert is_string_type(labels['digests'])
    digests = json.loads(labels['digests'])
    expected = [{
        "registry": DOCKER0_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
    },{
        "registry": DOCKER0_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
    }]
    assert digests == expected or digests == reversed(expected)

def test_metadata_plugin_rpmqa_failure(tmpdir):
    workflow = prepare()

    workflow.prebuild_results = {
        CpDockerfilePlugin.key: "dockerfile-content",
        DistgitFetchArtefactsPlugin.key: "artefact1\nartefact2",
    }
    workflow.postbuild_results = {
        PostBuildRPMqaPlugin.key: RuntimeError(),
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
    labels = output[StoreMetadataInOSv3Plugin.key]
    assert "dockerfile" in labels
    assert "artefacts" in labels
    assert "logs" in labels
    assert "rpm-packages" in labels
    assert "repositories" in labels
    assert "commit_id" in labels
    assert "base-image-id" in labels
    assert "base-image-name" in labels
    assert "image-id" in labels

    # On rpmqa failure, rpm-packages should be empty
    assert len(labels["rpm-packages"]) == 0
