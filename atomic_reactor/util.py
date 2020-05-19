"""
Copyright (c) 2015, 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import hashlib
from itertools import chain
import json
import io
import os
import re
import sys
import requests
from requests.exceptions import SSLError, HTTPError, RetryError
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import shutil
import tempfile
import logging
import uuid
import yaml
import string
import signal
import traceback
from collections import namedtuple
from copy import deepcopy
from base64 import b64decode

from six.moves.urllib.parse import urlparse
from six import PY2

from atomic_reactor.constants import (DOCKERFILE_FILENAME, REPO_CONTAINER_CONFIG, TOOLS_USED,
                                      INSPECT_CONFIG,
                                      IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR,
                                      HTTP_MAX_RETRIES, HTTP_BACKOFF_FACTOR,
                                      HTTP_CLIENT_STATUS_RETRY, HTTP_REQUEST_TIMEOUT,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1, MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST, MEDIA_TYPE_OCI_V1,
                                      MEDIA_TYPE_OCI_V1_INDEX,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                                      PLUGIN_KOJI_PARENT_KEY,
                                      PARENT_IMAGE_BUILDS_KEY, PARENT_IMAGES_KOJI_BUILDS,
                                      BASE_IMAGE_KOJI_BUILD, BASE_IMAGE_BUILD_ID_KEY,
                                      PARENT_IMAGES_KEY, SCRATCH_FROM, RELATIVE_REPOS_PATH,
                                      DOCKERIGNORE, DEFAULT_DOWNLOAD_BLOCK_SIZE)
from atomic_reactor.auth import HTTPRegistryAuth

from dockerfile_parse import DockerfileParser

from importlib import import_module
from requests.utils import guess_json_utf

from osbs.exceptions import OsbsException
from osbs.utils import clone_git_repo, reset_git_repo, Labels, ImageName
from osbs.utils.yaml import read_yaml as osbs_read_yaml

from tempfile import NamedTemporaryFile
try:
    # py3
    from faulthandler import dump_traceback
except ImportError:
    # py2
    import thread

    def dump_traceback():
        frames = sys._current_frames()
        th_traces = []
        for th_ident, frame in frames.items():
            trace_entries = traceback.format_stack(frame)
            # Comply with faulthandler output
            context_str = 'Thread'
            if th_ident == thread.get_ident():
                context_str = 'Current thread'
            trace_header = '%s 0x%x (most recent call first):' % (context_str, th_ident)
            trace_entries.append(trace_header)
            trace_entries.reverse()
            pretty_trace_entries = [s.split('\n')[0] for s in trace_entries]
            th_traces.append('\n'.join(pretty_trace_entries))
        print('\n\n'.join(th_traces), file=sys.stderr)

Output = namedtuple('Output', ['file', 'metadata'])

logger = logging.getLogger(__name__)


def figure_out_build_file(absolute_path, local_path=None):
    """
    try to figure out the build file (Dockerfile or just a container.yaml) from provided
    path and optionally from relative local path this is meant to be used with
    git repo: absolute_path is path to git repo, local_path is path to dockerfile
    within git repo

    :param absolute_path:
    :param local_path:
    :return: tuple, (dockerfile_path, dir_with_dockerfile_path)
    """
    logger.info("searching for dockerfile in '%s' (local path %s)", absolute_path, local_path)
    logger.debug("abs path = '%s', local path = '%s'", absolute_path, local_path)
    if local_path:
        if local_path.endswith(DOCKERFILE_FILENAME) or local_path.endswith(REPO_CONTAINER_CONFIG):
            git_build_file_dir = os.path.dirname(local_path)
            build_file_dir = os.path.abspath(os.path.join(absolute_path, git_build_file_dir))
        else:
            build_file_dir = os.path.abspath(os.path.join(absolute_path, local_path))
    else:
        build_file_dir = os.path.abspath(absolute_path)
    if not os.path.isdir(build_file_dir):
        raise IOError("Directory '%s' doesn't exist." % build_file_dir)
    build_file_path = os.path.join(build_file_dir, DOCKERFILE_FILENAME)
    if os.path.isfile(build_file_path):
        logger.debug("Dockerfile found: '%s'", build_file_path)
        return build_file_path, build_file_dir
    build_file_path = os.path.join(build_file_dir, REPO_CONTAINER_CONFIG)
    if os.path.isfile(build_file_path):
        logger.debug("container.yaml found: '%s'", build_file_path)

        # Without this check, there would be a confusing 'Dockerfile has not yet been generated'
        # exception later.
        with open(build_file_path) as f:
            data = yaml.safe_load(f)
            if data is None or 'flatpak' not in data:
                raise RuntimeError("container.yaml found, but no accompanying Dockerfile")

        return build_file_path, build_file_dir
    raise IOError("Dockerfile '%s' doesn't exist." % os.path.join(build_file_dir,
                                                                  DOCKERFILE_FILENAME))


class CommandResult(object):
    def __init__(self):
        self._logs = []
        self._parsed_logs = []
        self._error = None
        self._error_detail = None

    def parse_item(self, item):
        """
        :param item: dict, decoded log data
        """
        # append here just in case .get bellow fails
        self._parsed_logs.append(item)

        # make sure the log item is a dictionary object
        if isinstance(item, dict):
            lines = item.get("stream", "")
        else:
            lines = item
            item = None

        for line in lines.splitlines():
            line = line.strip()
            self._logs.append(line)
            if line:
                logger.debug(line)

        if item is not None:
            self._error = item.get("error", None)
            self._error_detail = item.get("errorDetail", None)
            if self._error:
                logger.error(item)

    @property
    def parsed_logs(self):
        return self._parsed_logs

    @property
    def logs(self):
        return self._logs

    @property
    def error(self):
        return self._error

    @property
    def error_detail(self):
        return self._error_detail

    def is_failed(self):
        return bool(self.error) or bool(self.error_detail)


def wait_for_command(logs_generator):
    """
    Create a CommandResult from given iterator

    :return: CommandResult
    """
    logger.info("wait_for_command")
    cr = CommandResult()
    for item in logs_generator:
        cr.parse_item(item)

    logger.info("no more logs")
    return cr


class LazyGit(object):
    """
    usage:

        lazy_git = LazyGit(git_url="...")
        with lazy_git:
            laze_git.git_path

    or

        lazy_git = LazyGit(git_url="...", tmpdir=tmp_dir)
        lazy_git.git_path
    """
    def __init__(self, git_url, commit=None, tmpdir=None, branch=None, depth=None):
        self.git_url = git_url
        # provided commit ID/reference to check out
        self.commit = commit
        # commit ID of HEAD; we'll figure this out ourselves
        self._commit_id = None
        self.provided_tmpdir = tmpdir
        self.our_tmpdir = None
        self._git_path = None
        self._branch = branch
        self._git_depth = depth

    @property
    def _tmpdir(self):
        return self.provided_tmpdir or self.our_tmpdir

    @property
    def commit_id(self):
        return self._commit_id

    @property
    def git_path(self):
        if self._git_path is None:
            repo_data = clone_git_repo(self.git_url, self._tmpdir, self.commit,
                                       branch=self._branch, depth=self._git_depth)
            self._commit_id = repo_data.commit_id
            self._git_path = repo_data.repo_path
            self._git_depth = repo_data.commit_depth
        return self._git_path

    def __enter__(self):
        if not self.provided_tmpdir:
            self.our_tmpdir = tempfile.mkdtemp()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.provided_tmpdir:
            if self.our_tmpdir:
                shutil.rmtree(self.our_tmpdir)

    def reset(self, git_reference):
        self._commit_id, _ = reset_git_repo(self.git_path, git_reference)
        self.commit = git_reference


def escape_dollar(v):
    try:
        str_type = unicode
    except NameError:
        str_type = str
    if isinstance(v, str_type):
        return v.replace('$', r'\$')
    else:
        return v


def render_yum_repo(repo, escape_dollars=True):
    repo.setdefault("name", str(uuid.uuid4().hex[:6]))
    repo_name = repo["name"]
    logger.info("rendering repo '%s'", repo_name)
    rendered_repo = '[%s]\n' % repo_name
    for key, value in repo.items():
        if escape_dollars:
            value = escape_dollar(value)
        rendered_repo += "%s=%s\n" % (key, value)
    logger.info("rendered repo: %r", rendered_repo)
    return rendered_repo


def process_substitutions(mapping, substitutions):
    """Process `substitutions` for given `mapping` (modified in place)

    :param mapping: a dict
    :param substitutions: either a dict {key: value} or a list of ["key=value"] strings
        keys can use dotted notation to change to nested dicts

    Note: Plugin substitutions are processed differently - they are accepted in form of
        plugin_type.plugin_name.arg_name, even though that doesn't reflect the actual
        structure of given mapping.
    Also note: For non-plugin substitutions, additional dicts/key/value pairs
        are created on the way if they're missing. For plugin substitutions, only
        existing values can be changed (TODO: do we want to change this behaviour?).
    """
    def parse_val(v):
        # TODO: do we need to recognize numbers,lists,dicts?
        if v.lower() == 'true':
            return True
        elif v.lower() == 'false':
            return False
        elif v.lower() == 'none':
            return None
        return v

    if isinstance(substitutions, list):
        # if we got a list, get a {key: val} dict out of it
        substitutions = dict([s.split('=', 1) for s in substitutions])

    for key, val in substitutions.items():
        cur_dict = mapping
        key_parts = key.split('.')
        if key_parts[0].endswith('_plugins'):
            _process_plugin_substitution(mapping, key_parts, val)
        else:
            key_parts_without_last = key_parts[:-1]

            # now go down mapping, following the dotted path; create empty dicts on way
            for k in key_parts_without_last:
                if k in cur_dict:
                    if not isinstance(cur_dict[k], dict):
                        cur_dict[k] = {}
                else:
                    cur_dict[k] = {}
                cur_dict = cur_dict[k]
            cur_dict[key_parts[-1]] = parse_val(val)


def _process_plugin_substitution(mapping, key_parts, value):
    try:
        plugin_type, plugin_name, arg_name = key_parts
    except ValueError:
        logger.error("invalid absolute path '%s': it requires exactly three parts: "
                     "plugin type, plugin name, argument name (dot separated)",
                     key_parts)
        raise ValueError("invalid absolute path to plugin, it should be "
                         "plugin_type.plugin_name.argument_name")

    logger.debug("getting plugin conf for '%s' with type '%s'",
                 plugin_name, plugin_type)
    plugins_of_a_type = mapping.get(plugin_type, None)
    if plugins_of_a_type is None:
        logger.warning("there are no plugins with type '%s'",
                       plugin_type)
        return
    plugin_conf = [x for x in plugins_of_a_type if x['name'] == plugin_name]
    plugins_num = len(plugin_conf)
    if plugins_num == 1:
        if arg_name not in plugin_conf[0]['args']:
            logger.warning("no configuration value '%s' for plugin '%s', skipping",
                           arg_name, plugin_name)
            return
        logger.info("changing value '%s' of plugin '%s': '%s' -> '%s'",
                    arg_name, plugin_name, plugin_conf[0]['args'][arg_name], value)
        plugin_conf[0]['args'][arg_name] = value
    elif plugins_num <= 0:
        logger.warning("there is no configuration for plugin '%s', skipping substitution",
                       plugin_name)
    else:
        logger.error("there is no configuration for plugin '%s'",
                     plugin_name)
        raise RuntimeError("plugin '%s' was specified multiple (%d) times, can't pick one" %
                           (plugin_name, plugins_num))


def _compute_checksums(fd, hash_objs, blocksize=65536):
    """
    Compute file checksums in given hash objects.

    :param fd: file-like object
    :param hash_objs: list, hashlib hash objects for each algorithm to be calculated
    :param blocksize: block size used to read fd
    """
    buf = fd.read(blocksize)
    while len(buf) > 0:
        for hash_object in hash_objs:
            hash_object.update(buf)
        buf = fd.read(blocksize)


def get_checksums(filename, algorithms):
    """
    Compute a checksum(s) of given file using specified algorithms.

    :param filename: path to file or file-like object
    :param algorithms: list of cryptographic hash functions, currently supported: md5, sha256
    :return: dictionary
    """
    if not algorithms:
        return {}

    allowed_algorithms = ['md5', 'sha256']
    if not all(elem in allowed_algorithms for elem in algorithms):
        raise ValueError('Algorithms supported {}. Found {}'.format(allowed_algorithms, algorithms))

    hash_objs = [getattr(hashlib, algorithm)() for algorithm in algorithms]
    if hasattr(filename, 'read'):
        _compute_checksums(filename, hash_objs)
    else:
        with open(filename, mode='rb') as f:
            _compute_checksums(f, hash_objs)

    checksums = {}
    for hash_obj in hash_objs:
        sum_name = '{}sum'.format(hash_obj.name)
        checksums[sum_name] = hash_obj.hexdigest()
        logger.debug('%s: %s', sum_name, checksums[sum_name])
    return checksums


def get_docker_architecture(tasker):
    docker_version = tasker.get_version()
    host_arch = docker_version['Arch']
    if host_arch == 'amd64':
        host_arch = 'x86_64'
    return (host_arch, docker_version['Version'])


def get_exported_image_metadata(path, image_type):
    logger.info('getting metadata for exported image %s (%s)', path, image_type)
    metadata = {'path': path, 'type': image_type}
    if image_type != IMAGE_TYPE_OCI:
        metadata['size'] = os.path.getsize(path)
        logger.debug('size: %d bytes', metadata['size'])
        metadata.update(get_checksums(path, ['md5', 'sha256']))
    return metadata


def get_image_upload_filename(metadata, image_id, platform):
    saved_image = metadata.get('path')
    image_type = metadata.get('type')
    if image_type == IMAGE_TYPE_DOCKER_ARCHIVE:
        base_name = 'docker-image'
    elif image_type == IMAGE_TYPE_OCI_TAR:
        base_name = 'oci-image'
    else:
        raise ValueError("Unhandled image type for upload: {}".format(image_type))
    ext = saved_image.split('.', 1)[1]
    name_fmt = '{base_name}-{id}.{platform}.{ext}'
    return name_fmt.format(base_name=base_name, id=image_id,
                           platform=platform, ext=ext)


def get_version_of_tools():
    """
    get versions of tools reactor is using (specified in constants.TOOLS_USED)

    :returns list of dicts, [{"name": "docker-py", "version": "1.2.3"}, ...]
    """
    response = []
    for tool in TOOLS_USED:
        pkg_name = tool["pkg_name"]
        try:
            tool_module = import_module(pkg_name)
        except ImportError as ex:
            logger.warning("can't import module %s: %s", pkg_name, ex)
        else:
            version = getattr(tool_module, "__version__", None)
            if version is None:
                logger.warning("tool %s doesn't have __version__", pkg_name)
            else:
                response.append({
                    "name": tool.get("display_name", pkg_name),
                    "version": version,
                    "path": tool_module.__file__,
                })
    return response


def print_version_of_tools():
    """
    print versions of used tools to logger
    """
    logger.info("Using these tools:")
    for tool in get_version_of_tools():
        logger.info("%s-%s at %s", tool["name"], tool["version"], tool["path"])


def get_build_json():
    try:
        return json.loads(os.environ["BUILD"])
    except KeyError:
        logger.error("No $BUILD env variable. Probably not running in build container")
        raise


def is_scratch_build(workflow):
    return workflow.user_params.get('scratch', False)


def is_isolated_build(workflow):
    return workflow.user_params.get('isolated', False)


def is_flatpak_build(workflow):
    return workflow.user_params.get('flatpak', False)


def base_image_is_scratch(base_image_name):
    return SCRATCH_FROM == base_image_name


def base_image_is_custom(base_image_name):
    return bool(re.match('^koji/image-build(:.*)?$', base_image_name))


def get_orchestrator_platforms(workflow):
    try:
        orchestrate_build_plugin = workflow.get_orchestrate_build_plugin()
    except ValueError:
        # Not an orchestrator build
        return None
    else:
        return orchestrate_build_plugin['args']['platforms']


def get_platforms(workflow):
    koji_platforms = workflow.prebuild_results.get(PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
    if koji_platforms:
        return koji_platforms

    # Not an orchestrator build
    return None


# copypasted and slightly modified from
# http://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size/1094933#1094933
def human_size(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.2f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.2f %s%s" % (num, 'Yi', suffix)


def registry_hostname(registry):
    """
    Strip a reference to a registry to just the hostname:port
    """
    if registry.startswith('http:') or registry.startswith('https:'):
        return urlparse(registry).netloc
    else:
        return registry


class Dockercfg(object):
    def __init__(self, secret_path):
        """
        Create a new Dockercfg object from a .dockercfg/.dockerconfigjson file whose
        containing directory is secret_path.

        :param secret_path: str, dirname of .dockercfg/.dockerconfigjson location
        """

        if os.path.exists(os.path.join(secret_path, '.dockercfg')):
            self.json_secret_path = os.path.join(secret_path, '.dockercfg')
        elif os.path.exists(os.path.join(secret_path, '.dockerconfigjson')):
            self.json_secret_path = os.path.join(secret_path, '.dockerconfigjson')
        elif os.path.exists(secret_path):
            self.json_secret_path = secret_path
        else:
            raise RuntimeError("The registry secret was not found on the filesystem, "
                               "either .dockercfg or .dockerconfigjson are supported")
        try:
            with open(self.json_secret_path) as fp:
                self.json_secret = json.load(fp)

            # If the auths key exist then we have a dockerconfigjson secret.
            if 'auths' in self.json_secret:
                self.json_secret = self.json_secret.get('auths')

        except Exception:
            msg = "failed to read registry secret"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg)

    def get_credentials(self, docker_registry):
        # For maximal robustness we check the host:port of the passed in
        # registry against the host:port of the items in the secret. This is
        # somewhat similar to what the Docker CLI does.
        #
        docker_registry = registry_hostname(docker_registry)
        try:
            return self.json_secret[docker_registry]
        except KeyError:
            for reg, creds in self.json_secret.items():
                if registry_hostname(reg) == docker_registry:
                    return creds

            logger.warning('%s not found in .dockercfg', docker_registry)
            return {}

    def unpack_auth_b64(self, docker_registry):
        """Decode and unpack base64 'auth' credentials from config file.

        :param docker_registry: str, registry reference in config file

        :return: namedtuple, UnpackedAuth (or None if no 'auth' is available)
        """
        UnpackedAuth = namedtuple('UnpackedAuth', ['raw_str', 'username', 'password'])
        credentials = self.get_credentials(docker_registry)
        auth_b64 = credentials.get('auth')
        if auth_b64:
            raw_str = b64decode(auth_b64).decode('utf-8')
            unpacked_credentials = raw_str.split(':', 1)
            if len(unpacked_credentials) == 2:
                return UnpackedAuth(raw_str, *unpacked_credentials)
            else:
                raise ValueError("Failed to parse 'auth' in '%s'" % self.json_secret_path)


class RegistrySession(object):
    def __init__(self, registry, insecure=False, dockercfg_path=None, access=None):
        self.registry = registry
        self._resolved = None
        self.insecure = insecure

        username = None
        password = None
        auth_b64 = None
        if dockercfg_path:
            dockercfg = Dockercfg(dockercfg_path).get_credentials(registry)

            username = dockercfg.get('username')
            password = dockercfg.get('password')
            auth_b64 = dockercfg.get('auth')
        self.auth = HTTPRegistryAuth(username, password, access=access, auth_b64=auth_b64)

        self._fallback = None
        if re.match('http(s)?://', self.registry):
            self._base = self.registry
        else:
            self._base = 'https://{}'.format(self.registry)
            if insecure:
                # In the insecure case, if the registry is just a hostname:port, we
                # don't know whether to talk HTTPS or HTTP to it, so we try first
                # with https then fallback
                self._fallback = 'http://{}'.format(self.registry)

        self.session = get_retrying_requests_session()

    def _do(self, f, relative_url, *args, **kwargs):
        kwargs['auth'] = self.auth
        kwargs['verify'] = not self.insecure
        if self._fallback:
            try:
                res = f(self._base + relative_url, *args, **kwargs)
                self._fallback = None  # don't fallback after one success
                return res
            except (SSLError, requests.ConnectionError):
                self._base = self._fallback
                self._fallback = None
        return f(self._base + relative_url, *args, **kwargs)

    def get(self, relative_url, data=None, **kwargs):
        return self._do(self.session.get, relative_url, **kwargs)

    def head(self, relative_url, data=None, **kwargs):
        return self._do(self.session.head, relative_url, **kwargs)

    def post(self, relative_url, data=None, **kwargs):
        return self._do(self.session.post, relative_url, data=data, **kwargs)

    def put(self, relative_url, data=None, **kwargs):
        return self._do(self.session.put, relative_url, data=data, **kwargs)

    def delete(self, relative_url, **kwargs):
        return self._do(self.session.delete, relative_url, **kwargs)


class RegistryClient(object):
    """
    Registry client, provides methods for looking up image digests and configs
    in container registries.

    Methods that accept the `image` parameter expect that the registry of the
    image matches the one of the client instance, but they do not check whether
    that is true.
    """

    def __init__(self, registry_session):
        self._session = registry_session

    def get_manifest(self, image, version):
        saved_not_found = None
        media_type = get_manifest_media_type(version)
        try:
            response = query_registry(self._session, image, digest=None, version=version)
        except (HTTPError, RetryError) as ex:
            if ex.response is None:
                raise
            if ex.response.status_code == requests.codes.not_found:
                saved_not_found = ex
            # If the registry has a v2 manifest that can't be converted into a v1
            # manifest, the registry fails with status=400 (BAD_REQUEST), and an error code of
            # MANIFEST_INVALID. Note that if the registry has v2 manifest and
            # you ask for an OCI manifest, the registry will try to convert the
            # v2 manifest into a v1 manifest as the default type, so the same
            # thing occurs.
            if version != 'v2' and ex.response.status_code == requests.codes.bad_request:
                logger.warning('Unable to fetch digest for %s, got error %s',
                               media_type, ex.response.status_code)
                return None, saved_not_found
            # Returned if the manifest could not be retrieved for the given
            # media type
            elif (ex.response.status_code == requests.codes.not_found or
                  ex.response.status_code == requests.codes.not_acceptable):
                logger.debug("skipping version %s due to status code %s",
                             version, ex.response.status_code)
                return None, saved_not_found
            else:
                raise

        if not manifest_is_media_type(response, media_type):
            logger.warning("content does not match expected media type")
            return None, saved_not_found
        logger.debug("content matches expected media type")
        return response, saved_not_found

    def get_manifest_digests(self,
                             image,
                             versions=('v1', 'v2', 'v2_list', 'oci', 'oci_index'),
                             require_digest=True):
        """Return manifest digest for image.

        :param image: ImageName, the remote image to inspect
        :param versions: tuple, which manifest schema versions to fetch digest
        :param require_digest: bool, when True exception is thrown if no digest is
                                     set in the headers.

        :return: dict, versions mapped to their digest
        """

        digests = {}
        # If all of the media types return a 404 NOT_FOUND status, then we rethrow
        # an exception, if all of the media types fail for some other reason - like
        # bad headers - then we return a ManifestDigest object with no digests.
        # This is interesting for the Pulp "retry until the manifest shows up" case.
        all_not_found = True
        saved_not_found = None
        for version in versions:
            media_type = get_manifest_media_type(version)
            response, saved_not_found = get_manifest(image, self._session, version)

            if saved_not_found is None:
                all_not_found = False

            if not response:
                continue
            # set it to truthy value so that koji_import would know pulp supports these digests
            digests[version] = True

            if not response.headers.get('Docker-Content-Digest'):
                logger.warning('Unable to fetch digest for %s, no Docker-Content-Digest header',
                               media_type)
                continue

            digests[version] = response.headers['Docker-Content-Digest']
            context = '/'.join([x for x in [image.namespace, image.repo] if x])
            tag = image.tag
            logger.debug('Image %s:%s has %s manifest digest: %s',
                         context, tag, version, digests[version])

        if not digests:
            if all_not_found and len(versions) > 0:
                raise saved_not_found   # pylint: disable=raising-bad-type
            if require_digest:
                raise RuntimeError('No digests found for {}'.format(image))

        return ManifestDigest(**digests)

    def get_manifest_list(self, image):
        """Return manifest list for image.

        :param image: ImageName, the remote image to inspect

        :return: response, or None, with manifest list
        """
        version = 'v2_list'
        response, _ = get_manifest(image, self._session, version)
        return response

    def get_all_manifests(self, image, versions=('v1', 'v2', 'v2_list')):
        """Return manifest digests for image.

        :param image: ImageName, the remote image to inspect
        :param versions: tuple, for which manifest schema versions to fetch manifests

        :return: dict of successful responses, with versions as keys
        """
        digests = {}
        for version in versions:
            response, _ = get_manifest(image, self._session, version)
            if response:
                digests[version] = response

        return digests

    def get_inspect_for_image(self, image):
        """Return inspect for image.

        :param image: ImageName, the remote image to inspect

        :return: dict of inspected image
        """
        all_man_digests = self.get_all_manifests(image)
        blob_config = None
        config_digest = None
        image_inspect = {}

        # we have manifest list (get digest for 1st platform)
        if 'v2_list' in all_man_digests:
            man_list_json = all_man_digests['v2_list'].json()
            if man_list_json['manifests'][0]['mediaType'] != MEDIA_TYPE_DOCKER_V2_SCHEMA2:
                raise RuntimeError('Image {image_name}: v2 schema 1 '
                                   'in manifest list'.format(image_name=image))

            v2_digest = man_list_json['manifests'][0]['digest']
            blob_config, config_digest = self.get_config_and_id_from_registry(image,
                                                                              v2_digest,
                                                                              version='v2')
        # get config for v2 digest
        elif 'v2' in all_man_digests:
            blob_config, config_digest = self.get_config_and_id_from_registry(image,
                                                                              image.tag,
                                                                              version='v2')
        # read config from v1
        elif 'v1' in all_man_digests:
            v1_json = all_man_digests['v1'].json()
            if PY2:
                blob_config = json.loads(v1_json['history'][0]['v1Compatibility'].decode('utf-8'))
            else:
                blob_config = json.loads(v1_json['history'][0]['v1Compatibility'])
        else:
            raise RuntimeError("Image {image_name} not found: No v2 schema 1 image, "
                               "or v2 schema 2 image or list, found".format(image_name=image))

        # dictionary to convert config keys to inspect keys
        config_2_inspect = {
            'created': 'Created',
            'os': 'Os',
            'container_config': 'ContainerConfig',
            'architecture': 'Architecture',
            'docker_version': 'DockerVersion',
            'config': 'Config',
        }

        if not blob_config:
            raise RuntimeError("Image {image_name}: Couldn't get inspect data "
                               "from digest config".format(image_name=image))

        # set Id, which isn't in config blob
        # Won't be set for v1,as for that image has to be pulled
        image_inspect['Id'] = config_digest
        # only v2 has rootfs, not v1
        if 'rootfs' in blob_config:
            image_inspect['RootFS'] = blob_config['rootfs']

        for old_key, new_key in config_2_inspect.items():
            image_inspect[new_key] = blob_config[old_key]

        return image_inspect

    def get_config_and_id_from_registry(self, image, digest, version='v2'):
        """Return image config by digest

        :param image: ImageName, the remote image to inspect
        :param digest: str, digest of the image manifest
        :param version: str, which manifest schema versions to fetch digest

        :return: dict, versions mapped to their digest
        """
        response = query_registry(
            self._session, image, digest=digest, version=version)
        response.raise_for_status()
        manifest_config = response.json()
        config_digest = manifest_config['config']['digest']

        config_response = query_registry(
            self._session, image, digest=config_digest, version=version, is_blob=True)
        config_response.raise_for_status()

        blob_config = config_response.json()

        context = '/'.join([x for x in [image.namespace, image.repo] if x])
        tag = image.tag
        logger.debug('Image %s:%s has config:\n%s', context, tag, blob_config)

        return blob_config, config_digest

    def get_config_from_registry(self, image, digest, version='v2'):
        """Return image config by digest

        :param image: ImageName, the remote image to inspect
        :param digest: str, digest of the image manifest
        :param version: str, which manifest schema versions to fetch digest

        :return: dict, versions mapped to their digest
        """
        blob_config, _ = self.get_config_and_id_from_registry(image, digest, version=version)
        return blob_config


class ManifestDigest(dict):
    """Wrapper for digests for a docker manifest."""

    content_type = {
        'v1': MEDIA_TYPE_DOCKER_V2_SCHEMA1,
        'v2': MEDIA_TYPE_DOCKER_V2_SCHEMA2,
        'v2_list': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
        'oci': MEDIA_TYPE_OCI_V1,
        'oci_index': MEDIA_TYPE_OCI_V1_INDEX,
    }

    @property
    def default(self):
        """Return the default manifest schema version.

        Depending on the docker version, <= 1.9, used to push
        the image to the registry, v2 schema may not be available.
        In such case, the v1 schema should be used when interacting
        with the registry. An OCI digest will only be present when
        the manifest was pushed as an OCI digest.
        """
        return self.v2_list or self.oci_index or self.oci or self.v2 or self.v1

    def __getattr__(self, attr):
        if attr not in self.content_type:
            raise AttributeError("Unknown version: %s" % attr)
        else:
            return self.get(attr, None)


def is_manifest_list(version):
    return version == MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST or version == MEDIA_TYPE_OCI_V1_INDEX


def get_manifest_media_type(version):
    try:
        return ManifestDigest.content_type[version]
    except KeyError:
        raise RuntimeError("Unknown manifest schema type")


def get_manifest_media_version(digest):
    found_version = None
    for version in ManifestDigest.content_type:
        if digest.default and getattr(digest, version) == digest.default:
            found_version = version
            break
    if not found_version:
        raise RuntimeError("Can't detect version for digest %s" % digest)
    return found_version


def get_digests_map_from_annotations(digests_str):
    digests = {}
    digests_annotations = json.loads(digests_str)
    for digest_annotation in digests_annotations:
        digest_version = digest_annotation['version']
        digest = digest_annotation['digest']
        media_type = get_manifest_media_type(digest_version)
        digests[media_type] = digest
    return digests


def query_registry(registry_session, image, digest=None, version='v1', is_blob=False):
    """Return manifest digest for image.

    :param registry_session: RegistrySession
    :param image: ImageName, the remote image to inspect
    :param digest: str, digest of the image manifest
    :param version: str, which manifest schema version to fetch digest
    :param is_blob: bool, read blob config if set to True

    :return: requests.Response object
    """

    context = '/'.join([x for x in [image.namespace, image.repo] if x])
    reference = digest or image.tag
    object_type = 'manifests'
    if is_blob:
        object_type = 'blobs'

    headers = {'Accept': (get_manifest_media_type(version))}
    url = '/v2/{}/{}/{}'.format(context, object_type, reference)
    logger.debug("query_registry: querying %s, headers: %s", url, headers)

    response = registry_session.get(url, headers=headers)
    for r in chain(response.history, [response]):
        logger.debug("query_registry: [%s] %s", r.status_code, r.url)

    logger.debug("query_registry: response headers: %s", response.headers)
    response.raise_for_status()

    return response


def guess_manifest_media_type(content):
    """
    Guess the media type for the given manifest content

    :param content: JSON content of manifest (bytes)
    :return: media type (str), or None if unable to guess
    """
    encoding = guess_json_utf(content)
    try:
        manifest = json.loads(content.decode(encoding))
    except (ValueError,           # Not valid JSON
            TypeError,            # Not an object
            UnicodeDecodeError):  # Unable to decode the bytes
        logger.exception("Unable to decode JSON")
        logger.debug("response content (%s): %r", encoding, content)
        return None

    try:
        return manifest['mediaType']
    except KeyError:
        # no mediaType key
        if manifest.get('schemaVersion') == 1:
            return get_manifest_media_type('v1')

        logger.warning("no mediaType or schemaVersion=1 in manifest, keys: %s",
                       manifest.keys())


def manifest_is_media_type(response, media_type):
    """
    Attempt to confirm the returned manifest is of a given media type

    :param response: a requests.Response
    :param media_type: media_type (str), or None to confirm
        the media type cannot be guessed
    """
    try:
        received_media_type = response.headers['Content-Type']
    except KeyError:
        # Guess media type from content
        logger.debug("No Content-Type header; inspecting content")
        received_media_type = guess_manifest_media_type(response.content)
        logger.debug("guessed media type: %s", received_media_type)

    if received_media_type is None:
        return media_type is None

    # Only compare prefix as response may use +prettyjws suffix
    # which is the case for signed manifest
    response_h_prefix = received_media_type.rsplit('+', 1)[0]
    request_h_prefix = media_type.rsplit('+', 1)[0]
    return response_h_prefix == request_h_prefix


def get_manifest(image, registry_session, version):
    registry_client = RegistryClient(registry_session)
    return registry_client.get_manifest(image, version)


def get_manifest_digests(image, registry, insecure=False, dockercfg_path=None,
                         versions=('v1', 'v2', 'v2_list', 'oci', 'oci_index'), require_digest=True):
    """Return manifest digest for image.

    :param image: ImageName, the remote image to inspect
    :param registry: str, URI for registry, if URI schema is not provided,
                          https:// will be used
    :param insecure: bool, when True registry's cert is not verified
    :param dockercfg_path: str, dirname of .dockercfg location
    :param versions: tuple, which manifest schema versions to fetch digest
    :param require_digest: bool, when True exception is thrown if no digest is
                                 set in the headers.

    :return: dict, versions mapped to their digest
    """
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)
    registry_client = RegistryClient(registry_session)
    return registry_client.get_manifest_digests(image,
                                                versions=versions,
                                                require_digest=require_digest)


def get_manifest_list(image, registry, insecure=False, dockercfg_path=None):
    """Return manifest list for image.

    :param image: ImageName, the remote image to inspect
    :param registry: str, URI for registry, if URI schema is not provided,
                          https:// will be used
    :param insecure: bool, when True registry's cert is not verified
    :param dockercfg_path: str, dirname of .dockercfg location

    :return: response, or None, with manifest list
    """
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)
    registry_client = RegistryClient(registry_session)
    return registry_client.get_manifest_list(image)


def get_all_manifests(image, registry, insecure=False, dockercfg_path=None,
                      versions=('v1', 'v2', 'v2_list')):
    """Return manifest digests for image.

    :param image: ImageName, the remote image to inspect
    :param registry: str, URI for registry, if URI schema is not provided,
                          https:// will be used
    :param insecure: bool, when True registry's cert is not verified
    :param dockercfg_path: str, dirname of .dockercfg location
    :param versions: tuple, for which manifest schema versions to fetch manifests

    :return: dict of successful responses, with versions as keys
    """
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)
    registry_client = RegistryClient(registry_session)
    return registry_client.get_all_manifests(image, versions=versions)


def get_inspect_for_image(image, registry, insecure=False, dockercfg_path=None):
    """Return inspect for image.

    :param image: ImageName, the remote image to inspect
    :param registry: str, URI for registry, if URI schema is not provided,
                          https:// will be used
    :param insecure: bool, when True registry's cert is not verified
    :param dockercfg_path: str, dirname of .dockercfg location

    :return: dict of inspected image
    """
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)
    registry_client = RegistryClient(registry_session)
    return registry_client.get_inspect_for_image(image)


def get_config_and_id_from_registry(image, registry, digest, insecure=False,
                                    dockercfg_path=None, version='v2'):
    """Return image config by digest

    :param image: ImageName, the remote image to inspect
    :param registry: str, URI for registry, if URI schema is not provided,
                          https:// will be used
    :param digest: str, digest of the image manifest
    :param insecure: bool, when True registry's cert is not verified
    :param dockercfg_path: str, dirname of .dockercfg location
    :param version: str, which manifest schema versions to fetch digest

    :return: dict, versions mapped to their digest
    """
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)
    registry_client = RegistryClient(registry_session)
    return registry_client.get_config_and_id_from_registry(image, digest, version=version)


def get_config_from_registry(image, registry, digest, insecure=False,
                             dockercfg_path=None, version='v2'):
    """Return image config by digest

    :param image: ImageName, the remote image to inspect
    :param registry: str, URI for registry, if URI schema is not provided,
                          https:// will be used
    :param digest: str, digest of the image manifest
    :param insecure: bool, when True registry's cert is not verified
    :param dockercfg_path: str, dirname of .dockercfg location
    :param version: str, which manifest schema versions to fetch digest

    :return: dict, versions mapped to their digest
    """
    blob_config, _ = get_config_and_id_from_registry(image, registry, digest, insecure=insecure,
                                                     dockercfg_path=dockercfg_path,
                                                     version=version)
    return blob_config


def df_parser(df_path, workflow=None, cache_content=False, env_replace=True, parent_env=None):
    """
    Wrapper for dockerfile_parse's DockerfileParser that takes into account
    parent_env inheritance.

    :param df_path: string, path to Dockerfile (normally in DockerBuildWorkflow instance)
    :param workflow: DockerBuildWorkflow object instance, used to find parent image information
    :param cache_content: bool, tells DockerfileParser to cache Dockerfile content
    :param env_replace: bool, replace ENV declarations as part of DockerfileParser evaluation
    :param parent_env: dict, parent ENV key:value pairs to be inherited

    :return: DockerfileParser object instance
    """

    p_env = {}

    if parent_env:
        # If parent_env passed in, just use that
        p_env = parent_env

    elif workflow:

        # If parent_env is not provided, but workflow is then attempt to inspect
        # the workflow for the parent_env

        try:
            parent_config = workflow.builder.base_image_inspect[INSPECT_CONFIG]
        except (AttributeError, TypeError, KeyError):
            logger.debug("base image unable to be inspected")
        else:
            try:
                tmp_env = parent_config["Env"]
                logger.debug("Parent Config ENV: %s", tmp_env)

                if isinstance(tmp_env, dict):
                    p_env = tmp_env
                elif isinstance(tmp_env, list):
                    try:
                        for key_val in tmp_env:
                            key, val = key_val.split("=", 1)
                            p_env[key] = val

                    except ValueError:
                        logger.debug("Unable to parse all of Parent Config ENV")

            except KeyError:
                logger.debug("Parent Environment not found, not applied to Dockerfile")

    try:
        dfparser = DockerfileParser(
            df_path,
            cache_content=cache_content,
            env_replace=env_replace,
            parent_env=p_env
        )
    except TypeError:
        logger.debug("Old version of dockerfile-parse detected, unable to set inherited parent "
                     "ENVs")
        dfparser = DockerfileParser(
            df_path,
            cache_content=cache_content,
            env_replace=env_replace,
        )

    return dfparser


def are_plugins_in_order(plugins_conf, *plugins_names):
    """Check if plugins are configured in given order."""
    all_plugins_names = [plugin['name'] for plugin in plugins_conf or []]
    start_index = 0
    for plugin_name in plugins_names:
        try:
            start_index = all_plugins_names.index(plugin_name, start_index)
        except ValueError:
            return False
    return True


def read_yaml_from_file_path(file_path, schema, package='atomic_reactor'):
    """
    :param yaml_data: string, path to the yaml data
    :param schema: string, path to the JSON schema file
    :param package: string, package name containing the JSON schema file
    """
    with open(file_path) as f:
        yaml_data = f.read()
    return osbs_read_yaml(yaml_data, schema, package)


def read_yaml_from_url(url, schema, package='atomic_reactor'):
    """
    :param url: string, URL of the yaml data
    :param schema: string, path to the JSON schema file
    :param package: string, package name containing the JSON schema file
    """
    session = get_retrying_requests_session()
    resp = session.get(url, stream=True)
    resp.raise_for_status()

    f = io.StringIO()
    for chunk in resp.iter_content(chunk_size=DEFAULT_DOWNLOAD_BLOCK_SIZE):
        f.write(chunk.decode('utf-8'))

    f.seek(0)
    return osbs_read_yaml(f.read(), schema, package)


def read_yaml(yaml_data, schema, package='atomic_reactor'):
    """
    :param yaml_data: string, yaml content
    :param schema: string, path to the JSON schema file
    :param package: string, package name containing the JSON schema file
    """
    return osbs_read_yaml(yaml_data, schema, package)


def allow_repo_dir_in_dockerignore(build_path):
    docker_ignore = os.path.join(str(build_path), DOCKERIGNORE)

    if os.path.isfile(docker_ignore):
        with open(docker_ignore, "a") as f:
            f.write("\n!%s\n" % RELATIVE_REPOS_PATH)
        logger.debug("Allowing %s in %s", RELATIVE_REPOS_PATH, DOCKERIGNORE)


class LabelFormatter(string.Formatter):
    """
    using this because str.format can't handle keys with dots and dashes
    which are included in some of the labels, such as
    'authoritative-source-url', 'com.redhat.component', etc
    """
    def get_field(self, field_name, args, kwargs):
        return (self.get_value(field_name, args, kwargs), field_name)


# Make sure to escape '\' and '"' characters.
try:
    # py3
    _label_env_trans = str.maketrans({'\\': '\\\\',
                                      '"': '\\"'})
except AttributeError:
    # py2
    _label_env_trans = None


def _label_escape(s):
    if _label_env_trans:
        return s.translate(_label_env_trans)
    return s.replace('\\', '\\\\').replace('"', '\\"')


def label_to_string(key, value):
    """Return a string "<key>"="<value>" with proper escaping to appear in
    a label statement. Multiple values results can be combined and used as
    LABEL "key"="value" "key2"="value2"
    """
    return '"%s"="%s"' % (_label_escape(key), _label_escape(value))


class SessionWithTimeout(requests.Session):
    """
    requests Session with added timeout
    """
    def __init__(self, *args, **kwargs):
        super(SessionWithTimeout, self).__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', HTTP_REQUEST_TIMEOUT)
        return super(SessionWithTimeout, self).request(*args, **kwargs)


# This is a hook to mock during tests to temporarily disable retries
def _http_retries_disabled():
    return False


def get_retrying_requests_session(client_statuses=HTTP_CLIENT_STATUS_RETRY,
                                  times=HTTP_MAX_RETRIES, delay=HTTP_BACKOFF_FACTOR,
                                  method_whitelist=None, raise_on_status=True):
    if _http_retries_disabled():
        times = 0

    retry = Retry(
        total=int(times),
        backoff_factor=delay,
        status_forcelist=client_statuses,
        method_whitelist=method_whitelist
    )

    # raise_on_status was added later to Retry, adding compatibility to work
    # with newer versions and ignoring this option with older ones
    if hasattr(retry, 'raise_on_status'):
        retry.raise_on_status = raise_on_status

    session = SessionWithTimeout()
    session.mount('http://', HTTPAdapter(max_retries=retry))
    session.mount('https://', HTTPAdapter(max_retries=retry))

    return session


def get_primary_images(workflow):
    primary_images = workflow.tag_conf.primary_images
    if not primary_images:
        primary_images = [
            ImageName.parse(primary) for primary in
            workflow.build_result.annotations['repositories']['primary']]

    return primary_images


def get_floating_images(workflow):
    floating_images = workflow.tag_conf.floating_images
    if not floating_images:
        floating_results = workflow.build_result.annotations['repositories'].get('floating', [])
        if floating_results:
            floating_images = [ImageName.parse(floating) for floating in floating_results]

    return floating_images


def get_unique_images(workflow):
    unique_images = workflow.tag_conf.unique_images
    if not unique_images:
        unique_results = workflow.build_result.annotations['repositories'].get('unique', [])
        if unique_results:
            unique_images = [ImageName.parse(unique) for unique in unique_results]

    return unique_images


def get_parent_image_koji_data(workflow):
    """Transform koji_parent plugin results into metadata dict."""
    koji_parent = workflow.prebuild_results.get(PLUGIN_KOJI_PARENT_KEY) or {}
    image_metadata = {}

    parents = {}
    for img, build in (koji_parent.get(PARENT_IMAGES_KOJI_BUILDS) or {}).items():
        if not build:
            parents[str(img)] = None
        else:
            parents[str(img)] = {key: val for key, val in build.items() if key in ('id', 'nvr')}
    image_metadata[PARENT_IMAGE_BUILDS_KEY] = parents

    # ordered list of parent images
    image_metadata[PARENT_IMAGES_KEY] = workflow.builder.parents_ordered

    # don't add parent image id key for scratch
    if workflow.builder.base_from_scratch:
        return image_metadata

    base_info = koji_parent.get(BASE_IMAGE_KOJI_BUILD) or {}
    parent_id = base_info.get('id')
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except ValueError:
            logger.exception("invalid koji parent id %r", parent_id)
        else:
            image_metadata[BASE_IMAGE_BUILD_ID_KEY] = parent_id
    return image_metadata


class OSBSLogs(object):
    def __init__(self, log):
        self.log = log

    def get_log_metadata(self, path, filename):
        """
        Describe a file by its metadata.

        :return: dict
        """

        checksums = get_checksums(path, ['md5'])
        metadata = {'filename': filename,
                    'filesize': os.path.getsize(path),
                    'checksum': checksums['md5sum'],
                    'checksum_type': 'md5'}

        return metadata

    def get_log_files(self, osbs, build_id):
        """
        Build list of log files

        :return: list, of log files
        """

        logs = None
        output = []

        # Collect logs from server
        try:
            logs = osbs.get_orchestrator_build_logs(build_id)
        except OsbsException as ex:
            self.log.error("unable to get build logs: %s", ex)
            return output
        except TypeError:
            # Older osbs-client has no get_orchestrator_build_logs
            self.log.error("OSBS client does not support get_orchestrator_build_logs")
            return output

        platform_logs = {}
        for entry in logs:
            platform = entry.platform
            if platform not in platform_logs:
                filename = 'orchestrator' if platform is None else platform
                platform_logs[platform] = NamedTemporaryFile(prefix="%s-%s" %
                                                             (build_id, filename),
                                                             suffix=".log", mode='r+b')
            platform_logs[platform].write((entry.line + '\n').encode('utf-8'))

        for platform, logfile in platform_logs.items():
            logfile.flush()
            filename = 'orchestrator' if platform is None else platform
            metadata = self.get_log_metadata(logfile.name, "%s.log" % filename)
            output.append(Output(file=logfile, metadata=metadata))

        return output


# As defined in pyhton docs example for format_map:
#   https://docs.python.org/3/library/stdtypes.html#str.format_map
class DefaultKeyDict(dict):
    def __missing__(self, key):
        return key


def get_platforms_in_limits(workflow, input_platforms=None):
    def make_list(value):
        if not isinstance(value, list):
            value = [value]
        return value

    if not input_platforms:
        return None
    excluded_platforms = set()

    if not isinstance(input_platforms, set):
        expected_platforms = set(input_platforms)
    else:
        expected_platforms = deepcopy(input_platforms)

    data = workflow.source.config.data

    logger.info("%s contains: %s", REPO_CONTAINER_CONFIG, data)
    if data and 'platforms' in data and data['platforms']:
        excluded_platforms = set(make_list(data['platforms'].get('not', [])))
        only_platforms = set(make_list(data['platforms'].get('only', [])))
        if only_platforms:
            if excluded_platforms == only_platforms:
                logger.warning(
                    'only and not platforms are the same in %s',
                    workflow.source.config.file_path
                )
            expected_platforms = expected_platforms & only_platforms
    return expected_platforms - excluded_platforms


def dump_stacktraces(sig, frame):
    dump_traceback()


def setup_introspection_signal_handler():
    signal.signal(signal.SIGUSR1, dump_stacktraces)


def exception_message(exc):
    """
    Take an exception and return an error message.
    The message includes the type of the exception.
    """
    return '{exc.__class__.__name__}: {exc}'.format(exc=exc)


def _has_label_flag(workflow, label):
    dockerfile = df_parser(workflow.builder.df_path, workflow=workflow)
    labels = Labels(dockerfile.labels)
    try:
        _, value = labels.get_name_and_value(label)
    except KeyError:
        value = 'false'
    return value.lower() == 'true'


def has_operator_appregistry_manifest(workflow):
    """
    Check if Dockerfile sets the operator manifest appregistry label

    :return: bool
    """
    return _has_label_flag(workflow, Labels.LABEL_TYPE_OPERATOR_MANIFESTS)


def has_operator_bundle_manifest(workflow):
    """
    Check if Dockerfile sets the operator manifest bundle label

    :return: bool
    """
    return _has_label_flag(workflow, Labels.LABEL_TYPE_OPERATOR_BUNDLE_MANIFESTS)


class BadConfigMapError(Exception):
    """
    Build annotation does not indicate a valid ConfigMap.
    """


def get_platform_config(platform, build_annotations):
    """
    Return tuple platform config map and config map key
    """
    kind = "configmap/"
    cmlen = len(kind)
    cm_key_tmp = build_annotations['metadata_fragment']
    cm_frag_key = build_annotations['metadata_fragment_key']

    if not cm_key_tmp or not cm_frag_key or cm_key_tmp[:cmlen] != kind:
        msg = "Bad ConfigMap annotations for platform {}".format(platform)
        logger.warning(msg)
        raise BadConfigMapError(msg)

    # use the key to get the configmap data and then use the
    # fragment_key to get the build metadata inside the configmap data
    # save the worker_build metadata
    cm_key = cm_key_tmp[cmlen:]

    return cm_key, cm_frag_key


def chain_get(d, path, default=None):
    """
    Traverse nested dicts/lists (typically in data loaded from yaml/json)
    according to keys/indices in `path`, return found value.

    If any of the lookups would fail, return `default`.

    :param d: Data to chain-get a value from (a dict)
    :param path: List of keys/indices specifying a path in the data
    :param default: Value to return if any key/index lookup fails along the way
    :return: Value found in data or `default`
    """
    obj = d
    for key_or_index in path:
        try:
            obj = obj[key_or_index]
        except (IndexError, KeyError):
            return default
    return obj
