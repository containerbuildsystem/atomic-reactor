"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os.path
from tempfile import TemporaryDirectory

import pytest
from osbs.exceptions import OsbsValidationException
from osbs.utils import ImageName

from atomic_reactor.inner import TagConf, ImageBuildWorkflowData, WorkflowDataEncoder
from atomic_reactor.util import validate_with_schema, DockerfileImages

SOURCE_CONTAINERS_USER_PARAMS_ALL_PROPERTIES = {
    "component": "osbs-test-base-container-source",
    "image_tag": "user/osbs-test-base-container-source:osbs-test-1.0-rhel-7",
    "kind": "source_containers_user_params",
    "koji_target": "osbs-test-1.0-rhel-7-containers-candidate",
    "namespace": "exd-sp-guild-container-build--osbs-qa-source-containers",
    "pipeline_run_name": "source-container-0-1-49063-20211129161231",
    "reactor_config_map": "reactor-config-map-user",
    "sources_for_koji_build_nvr": "osbs-test-base-container-1.0.distscope.base-2396",
    "user": "user",
}

SOURCE_CONTAINERS_USER_PARAMS_MINIMAL = {
    "user": "user",
}
SOURCE_CONTAINERS_USER_PARAMS_MISSING_BUILD_JSON_DIR = {"user": "user"}
SOURCE_CONTAINERS_USER_PARAMS_MISSING_USER = {}
SOURCE_CONTAINERS_USER_PARAMS_ADDITIONAL_NONDEFINED_PROPERTY = {
    "user": "user",
    "additional_random_property": "value",
}

USER_PARAMS_MINIMAL = {
    "user": "user",
    "git_uri": "git://git/uri",
    "git_ref": "aaaaaaa",
}
USER_PARAMS_MISSING_BUILD_JSON_DIR = {
    "user": "user",
    "git_uri": "git://git/uri",
    "git_ref": "aaaaaaa",
}
USER_PARAMS_MISSING_USER = {
    "git_uri": "git://git/uri",
    "git_ref": "aaaaaaa",
}
USER_PARAMS_MISSING_GIT_URI = {
    "user": "user",
    "git_ref": "aaaaaaa",
}
USER_PARAMS_MISSING_GIT_REF = {
    "user": "user",
    "git_uri": "git://git/uri",
}
USER_PARAMS_ADDITIONAL_NONDEFINED_PROPERTY = {
    "user": "user",
    "git_uri": "git://git/uri",
    "git_ref": "aaaaaaa",
    "additional_random_property": "value",
}

OPERATOR_CSV_MODIFICATIONS_MINIMAL = {
    "pullspec_replacements": [
        {
            "original": "registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-cluster-etcd-operator:v4.5.0-202003080931", # noqa
            "new": "registry.redhat.io/openshift4/cluster-etcd-rhel7-operator@sha256:4070728c2ed8ad8e651b32551dec3e3fc7f78c3c96589c9150c0cb7c84285001", # noqa
            "pinned": False,
        }
    ]
}

OPERATOR_CSV_MODIFICATIONS_MISSING_ORIGINAL_PULLSPEC = {
    "pullspec_replacements": [
        {
            "new": "registry.redhat.io/openshift4/cluster-etcd-rhel7-operator@sha256:4070728c2ed8ad8e651b32551dec3e3fc7f78c3c96589c9150c0cb7c84285001", # noqa
            "pinned": False,
        }
    ]
}
OPERATOR_CSV_MODIFICATIONS_MISSING_NEW_PULLSPEC = {
    "pullspec_replacements": [
        {
            "original": "registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-cluster-etcd-operator:v4.5.0-202003080931", # noqa
            "pinned": False,
        }
    ]
}
OPERATOR_CSV_MODIFICATIONS_MISSING_PINNED_PULLSPEC = {
    "pullspec_replacements": [
        {
            "original": "registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-cluster-etcd-operator:v4.5.0-202003080931", # noqa
            "new": "registry.redhat.io/openshift4/cluster-etcd-rhel7-operator@sha256:4070728c2ed8ad8e651b32551dec3e3fc7f78c3c96589c9150c0cb7c84285001", # noqa
        }
    ]
}

