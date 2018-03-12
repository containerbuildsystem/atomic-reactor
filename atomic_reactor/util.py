"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import hashlib
from itertools import chain
import json
import jsonschema
import os
import re
from pipes import quote
import requests
from requests.exceptions import ConnectionError, SSLError, HTTPError, RetryError, Timeout
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util import Retry
import shutil
import subprocess
import tempfile
import logging
import uuid
import yaml
import codecs
import string
import time
from collections import namedtuple

from six.moves.urllib.parse import urlparse

from atomic_reactor.constants import (DOCKERFILE_FILENAME, REPO_CONTAINER_CONFIG, TOOLS_USED,
                                      INSPECT_CONFIG,
                                      IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR,
                                      HTTP_MAX_RETRIES, HTTP_BACKOFF_FACTOR,
                                      HTTP_CLIENT_STATUS_RETRY, HTTP_REQUEST_TIMEOUT,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1, MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST, MEDIA_TYPE_OCI_V1,
                                      MEDIA_TYPE_OCI_V1_INDEX, GIT_MAX_RETRIES, GIT_BACKOFF_FACTOR)

from dockerfile_parse import DockerfileParser
from pkg_resources import resource_stream

from importlib import import_module
from requests.utils import guess_json_utf

from osbs.exceptions import OsbsException
from tempfile import NamedTemporaryFile
Output = namedtuple('Output', ['file', 'metadata'])

logger = logging.getLogger(__name__)


class ImageName(object):
    def __init__(self, registry=None, namespace=None, repo=None, tag=None):
        self.registry = registry
        self.namespace = namespace
        self.repo = repo
        self.tag = tag

    @classmethod
    def parse(cls, image_name):
        result = cls()

        # registry.org/namespace/repo:tag
        s = image_name.split('/', 2)

        if len(s) == 2:
            if '.' in s[0] or ':' in s[0]:
                result.registry = s[0]
            else:
                result.namespace = s[0]
        elif len(s) == 3:
            result.registry = s[0]
            result.namespace = s[1]
        result.repo = s[-1]

        for sep in '@:':
            try:
                result.repo, result.tag = result.repo.rsplit(sep, 1)
            except ValueError:
                continue
            break

        return result

    def to_str(self, registry=True, tag=True, explicit_tag=False,
               explicit_namespace=False):
        if self.repo is None:
            raise RuntimeError('No image repository specified')

        result = self.get_repo(explicit_namespace)

        if tag and self.tag and ':' in self.tag:
            result = '{0}@{1}'.format(result, self.tag)
        elif tag and self.tag:
            result = '{0}:{1}'.format(result, self.tag)
        elif tag and explicit_tag:
            result = '{0}:{1}'.format(result, 'latest')

        if registry and self.registry:
            result = '{0}/{1}'.format(self.registry, result)

        return result

    def get_repo(self, explicit_namespace=False):
        result = self.repo
        if self.namespace:
            result = '{0}/{1}'.format(self.namespace, result)
        elif explicit_namespace:
            result = '{0}/{1}'.format('library', result)
        return result

    @property
    def pulp_repo(self):
        return self.to_str(registry=False, tag=False).replace("/", "-")

    def __str__(self):
        return self.to_str(registry=True, tag=True)

    def __repr__(self):
        return "ImageName(image=%r)" % self.to_str()

    def __eq__(self, other):
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.to_str())

    def copy(self):
        return ImageName(
            registry=self.registry,
            namespace=self.namespace,
            repo=self.repo,
            tag=self.tag)


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
            line = item.get("stream", "")
        else:
            line = item
            item = None

        for l in line.splitlines():
            l = l.strip()
            self._logs.append(l)
            if l:
                logger.debug(l)

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


