"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import hashlib
import json
import os
from pipes import quote
import shutil
import subprocess
import tempfile
import logging
import uuid

from atomic_reactor.constants import DOCKERFILE_FILENAME, TOOLS_USED

try:
    from importlib import import_module
except ImportError:
    import_module = __import__  # I love python 2.6


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

        try:
            result.repo, result.tag = result.repo.rsplit(':', 1)
        except ValueError:
            pass

        return result

    def to_str(self, registry=True, tag=True, explicit_tag=False,
               explicit_namespace=False):
        if self.repo is None:
            raise RuntimeError('No image repository specified')

        result = self.repo

        if tag and self.tag:
            result = '{0}:{1}'.format(result, self.tag)
        elif tag and explicit_tag:
            result = '{0}:{1}'.format(result, 'latest')

        if self.namespace:
            result = '{0}/{1}'.format(self.namespace, result)
        elif explicit_namespace:
            result = '{0}/{1}'.format('library', result)

        if registry and self.registry:
            result = '{0}/{1}'.format(self.registry, result)

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

def figure_out_dockerfile(absolute_path, local_path=None):
    """
    try to figure out dockerfile from provided path and optionally from relative local path
    this is meant to be used with git repo: absolute_path is path to git repo,
    local_path is path to dockerfile within git repo

    :param absolute_path:
    :param local_path:
    :return: tuple, (dockerfile_path, dir_with_dockerfile_path)
    """
    logger.info("searching for dockerfile in '%s' (local path %s)", absolute_path, local_path)
    logger.debug("abs path = '%s', local path = '%s'", absolute_path, local_path)
    if local_path:
        if local_path.endswith(DOCKERFILE_FILENAME):
            git_df_dir = os.path.dirname(local_path)
            df_dir = os.path.abspath(os.path.join(absolute_path, git_df_dir))
        else:
            df_dir = os.path.abspath(os.path.join(absolute_path, local_path))
    else:
        df_dir = os.path.abspath(absolute_path)
    if not os.path.isdir(df_dir):
        raise IOError("Directory '%s' doesn't exist." % df_dir)
    df_path = os.path.join(df_dir, DOCKERFILE_FILENAME)
    if not os.path.isfile(df_path):
        raise IOError("Dockerfile '%s' doesn't exist." % df_path)
    logger.debug("Dockerfile found: '%s'", df_path)
    return df_path, df_dir