OPERATOR_CSV_MODIFICATIONS_ADDITIONAL_NONDEFINED_PROPERTY = {
    "pullspec_replacements": [
        {
            "original": "registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-cluster-etcd-operator:v4.5.0-202003080931", # noqa
            "new": "registry.redhat.io/openshift4/cluster-etcd-rhel7-operator@sha256:4070728c2ed8ad8e651b32551dec3e3fc7f78c3c96589c9150c0cb7c84285001", # noqa
            "pinned": False,
        }
    ],
    "additional_random_property": "value",
}

OPERATOR_CSV_MODIFICATIONS_ALL_PROPERTIES = {
    "pullspec_replacements": [
        {
            "original": "registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-cluster-etcd-operator:v4.5.0-202003080931", # noqa
            "new": "registry.redhat.io/openshift4/cluster-etcd-rhel7-operator@sha256:4070728c2ed8ad8e651b32551dec3e3fc7f78c3c96589c9150c0cb7c84285001", # noqa
            "pinned": False,
        }
    ],
    "append": {"spec": {"skips": ["etcdoperator.v0.9.2"]}},
    "update": {
        "metadata": {
            "name": "etcdoperator.v1.0.0-patched",
            "annotations": {"olm.substitutesFor": "v0.9.2"},
        },
        "spec": {"version": "1.0.0-01610399900-patched"},
    },
}


def get_workflow_data_json():
    tag_conf = TagConf()
    tag_conf.add_floating_image(ImageName.parse("registry/image:latest"))
    tag_conf.add_primary_image(ImageName.parse("registry/image:1.0"))

    wf_data = ImageBuildWorkflowData(
        dockerfile_images=DockerfileImages(["scratch", "registry/f:35"]),
        # Test object in dict values is serialized
        plugins_results={
            "image_build": {"logs": ["Build succeeds."]},
            "tag_and_push": [
                # Such object in a list should be handled properly.
                ImageName(registry="localhost:5000", repo='image', tag='latest'),
            ],
            "plugin_a": {
                'parent-images-koji-builds': {
                    ImageName(repo='base', tag='latest').to_str(): {
                        'id': 123456789,
                        'nvr': 'base-image-1.0-99',
                        'state': 1,
                    },
                },
            },
        },
        tag_conf=tag_conf,
        koji_upload_files=[
            {
                "local_filename": "/path/to/build1.log",
                "dest_filename": "x86_64-build.log",
            },
            {
                "local_filename": "/path/to/dir1/remote-source.tar.gz",
                "dest_filename": "remote-source.tar.gz",
            },
        ]
    )

    wf_data.image_components = {'x86_64': [{'type': 'rpm', 'name': 'python-docker-py',
                                            'version': '1.3.1', 'release': '1.fc24',
                                            'arch': 'noarch',
                                            'sigmd5': '7c1f60d8cde73e97a45e0c489f4a3b26',
                                            'signature': None, 'epoch': None},
                                           {'type': 'rpm', 'name': 'fedora-repos-rawhide',
                                            'version': '24', 'release': '0.1', 'arch': 'noarch',
                                            'sigmd5': 'd41df1e059544d906363605d47477e60',
                                            'signature': None, 'epoch': None},
                                           {'type': 'rpm', 'name': 'gpg-pubkey-doc',
                                            'version': '1.0', 'release': '1', 'arch': 'noarch',
                                            'sigmd5': '00000000000000000000000000000000',
                                            'signature': None, 'epoch': None}],
                                'ppc64le': [{'type': 'rpm', 'name': 'python-docker-py',
                                             'version': '1.3.1', 'release': '1.fc24',
                                             'arch': 'noarch',
                                             'sigmd5': '7c1f60d8cde73e97a45e0c489f4a3b26',
                                             'signature': None, 'epoch': None},
                                            {'type': 'rpm', 'name': 'fedora-repos-rawhide',
                                             'version': '24', 'release': '0.1', 'arch': 'noarch',
                                             'sigmd5': 'd41df1e059544d906363605d47477e60',
                                             'signature': None, 'epoch': None},
                                            {'type': 'rpm', 'name': 'gpg-pubkey-doc',
                                             'version': '1.0', 'release': '1', 'arch': 'noarch',
                                             'sigmd5': '00000000000000000000000000000000',
                                             'signature': None, 'epoch': None}],
                                }

    with TemporaryDirectory() as d:
        with open(os.path.join(d, 'workflow_data.json'), 'w') as f:
            json.dump(wf_data.as_dict(), f, cls=WorkflowDataEncoder)
        with open(os.path.join(d, 'workflow_data.json')) as f:
            workflow_json = json.load(f)

    return workflow_json