def clone_git_repo(git_url, target_dir, commit=None, retry_times=GIT_MAX_RETRIES):
    """
    clone provided git repo to target_dir, optionally checkout provided commit

    :param git_url: str, git repo to clone
    :param target_dir: str, filesystem path where the repo should be cloned
    :param commit: str, commit to checkout, SHA-1 or ref
    :param retry_times: int, number of retries for git clone
    :return: str, commit ID of HEAD
    """
    retry_delay = GIT_BACKOFF_FACTOR

    commit = commit or "master"
    logger.info("cloning git repo '%s'", git_url)
    logger.debug("url = '%s', dir = '%s', commit = '%s'",
                 git_url, target_dir, commit)

    cmd = ["git", "clone", git_url, quote(target_dir)]

    logger.debug("cloning '%s'", cmd)
    for counter in range(retry_times + 1):
        try:
            # we are using check_output, even though we aren't using
            # the return value, but we will get 'output' in exception
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            break
        except subprocess.CalledProcessError as exc:
            if counter != retry_times:
                logger.info("retrying command '%s':\n '%s'", cmd, exc.output)
                time.sleep(retry_delay * (2 ** counter))
            else:
                raise

    cmd = ["git", "reset", "--hard", commit]
    logger.debug("checking out branch '%s'", cmd)
    subprocess.check_call(cmd, cwd=target_dir)
    cmd = ["git", "rev-parse", "HEAD"]
    logger.debug("getting SHA-1 of provided ref '%s'", cmd)
    commit_id = subprocess.check_output(cmd, cwd=target_dir, universal_newlines=True)
    commit_id = commit_id.strip()
    logger.info("commit ID = %s", commit_id)
    return commit_id


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
    def __init__(self, git_url, commit=None, tmpdir=None):
        self.git_url = git_url
        # provided commit ID/reference to check out
        self.commit = commit
        # commit ID of HEAD; we'll figure this out ourselves
        self._commit_id = None
        self.provided_tmpdir = tmpdir
        self._git_path = None

    @property
    def _tmpdir(self):
        return self.provided_tmpdir or self.our_tmpdir

    @property
    def commit_id(self):
        return self._commit_id

    @property
    def git_path(self):
        if self._git_path is None:
            self._commit_id = clone_git_repo(self.git_url, self._tmpdir, self.commit)
            self._git_path = self._tmpdir
        return self._git_path

    def __enter__(self):
        if not self.provided_tmpdir:
            self.our_tmpdir = tempfile.mkdtemp()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.provided_tmpdir:
            if self.our_tmpdir:
                shutil.rmtree(self.our_tmpdir)


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
        raise RuntimeError("plugin '%s' was specified multiple (%d) times, can't pick one",
                           plugin_name, plugins_num)


def get_checksums(path, algorithms):
    """
    Compute a checksum(s) of given file using specified algorithms.

    :param path: path to file
    :param algorithms: list of cryptographic hash functions, currently supported: md5, sha256
    :return: dictionary
    """
    if not algorithms:
        return {}

    compute_md5 = 'md5' in algorithms
    compute_sha256 = 'sha256' in algorithms

    if compute_md5:
        md5 = hashlib.md5()
    if compute_sha256:
        sha256 = hashlib.sha256()
    blocksize = 65536
    with open(path, mode='rb') as f:
        buf = f.read(blocksize)
        while len(buf) > 0:
            if compute_md5:
                md5.update(buf)
            if compute_sha256:
                sha256.update(buf)
            buf = f.read(blocksize)

    checksums = {}
    if compute_md5:
        checksums['md5sum'] = md5.hexdigest()
        logger.debug('md5sum: %s', checksums['md5sum'])
    if compute_sha256:
        checksums['sha256sum'] = sha256.hexdigest()
        logger.debug('sha256sum: %s', checksums['sha256sum'])
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
            logger.warning("can't import module %s: %r", pkg_name, ex)
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


