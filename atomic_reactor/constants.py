"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os

DOCKER_SOCKET_PATH = '/var/run/docker.sock'
DOCKERFILE_FILENAME = 'Dockerfile'
CACHITO_ENV_FILENAME = 'cachito.env'
CACHITO_ENV_ARG_ALIAS = 'CACHITO_ENV_FILE'
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

CONTAINER_BUILDAH_BUILD_METHOD = 'buildah_bud'
CONTAINER_IMAGEBUILDER_BUILD_METHOD = 'imagebuilder'
CONTAINER_DOCKERPY_BUILD_METHOD = 'docker_api'
CONTAINER_BUILD_METHODS = (
    CONTAINER_BUILDAH_BUILD_METHOD,
    CONTAINER_IMAGEBUILDER_BUILD_METHOD,
    CONTAINER_DOCKERPY_BUILD_METHOD
)
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
HOMEPAGE = "https://github.com/containerbuildsystem/atomic-reactor"
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
)

DEFAULT_DOWNLOAD_BLOCK_SIZE = 10 * 1024 * 1024  # 10Mb

TAG_NAME_REGEX = r'^[\w][\w.-]{0,127}$'

IMAGE_TYPE_DOCKER_ARCHIVE = 'docker-archive'
IMAGE_TYPE_OCI = 'oci'
IMAGE_TYPE_OCI_TAR = 'oci-tar'

PLUGIN_KOJI_PROMOTE_PLUGIN_KEY = 'koji_promote'
PLUGIN_KOJI_IMPORT_PLUGIN_KEY = 'koji_import'
PLUGIN_KOJI_IMPORT_SOURCE_CONTAINER_PLUGIN_KEY = 'koji_import_source_container'
PLUGIN_KOJI_UPLOAD_PLUGIN_KEY = 'koji_upload'
PLUGIN_KOJI_TAG_BUILD_KEY = 'koji_tag_build'
PLUGIN_ADD_FILESYSTEM_KEY = 'add_filesystem'
PLUGIN_BUMP_RELEASE_KEY = 'bump_release'
PLUGIN_DISTGIT_FETCH_KEY = 'distgit_fetch_artefacts'
PLUGIN_FETCH_MAVEN_KEY = 'fetch_maven_artifacts'
PLUGIN_FETCH_WORKER_METADATA_KEY = 'fetch_worker_metadata'
PLUGIN_GROUP_MANIFESTS_KEY = 'group_manifests'
PLUGIN_INJECT_PARENT_IMAGE_KEY = 'inject_parent_image'
PLUGIN_BUILD_ORCHESTRATE_KEY = 'orchestrate_build'
PLUGIN_KOJI_PARENT_KEY = 'koji_parent'
PLUGIN_COMPARE_COMPONENTS_KEY = 'compare_components'
PLUGIN_CHECK_AND_SET_PLATFORMS_KEY = 'check_and_set_platforms'
PLUGIN_CHECK_USER_SETTINGS = 'check_user_settings'
PLUGIN_REMOVE_WORKER_METADATA_KEY = 'remove_worker_metadata'
PLUGIN_RESOLVE_COMPOSES_KEY = 'resolve_composes'
PLUGIN_RESOLVE_REMOTE_SOURCE = 'resolve_remote_source'
PLUGIN_SENDMAIL_KEY = 'sendmail'
PLUGIN_VERIFY_MEDIA_KEY = 'verify_media'
PLUGIN_PIN_OPERATOR_DIGESTS_KEY = 'pin_operator_digest'
PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY = 'export_operator_manifests'
PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY = 'push_operator_manifests'
PLUGIN_SOURCE_CONTAINER_KEY = 'source_container'
PLUGIN_FETCH_SOURCES_KEY = 'fetch_sources'
PLUGIN_KOJI_DELEGATE_KEY = 'koji_delegate'
PLUGIN_PUSH_FLOATING_TAGS_KEY = 'push_floating_tags'
PLUGIN_ADD_IMAGE_CONTENT_MANIFEST = 'add_image_content_manifest'

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
HTTP_CLIENT_STATUS_RETRY = (408, 429, 500, 502, 503, 504)
# requests timeout in seconds
HTTP_REQUEST_TIMEOUT = 600
# max retries for git clone
GIT_MAX_RETRIES = 3
# how many seconds should wait before another try of git clone
GIT_BACKOFF_FACTOR = 5
# max retries for reserving koji builds
KOJI_RESERVE_MAX_RETRIES = 20
# wait for 2sec (usual time of bump_release with reserve)
KOJI_RESERVE_RETRY_DELAY = 2
KOJI_MAX_RETRIES = 120
KOJI_RETRY_INTERVAL = 60
KOJI_OFFLINE_RETRY_INTERVAL = 120

# Media types
MEDIA_TYPE_DOCKER_V2_SCHEMA1 = "application/vnd.docker.distribution.manifest.v1+json"
MEDIA_TYPE_DOCKER_V2_SCHEMA2 = "application/vnd.docker.distribution.manifest.v2+json"
MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MEDIA_TYPE_OCI_V1 = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_V1_INDEX = "application/vnd.oci.image.index.v1+json"

REPO_CONTAINER_CONFIG = 'container.yaml'
REPO_CONTENT_SETS_CONFIG = 'content_sets.yml'
REPO_FETCH_ARTIFACTS_URL = 'fetch-artifacts-url.yaml'
REPO_FETCH_ARTIFACTS_KOJI = 'fetch-artifacts-koji.yaml'

DOCKERIGNORE = '.dockerignore'

# Operator manifest constants
OPERATOR_MANIFESTS_ARCHIVE = 'operator_manifests.zip'

KOJI_BTYPE_IMAGE = 'image'
KOJI_BTYPE_OPERATOR_MANIFESTS = 'operator-manifests'
KOJI_BTYPE_REMOTE_SOURCES = 'remote-sources'

# Path to where the remote source bundle is copied to during the build process
REMOTE_SOURCE_DIR = '/remote-source'

# Name of downloaded remote sources tarball
REMOTE_SOURCES_FILENAME = 'remote-source.tar.gz'

# koji osbs_build metadata
KOJI_KIND_IMAGE_BUILD = 'container_build'
KOJI_KIND_IMAGE_SOURCE_BUILD = 'source_container_build'
KOJI_SUBTYPE_OP_APPREGISTRY = 'operator_appregistry'
KOJI_SUBTYPE_OP_BUNDLE = 'operator_bundle'
KOJI_SOURCE_ENGINE = 'bsi'

# Storage names as defined in skopeo
DOCKER_STORAGE_TRANSPORT_NAME = 'docker-daemon'

# location for build info directory in the image
IMAGE_BUILD_INFO_DIR = '/root/buildinfo/'

USER_CONFIG_FILES = {
    # filename: json schema file
    REPO_FETCH_ARTIFACTS_URL: 'schemas/fetch-artifacts-url.json',
    REPO_FETCH_ARTIFACTS_KOJI: 'schemas/fetch-artifacts-nvr.json',
    REPO_CONTENT_SETS_CONFIG: 'schemas/content_sets.json',
}