RPM_COMPONENTS_WORKFLOW_DATA = get_workflow_data_json()

CONTENT_MANIFEST_MINIMAL = {
    "metadata": {
        "icm_spec": "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/content_manifest.json", # noqa
        "icm_version": 1,
    },
}

CONTENT_MANIFEST_MISSING_ICM_SPEC = {
    "metadata": {
        "icm_version": 1,
    },
}

CONTENT_MANIFEST_MISSING_ICM_VERSION = {
    "metadata": {
        "icm_spec": "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/content_manifest.json", # noqa
    },
}

CONTENT_MANIFEST_ALL_PROPERTIES = {
    "image_contents": [
        {
            "dependencies": [
                {"purl": "pkg:pypi/six@1.15.0"},
            ],
            "purl": "pkg:generic/osbs/cachito-pip-with-deps?vcs_url=https%3A%2F%2Fgitlab.cee.redhat.com%2Fosbs%2Fosbs-test-cachito-project.git%40b3b2684a42971a6a7afc2b88106d908920519512", # noqa
            "sources": [
                {"purl": "pkg:pypi/six@1.15.0"},
            ],
        }
    ],
    "content_sets": ["nodejs-rpms", "extra-rpms"],
    "metadata": {
        "icm_spec": "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/content_manifest.json", # noqa
        "icm_version": 1,
        "image_layer_index": -1,
    },
}

CONTENT_MANIFEST_MISSING_IMAGE_CONTENTS_PURL = {
    "image_contents": [
        {
            "dependencies": [
                {"purl": "pkg:pypi/six@1.15.0"},
            ],
            "sources": [
                {"purl": "pkg:pypi/six@1.15.0"},
            ],
        }
    ],
    "content_sets": ["nodejs-rpms", "extra-rpms"],
    "metadata": {
        "icm_spec": "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/content_manifest.json", # noqa
        "icm_version": 1,
        "image_layer_index": -1,
    },
}

CONTENT_MANIFEST_MISSING_DEPENDENCIES_PURL = {
    "image_contents": [
        {
            "dependencies": [],
            "purl": "pkg:generic/osbs/cachito-pip-with-deps?vcs_url=https%3A%2F%2Fgitlab.cee.redhat.com%2Fosbs%2Fosbs-test-cachito-project.git%40b3b2684a42971a6a7afc2b88106d908920519512", # noqa
            "sources": [
                {"purl": "pkg:pypi/six@1.15.0"},
            ],
        }
    ],
    "content_sets": ["nodejs-rpms", "extra-rpms"],
    "metadata": {
        "icm_spec": "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/content_manifest.json", # noqa
        "icm_version": 1,
        "image_layer_index": -1,
    },
}
CONTENT_MANIFEST_MISSING_SOURCES_PURL = {
    "image_contents": [
        {
            "dependencies": [
                {"purl": "pkg:pypi/six@1.15.0"},
            ],
            "purl": "pkg:generic/osbs/cachito-pip-with-deps?vcs_url=https%3A%2F%2Fgitlab.cee.redhat.com%2Fosbs%2Fosbs-test-cachito-project.git%40b3b2684a42971a6a7afc2b88106d908920519512", # noqa
            "sources": [],
        }
    ],
    "content_sets": ["nodejs-rpms", "extra-rpms"],
    "metadata": {
        "icm_spec": "https://raw.githubusercontent.com/content_manifest.json",
        "icm_version": 1,
        "image_layer_index": -1,
    },
}


