"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
from copy import deepcopy

from flexmock import flexmock
from osbs.api import OSBS
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin

from atomic_reactor.plugins.post_store_metadata_in_osv3 import StoreMetadataInOSv3Plugin
from atomic_reactor.plugins.pre_cp_dockerfile import CpDockerfilePlugin
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.util import ImageName, LazyGit
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE


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

    setattr(workflow, 'builder', X)
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

    runner = PostBuildPluginsRunner(
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


def test_metadata_plugin_rpmqa_failure(tmpdir):
    workflow = prepare()

    workflow.prebuild_results = {
        CpDockerfilePlugin.key: "dockerfile-content",
        DistgitFetchArtefactsPlugin.key: "artefact1\nartefact2",
    }
    workflow.postbuild_results = {
        PostBuildRPMqaPlugin.key: RuntimeError(),
    }

    runner = PostBuildPluginsRunner(
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

    # On rpmqa failure, rpm-packages should be empty
    assert len(labels["rpm-packages"]) == 0