def is_scratch_build():
    build_json = get_build_json()
    try:
        return build_json['metadata']['labels'].get('scratch', False)
    except KeyError:
        logger.error('metadata.labels not found in build json')
        raise


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
        Create a new Dockercfg object from a .dockercfg file whose
        containing directory is secret_path.

        :param secret_path: str, dirname of .dockercfg location
        """

        self.json_secret_path = os.path.join(secret_path, '.dockercfg')
        try:
            with open(self.json_secret_path) as fp:
                self.json_secret = json.load(fp)
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

            logger.warn('%s not found in .dockercfg', docker_registry)
            return {}


class RegistrySession(object):
    def __init__(self, registry, insecure=False, dockercfg_path=None):
        self.registry = registry
        self._resolved = None
        self.insecure = insecure

        self.auth = None
        if dockercfg_path:
            dockercfg = Dockercfg(dockercfg_path).get_credentials(registry)

            username = dockercfg.get('username')
            password = dockercfg.get('password')
            if username and password:
                self.auth = requests.auth.HTTPBasicAuth(username, password)

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
            except (SSLError, ConnectionError):
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
            raise AttributeError("Unknown version: %s", attr)
        else:
            return self.get(attr, None)


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
    reference = digest or image.tag or 'latest'
    object_type = 'manifests'
    if is_blob:
        object_type = 'blobs'

    headers = {'Accept': (get_manifest_media_type(version))}
    url = '/v2/{}/{}/{}'.format(context, object_type, reference)
    logger.debug("query_registry: querying {}, headers: {}".format(url, headers))

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

    digests = {}
    # If all of the media types return a 404 NOT_FOUND status, then we rethrow
    # an exception, if all of the media types fail for some other reason - like
    # bad headers - then we return a ManifestDigest object with no digests.
    # This is interesting for the Pulp "retry until the manifest shows up" case.
    all_not_found = True
    saved_not_found = None
    for version in versions:
        media_type = get_manifest_media_type(version)

        try:
            response = query_registry(registry_session, image, digest=None, version=version)
            all_not_found = False
        except (HTTPError, RetryError, Timeout) as ex:
            if ex.response.status_code == requests.codes.not_found:
                saved_not_found = ex
            else:
                all_not_found = False

            # If the registry has a v2 manifest that can't be converted into a v1
            # manifest, the registry fails with status=400 (BAD_REQUEST), and an error code of
            # MANIFEST_INVALID. Note that if the registry has v2 manifest and
            # you ask for an OCI manifest, the registry will try to convert the
            # v2 manifest into a v1 manifest as the default type, so the same
            # thing occurs.
            if version != 'v2' and ex.response.status_code == requests.codes.bad_request:
                logger.warning('Unable to fetch digest for %s, got error %s',
                               media_type, ex.response.status_code)
                continue
            # Returned if the manifest could not be retrieved for the given
            # media type
            elif (ex.response.status_code == requests.codes.not_found or
                  ex.response.status_code == requests.codes.not_acceptable):
                logger.debug("skipping version %s due to status code %s",
                             version, ex.response.status_code)
                continue
            else:
                raise

        if not manifest_is_media_type(response, media_type):
            logger.error("content does not match expected media type")
            continue
        logger.debug("content matches expected media type")

        # set it to truthy value so that koji_import would know pulp supports these digests
        digests[version] = True

        if not response.headers.get('Docker-Content-Digest'):
            logger.warning('Unable to fetch digest for %s, no Docker-Content-Digest header',
                           media_type)
            continue

        digests[version] = response.headers['Docker-Content-Digest']
        context = '/'.join([x for x in [image.namespace, image.repo] if x])
        tag = image.tag or 'latest'
        logger.debug('Image %s:%s has %s manifest digest: %s',
                     context, tag, version, digests[version])

    if not digests:
        if all_not_found and len(versions) > 0:
            raise saved_not_found
        if require_digest:
            raise RuntimeError('No digests found for {}'.format(image))

    return ManifestDigest(**digests)


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
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)

    response = query_registry(
        registry_session, image, digest=digest, version=version)
    response.raise_for_status()
    manifest_config = response.json()
    config_digest = manifest_config['config']['digest']

    config_response = query_registry(
        registry_session, image, digest=config_digest, version=version, is_blob=True)
    config_response.raise_for_status()

    blob_config = config_response.json()

    context = '/'.join([x for x in [image.namespace, image.repo] if x])
    tag = image.tag or 'latest'
    logger.debug('Image %s:%s has config:\n%s', context, tag, blob_config)

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
            parent_config = workflow.base_image_inspect[INSPECT_CONFIG]
        except (AttributeError, TypeError, KeyError):
            logger.debug("base image unable to be inspected")
        else:
            try:
                tmp_env = parent_config["Env"]
                logger.debug("Parent Config ENV: %s" % tmp_env)

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


def read_yaml_from_file_path(file_path, schema):
    with open(file_path) as f:
        yaml_data = f.read()
    return read_yaml(yaml_data, schema)


def read_yaml(yaml_data, schema):
    """
    :param yaml_data: string, yaml content
    """
    try:
        resource = resource_stream('atomic_reactor', schema)
        schema = codecs.getreader('utf-8')(resource)
    except (IOError, TypeError):
        logger.error('unable to extract JSON schema, cannot validate')
        raise

    try:
        schema = json.load(schema)
    except ValueError:
        logger.error('unable to decode JSON schema, cannot validate')
        raise
    data = yaml.safe_load(yaml_data)
    validator = jsonschema.Draft4Validator(schema=schema)
    try:
        jsonschema.Draft4Validator.check_schema(schema)
        validator.validate(data)
    except jsonschema.SchemaError:
        logger.error('invalid schema, cannot validate')
        raise
    except jsonschema.ValidationError:
        for error in validator.iter_errors(data):
            path = ''
            for element in error.absolute_path:
                if isinstance(element, int):
                    path += '[{}]'.format(element)
                else:
                    path += '.{}'.format(element)

            if path.startswith('.'):
                path = path[1:]

            logger.error('validation error (%s): %s', path or 'at top level', error.message)

        raise

    return data


class LabelFormatter(string.Formatter):
    """
    using this because str.format can't handle keys with dots and dashes
    which are included in some of the labels, such as
    'authoritative-source-url', 'com.redhat.component', etc
    """
    def get_field(self, field_name, args, kwargs):
        return (self.get_value(field_name, args, kwargs), field_name)


class SessionWithTimeout(requests.Session):
    """
    requests Session with added timeout
    """
    def __init__(self, *args, **kwargs):
        super(SessionWithTimeout, self).__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', HTTP_REQUEST_TIMEOUT)
        return super(SessionWithTimeout, self).request(*args, **kwargs)


def get_retrying_requests_session(client_statuses=HTTP_CLIENT_STATUS_RETRY,
                                  times=HTTP_MAX_RETRIES, delay=HTTP_BACKOFF_FACTOR,
                                  method_whitelist=None):
    retry = Retry(
        total=int(times),
        backoff_factor=delay,
        status_forcelist=client_statuses,
        method_whitelist=method_whitelist
    )
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


class ModuleSpec(object):
    def __init__(self, name, stream, version=None, profile=None):
        self.name = name
        self.stream = stream
        self.version = version
        self.profile = profile

    def to_str(self, include_profile=True):
        result = self.name + ':' + self.stream
        if self.version:
            result += ':' + self.version
        if include_profile and self.profile:
            result += '/' + self.profile

        return result

    def __repr__(self):
        return "ModuleSpec({})".format(self.to_str())

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


def split_module_spec(module):
    # Current module naming guidelines are at:
    # https://docs.pagure.org/modularity/development/building-modules/naming-policy.html
    # We simplify the possible NAME:STREAM:CONTEXT:ARCH/PROFILE and only care about
    # NAME:STREAM or NAME:STREAM:VERSION with optional PROFILE. ARCH is determined by
    # the architecture. CONTEXT may become important in the future, but we ignore it
    # for now.
    #
    # Previously the separator was '-' instead of ':', which required hardcoding the
    # format of VERSION to distinguish between HYPHENATED-NAME-STREAM and NAME-STREAM-VERSION.
    # We support the old format for compatibility.
    #
    PATTERNS = [
        (r'^([^:/]+):([^:/]+):([^:/]+)(?:/([^:/]+))?$', 3, 4),
        (r'^([^:/]+):([^:/]+)(?:/([^:/]+))?$', None, 3),
        (r'^(.+)-([^-]+)-(\d{14})$', 3, None),
        (r'^(.+)-([^-]+)$', None, None)
    ]

    for pat, version_index, profile_index in PATTERNS:
        m = re.match(pat, module)
        if m:
            name = m.group(1)
            stream = m.group(2)
            version = None
            if version_index is not None:
                version = m.group(version_index)
            else:
                version = None
            if profile_index is not None:
                profile = m.group(profile_index)
            else:
                profile = None

            return ModuleSpec(name, stream, version, profile)

    raise RuntimeError(
        'Module specification should be NAME:STREAM[/PROFILE] or NAME:STREAM:VERSION[/PROFILE]. ' +
        '(NAME-STREAM and NAME-STREAM-VERSION supported for compatibility.)'
    )


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
            self.log.error("unable to get build logs: %r", ex)
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
