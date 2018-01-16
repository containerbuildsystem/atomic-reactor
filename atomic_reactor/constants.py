"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os

DOCKER_SOCKET_PATH = '/var/run/docker.sock'
DOCKERFILE_FILENAME = 'Dockerfile'
FLATPAK_FILENAME = 'flatpak.json'
BUILD_JSON = 'build.json'
BUILD_JSON_ENV = 'BUILD_JSON'
RESULTS_JSON = 'results.json'

CONTAINER_SHARE_PATH = '/run/share/'
CONTAINER_SHARE_SOURCE_SUBDIR = 'source'
CONTAINER_SECRET_PATH = ''
CONTAINER_BUILD_JSON_PATH = os.path.join(CONTAINER_SHARE_PATH, BUILD_JSON)
CONTAINER_RESULTS_JSON_PATH = os.path.join(CONTAINER_SHARE_PATH, RESULTS_JSON)
CONTAINER_DOCKERFILE_PATH = os.path.join(CONTAINER_SHARE_PATH, DOCKERFILE_FILENAME)

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
PLUGIN_FETCH_WORKER_METADATA_KEY = 'fetch_worker_metadata'
PLUGIN_GROUP_MANIFESTS_KEY = 'group_manifests'
PLUGIN_BUILD_ORCHESTRATE_KEY = 'orchestrate_build'
PLUGIN_KOJI_PARENT_KEY = 'koji_parent'
PLUGIN_COMPARE_COMPONENTS_KEY = 'compare_components'
PLUGIN_REMOVE_WORKER_METADATA_KEY = 'remove_worker_metadata'
PLUGIN_RESOLVE_COMPOSES_KEY = 'resolve_composes'

# max retries for docker requests
DOCKER_MAX_RETRIES = 3
# how many seconds should wait before another try of docker request
DOCKER_BACKOFF_FACTOR = 5
# docker retries statuses
DOCKER_CLIENT_STATUS_RETRY = (408, 500, 502, 503, 504)
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


# Media types
MEDIA_TYPE_DOCKER_V1 = "application/json"
MEDIA_TYPE_DOCKER_V2_SCHEMA1 = "application/vnd.docker.distribution.manifest.v1+json"
MEDIA_TYPE_DOCKER_V2_SCHEMA2 = "application/vnd.docker.distribution.manifest.v2+json"
MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MEDIA_TYPE_OCI_V1 = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_V1_INDEX = "application/vnd.oci.image.index.v1+json"

REPO_CONTAINER_CONFIG = 'container.yaml'
