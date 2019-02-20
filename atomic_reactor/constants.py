"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os

DOCKER_SOCKET_PATH = '/var/run/docker.sock'
DOCKERFILE_FILENAME = 'Dockerfile'
BUILD_JSON = 'build.json'
BUILD_JSON_ENV = 'BUILD_JSON'
RESULTS_JSON = 'results.json'
METADATA_TAG = "_metadata_"

CONTAINER_SHARE_PATH = '/run/share/'
CONTAINER_SHARE_SOURCE_SUBDIR = 'source'
CONTAINER_SECRET_PATH = ''
CONTAINER_BUILD_JSON_PATH = os.path.join(CONTAINER_SHARE_PATH, BUILD_JSON)
CONTAINER_RESULTS_JSON_PATH = os.path.join(CONTAINER_SHARE_PATH, RESULTS_JSON)
CONTAINER_DOCKERFILE_PATH = os.path.join(CONTAINER_SHARE_PATH, DOCKERFILE_FILENAME)

CONTAINER_IMAGEBUILDER_BUILD_METHOD = 'imagebuilder'
CONTAINER_DOCKERPY_BUILD_METHOD = 'docker_api'
CONTAINER_BUILD_METHODS = (CONTAINER_IMAGEBUILDER_BUILD_METHOD, CONTAINER_DOCKERPY_BUILD_METHOD)
CONTAINER_DEFAULT_BUILD_METHOD = CONTAINER_DOCKERPY_BUILD_METHOD  # ... for now.

HOST_SECRET_PATH = ''

EXPORTED_SQUASHED_IMAGE_NAME = 'image.tar'
EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE = 'compressed.tar.{0}'

YUM_REPOS_DIR = '/etc/yum.repos.d/'
RELATIVE_REPOS_PATH = "atomic-reactor-repos/"
DEFAULT_YUM_REPOFILE_NAME = 'atomic-reactor-injected.repo'

# key in dictionary returned by "docker inspect" that holds the image
# configuration (such as labels)
INSPECT_CONFIG = "Config"
# key that holds the RootFS
INSPECT_ROOTFS = "RootFS"
# key that holds the layer diff_ids
INSPECT_ROOTFS_LAYERS = 'Layers'

# docs constants

DESCRIPTION = "Python library with command line interface for building docker images."
HOMEPAGE = "https://github.com/projectatomic/atomic-reactor"
PROG = "atomic-reactor"
MANPAGE_AUTHORS = "Jiri Popelka <jpopelka@redhat.com>, " \
                  "Martin Milata <mmilata@redhat.com>, " \
                  "Slavek Kabrda <slavek@redhat.com>, " \
                  "Tim Waugh <twaugh@redhat.com>, " \
                  "Tomas Tomecek <ttomecek@redhat.com>"
MANPAGE_SECTION = 1


# debug print of tools reactor uses

TOOLS_USED = (
    {"pkg_name": "docker", "display_name": "docker-py"},
    {"pkg_name": "docker_squash"},
    {"pkg_name": "atomic_reactor"},
    {"pkg_name": "osbs", "display_name": "osbs-client"},
    {"pkg_name": "dockpulp"},
)

DEFAULT_DOWNLOAD_BLOCK_SIZE = 10 * 1024 * 1024  # 10Mb

TAG_NAME_REGEX = r'^[\w][\w.-]{0,127}$'

IMAGE_TYPE_DOCKER_ARCHIVE = 'docker-archive'
IMAGE_TYPE_OCI = 'oci'
IMAGE_TYPE_OCI_TAR = 'oci-tar'