@pytest.mark.parametrize(
    "data, schema, err_message",
    [
        # source_containers_user_params.json
        ({}, "schemas/source_containers_user_params.json", "validating 'required' has failed"),
        (SOURCE_CONTAINERS_USER_PARAMS_MINIMAL, "schemas/source_containers_user_params.json", None),
        (SOURCE_CONTAINERS_USER_PARAMS_ALL_PROPERTIES,
         "schemas/source_containers_user_params.json", None),
        (SOURCE_CONTAINERS_USER_PARAMS_MISSING_USER,
         "schemas/source_containers_user_params.json",
         r"validating 'required' has failed \('user' is a required property\)"),
        (SOURCE_CONTAINERS_USER_PARAMS_ADDITIONAL_NONDEFINED_PROPERTY,
         "schemas/source_containers_user_params.json", None),

        # user_params.json
        (USER_PARAMS_MINIMAL, "schemas/user_params.json", None),
        (USER_PARAMS_MISSING_USER,
         "schemas/user_params.json",
         r"validating 'required' has failed \('user' is a required property\)"),
        (USER_PARAMS_MISSING_GIT_URI,
         "schemas/user_params.json",
         r"validating 'required' has failed \('git_uri' is a required property\)"),
        (USER_PARAMS_MISSING_GIT_REF,
         "schemas/user_params.json",
         r"validating 'required' has failed \('git_ref' is a required property\)"),
        (USER_PARAMS_ADDITIONAL_NONDEFINED_PROPERTY,
         "schemas/user_params.json", None),
        ({}, "schemas/user_params.json", "validating 'required' has failed"),

        # operator_csv_modifications.json
        ({}, "schemas/operator_csv_modifications.json", "validating 'required' has failed"),
        (OPERATOR_CSV_MODIFICATIONS_MINIMAL, "schemas/operator_csv_modifications.json", None),
        (OPERATOR_CSV_MODIFICATIONS_MISSING_ORIGINAL_PULLSPEC,
         "schemas/operator_csv_modifications.json",
         r"validating 'required' has failed \('original' is a required property\)"),
        (OPERATOR_CSV_MODIFICATIONS_MISSING_NEW_PULLSPEC, "schemas/operator_csv_modifications.json",
         r"validating 'required' has failed \('new' is a required property\)"),
        (OPERATOR_CSV_MODIFICATIONS_MISSING_PINNED_PULLSPEC,
         "schemas/operator_csv_modifications.json",
         r"validating 'required' has failed \('pinned' is a required property\)"),
        ({"pullspec_replacements": []},
         "schemas/operator_csv_modifications.json", None),
        (OPERATOR_CSV_MODIFICATIONS_ADDITIONAL_NONDEFINED_PROPERTY,
         "schemas/operator_csv_modifications.json",
         r"Additional properties are not allowed \('additional_random_property' was unexpected\)"),
        (OPERATOR_CSV_MODIFICATIONS_ALL_PROPERTIES,
         "schemas/operator_csv_modifications.json", None),

        # content_manifest.json
        ({}, "schemas/content_manifest.json",
         r"validating 'required' has failed \('metadata' is a required property\)"),
        ({"metadata": {}}, "schemas/content_manifest.json", "validating 'required' has failed"),
        (RPM_COMPONENTS_WORKFLOW_DATA, "schemas/workflow_data.json", None),
        (CONTENT_MANIFEST_MINIMAL, "schemas/content_manifest.json", None),
        (CONTENT_MANIFEST_MISSING_ICM_VERSION,
         "schemas/content_manifest.json",
         r"validating 'required' has failed \('icm_version' is a required property\)"),
        (CONTENT_MANIFEST_MISSING_ICM_SPEC,
         "schemas/content_manifest.json",
         r"validating 'required' has failed \('icm_spec' is a required property\)"),
        (CONTENT_MANIFEST_ALL_PROPERTIES, "schemas/content_manifest.json", None),
        (CONTENT_MANIFEST_MISSING_IMAGE_CONTENTS_PURL, "schemas/content_manifest.json",
         r".image_contents\[0\]: validating 'required' has failed \('purl' is a required property\)"), # noqa
        (CONTENT_MANIFEST_MISSING_DEPENDENCIES_PURL,
         "schemas/content_manifest.json", None),
        (CONTENT_MANIFEST_MISSING_SOURCES_PURL,
         "schemas/content_manifest.json", None),
    ],
)
def test_schema(data, schema, err_message):
    if not err_message:
        validate_with_schema(data, schema)
    else:
        with pytest.raises(OsbsValidationException, match=err_message):
            validate_with_schema(data, schema)