class CommandResult(object):
    def __init__(self):
        self._logs = []
        self._parsed_logs = []
        self._error = None
        self._error_detail = None

    def parse_item(self, item):
        """
        :param item: str, json-encoded string
        """
        item = item.decode("utf-8")
        try:
            parsed_item = json.loads(item)
        except ValueError:
            parsed_item = None
        else:
            # append here just in case .get bellow fails
            self._parsed_logs.append(parsed_item)

        # make sure the json is a dictionary object
        if isinstance(parsed_item, dict):
            line = parsed_item.get("stream", "")
        else:
            parsed_item = None
            line = item

        for l in line.splitlines():
            l = l.strip()
            self._logs.append(l)
            if l:
                logger.debug(l)

        if parsed_item is not None:
            self._error = parsed_item.get("error", None)
            self._error_detail = parsed_item.get("errorDetail", None)
            if self._error:
                logger.error(item.strip())

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
    using given generator, wait for it to raise StopIteration, which
    indicates that docker has finished with processing

    :return: list of str, logs
    """
    logger.info("wait_for_command")
    cr = CommandResult()
    while True:
        try:
            item = next(logs_generator)  # py2 & 3 compat
            cr.parse_item(item)
        except StopIteration:
            logger.info("no more logs")
            break
    return cr


def backported_check_output(*popenargs, **kwargs):
    """
    Run command with arguments and return its output as a byte string.

    Backported from Python 2.7 as it's implemented as pure python on stdlib.

    https://gist.github.com/edufelipe/1027906
    """
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output, _ = process.communicate()
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        error = subprocess.CalledProcessError(retcode, cmd)
        error.output = output
        raise error
    return output


def clone_git_repo(git_url, target_dir, commit=None):
    """
    clone provided git repo to target_dir, optionally checkout provided commit

    :param git_url: str, git repo to clone
    :param target_dir: str, filesystem path where the repo should be cloned
    :param commit: str, commit to checkout, SHA-1 or ref
    :return: str, commit ID of HEAD
    """
    commit = commit or "master"
    logger.info("cloning git repo '%s'", git_url)
    logger.debug("url = '%s', dir = '%s', commit = '%s'",
                 git_url, target_dir, commit)

    cmd = ["git", "clone", "-b", commit, "--depth", "1", git_url, quote(target_dir)]
    logger.debug("doing a shallow clone '%s'", cmd)
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as ex:
        logger.warning(repr(ex))
        # http://stackoverflow.com/questions/1911109/clone-a-specific-git-branch/4568323#4568323
        # -b takes only refs, not SHA-1
        cmd = ["git", "clone", "-b", commit, "--single-branch", git_url, quote(target_dir)]
        logger.debug("cloning single branch '%s'", cmd)
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as ex:
            logger.warning(repr(ex))
            # let's try again with plain `git clone $url && git checkout`
            cmd = ["git", "clone", git_url, quote(target_dir)]
            logger.debug("cloning '%s'", cmd)
            subprocess.check_call(cmd)
            cmd = ["git", "reset", "--hard", commit]
            logger.debug("checking out branch '%s'", cmd)
            subprocess.check_call(cmd, cwd=target_dir)
    cmd = ["git", "rev-parse", "HEAD"]
    logger.debug("getting SHA-1 of provided ref '%s'", cmd)
    try:
        commit_id = subprocess.check_output(cmd, cwd=target_dir)  # py 2.7
    except AttributeError:
        commit_id = backported_check_output(cmd, cwd=target_dir)  # py 2.6
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


def get_exported_image_metadata(path):
    logger.info('getting metadata for tarball %s', path)
    metadata = {'path': path}
    if not path or not os.path.isfile(path):
        logger.error('%s is not a file', path)
        return

    metadata['size'] = os.path.getsize(path)
    logger.debug('size: %d bytes', metadata['size'])
    metadata.update(get_checksums(path, ['md5', 'sha256']))
    return metadata


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


# each tuple is sorted from most preferred to least
_PREFERRED_LABELS = (
    ('name', 'Name'),
    ('version', 'Version'),
    ('release', 'Release'),
    ('architecture', 'Architecture'),
    ('vendor', 'Vendor'),
    ('authoritative-source', 'Authoritative_Registry'),
    ('com.redhat.component', 'BZComponent'),
    ('com.redhat.build-host', 'Build_Host'),
)


def get_preferred_label_key(labels, name):
    """
    We can have multiple variants of some labels (e.g. Version and version), sorted by preference.
    This function returns the best label corresponding to "name" that is present in the "labels"
    dictionary.

    Returns unchanged name if we don't have it in the preference table. If name is in the table but
    none of the variants are in the labels dict, returns the most-preferred label - the assumption
    is that we're gonna raise an error later and the error message should contain the preferred
    variant.
    """
    for label_chain in _PREFERRED_LABELS:
        if name in label_chain:
            break
    else:
        # no variants known, return the name unchanged
        return name

    for lbl in label_chain:
        if lbl in labels:
            return lbl

    # none of the variants is in 'labels', return the best
    return label_chain[0]


def get_preferred_label(labels, name):
    key = get_preferred_label_key(labels, name)
    return labels.get(key)


def get_build_json():
    try:
        return json.loads(os.environ["BUILD"])
    except KeyError:
        logger.error("No $BUILD env variable. Probably not running in build container")
        raise

# copypasted and slightly modified from
# http://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size/1094933#1094933
def human_size(num, suffix='B'):
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.2f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.2f %s%s" % (num, 'Yi', suffix)