PLUGIN_KOJI_PROMOTE_PLUGIN_KEY = 'koji_promote'
PLUGIN_KOJI_IMPORT_PLUGIN_KEY = 'koji_import'
PLUGIN_KOJI_UPLOAD_PLUGIN_KEY = 'koji_upload'
PLUGIN_KOJI_TAG_BUILD_KEY = 'koji_tag_build'
PLUGIN_PULP_PUBLISH_KEY = 'pulp_publish'
PLUGIN_PULP_PUSH_KEY = 'pulp_push'
PLUGIN_PULP_SYNC_KEY = 'pulp_sync'
PLUGIN_PULP_PULL_KEY = 'pulp_pull'
PLUGIN_PULP_TAG_KEY = 'pulp_tag'
PLUGIN_ADD_FILESYSTEM_KEY = 'add_filesystem'
PLUGIN_BUMP_RELEASE_KEY = 'bump_release'
PLUGIN_DELETE_FROM_REG_KEY = 'delete_from_registry'
PLUGIN_DISTGIT_FETCH_KEY = 'distgit_fetch_artefacts'
PLUGIN_DOCKERFILE_CONTENT_KEY = 'dockerfile_content'
PLUGIN_FETCH_MAVEN_KEY = 'fetch_maven_artifacts'
PLUGIN_FETCH_WORKER_METADATA_KEY = 'fetch_worker_metadata'
PLUGIN_GROUP_MANIFESTS_KEY = 'group_manifests'
PLUGIN_INJECT_PARENT_IMAGE_KEY = 'inject_parent_image'
PLUGIN_BUILD_ORCHESTRATE_KEY = 'orchestrate_build'
PLUGIN_KOJI_PARENT_KEY = 'koji_parent'
PLUGIN_COMPARE_COMPONENTS_KEY = 'compare_components'
PLUGIN_CHECK_AND_SET_PLATFORMS_KEY = 'check_and_set_platforms'
PLUGIN_REMOVE_WORKER_METADATA_KEY = 'remove_worker_metadata'
PLUGIN_RESOLVE_COMPOSES_KEY = 'resolve_composes'
PLUGIN_SENDMAIL_KEY = 'sendmail'
PLUGIN_VERIFY_MEDIA_KEY = 'verify_media'
PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY = 'export_operator_manifests'

# some shared dict keys for build metadata that gets recorded with koji.
# for consistency of metadata in historical builds, these values basically cannot change.
# however constant names could change to more accurately reflect current semantics.
BASE_IMAGE_KOJI_BUILD = 'parent-image-koji-build'  # from when the base image was the only parent
PARENT_IMAGES_KOJI_BUILDS = 'parent-images-koji-builds'
BASE_IMAGE_BUILD_ID_KEY = 'parent_build_id'  # from when the base image was the only parent
PARENT_IMAGE_BUILDS_KEY = 'parent_image_builds'
PARENT_IMAGES_KEY = 'parent_images'
SCRATCH_FROM = 'scratch'

# max retries for docker requests
DOCKER_MAX_RETRIES = 3
# how many seconds should wait before another try of docker request
DOCKER_BACKOFF_FACTOR = 5
# docker retries statuses
DOCKER_CLIENT_STATUS_RETRY = (408, 500, 502, 503, 504)
# max retries for docker push
DOCKER_PUSH_MAX_RETRIES = 6
# how many seconds should wait before another try of docker push
DOCKER_PUSH_BACKOFF_FACTOR = 5
# max retries for http requests
HTTP_MAX_RETRIES = 3
# how many seconds should wait before another try of http request
HTTP_BACKOFF_FACTOR = 5
# http retries statuses
HTTP_CLIENT_STATUS_RETRY = (408, 500, 502, 503, 504)
# requests timeout in seconds
HTTP_REQUEST_TIMEOUT = 600
# max retries for git clone
GIT_MAX_RETRIES = 3
# how many seconds should wait before another try of git clone
GIT_BACKOFF_FACTOR = 5
# max retries for creating 'lock' repository
LOCKEDPULPREPOSITORY_RETRIES = 10
# how many seconds to wait before 1st retry; doubles each retry
LOCKEDPULPREPOSITORY_BACKOFF = 5

# Media types
MEDIA_TYPE_DOCKER_V1 = "application/json"
MEDIA_TYPE_DOCKER_V2_SCHEMA1 = "application/vnd.docker.distribution.manifest.v1+json"
MEDIA_TYPE_DOCKER_V2_SCHEMA2 = "application/vnd.docker.distribution.manifest.v2+json"
MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MEDIA_TYPE_OCI_V1 = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_V1_INDEX = "application/vnd.oci.image.index.v1+json"

REPO_CONTAINER_CONFIG = 'container.yaml'
REPO_CONTENT_SETS_CONFIG = 'content_sets.yml'

DOCKERIGNORE = '.dockerignore'

# Operator manifest constants
OPERATOR_MANIFESTS_ARCHIVE = 'operator_manifests.zip'
