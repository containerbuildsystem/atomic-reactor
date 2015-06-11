"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import json
import os
from pipes import quote
import re
import shlex
import shutil
import subprocess
import tempfile
import logging
import uuid
from dock.constants import DOCKERFILE_FILENAME, PY2


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
        if result.namespace == 'library':
            # https://github.com/DBuildService/dock/issues/45
            logger.debug("namespace 'library' -> ''")
            result.namespace = None
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
        return self.to_str(registry=False, tag=False, explicit_namespace=True).replace("/", "-")

    def __str__(self):
        return self.to_str(registry=True, tag=True)

    def __repr__(self):
        return "ImageName(image=%s)" % repr(self.to_str())

    def __eq__(self, other):
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def copy(self):
        return ImageName(
            registry=self.registry,
            namespace=self.namespace,
            repo=self.repo,
            tag=self.tag)


class DockerfileParser(object):
    def __init__(self, git_path, path=''):
        if git_path.endswith(DOCKERFILE_FILENAME):
            self.dockerfile_path = git_path
        else:
            if path.endswith(DOCKERFILE_FILENAME):
                self.dockerfile_path = os.path.join(git_path, path)
            else:
                self.dockerfile_path = os.path.join(git_path, path, DOCKERFILE_FILENAME)

    @staticmethod
    def b2u(string):
        """ bytes to unicode """
        if isinstance(string, bytes):
            return string.decode('utf-8')
        return string

    @staticmethod
    def u2b(string):
        """ unicode to bytes (Python 2 only) """
        if PY2 and isinstance(string, unicode):
            return string.encode('utf-8')
        return string

    @property
    def lines(self):
        try:
            with open(self.dockerfile_path, 'r') as dockerfile:
                return [self.b2u(l) for l in dockerfile.readlines()]
        except (IOError, OSError) as ex:
            logger.error("Couldn't retrieve lines from dockerfile: %s" % repr(ex))
            raise

    @lines.setter
    def lines(self, lines):
        try:
            with open(self.dockerfile_path, 'w') as dockerfile:
                dockerfile.writelines([self.u2b(l) for l in lines])
        except (IOError, OSError) as ex:
            logger.error("Couldn't write lines to dockerfile: %s" % repr(ex))
            raise

    @property
    def content(self):
        try:
            with open(self.dockerfile_path, 'r') as dockerfile:
                return self.b2u(dockerfile.read())
        except (IOError, OSError) as ex:
            logger.error("Couldn't retrieve content of dockerfile: %s" % repr(ex))
            raise

    @content.setter
    def content(self, content):
        try:
            with open(self.dockerfile_path, 'w') as dockerfile:
                dockerfile.write(self.u2b(content))
        except (IOError, OSError) as ex:
            logger.error("Couldn't write content to dockerfile: %s" % repr(ex))
            raise

    def get_baseimage(self):
        for line in self.lines:
            if line.startswith("FROM"):
                return line.split()[1]

    def _split(self, string):
        if PY2 and isinstance(string, unicode):
            # Python2's shlex doesn't like unicode
            string = self.u2b(string)
            splits = shlex.split(string)
            return map(self.b2u, splits)
        else:
            return shlex.split(string)

    def get_labels(self):
        """ opposite of AddLabelsPlugin, i.e. return dict of labels from dockerfile
        :return: dictionary of label:value or label:'' if there's no value
        """
        labels = {}
        multiline = False
        processed_instr = ""
        for line in self.lines:
            line = line.rstrip()  # docker does this
            logger.debug("processing line %s", repr(line))
            if multiline:
                processed_instr += line
                if line.endswith("\\"):  # does multiline continue?
                    # docker strips single \
                    processed_instr = processed_instr[:-1]
                    continue
                else:
                    multiline = False
            else:
                processed_instr = line
            if processed_instr.startswith("LABEL"):
                if processed_instr.endswith("\\"):
                    logger.debug("multiline LABEL")
                    # docker strips single \
                    processed_instr = processed_instr[:-1]
                    multiline = True
                    continue
                for token in self._split(processed_instr[len("LABEL "):]):
                    key_val = token.split("=", 1)
                    if len(key_val) == 2:
                        labels[key_val[0]] = key_val[1]
                    else:
                        labels[key_val[0]] = ''
                    logger.debug("new label %s=%s", repr(key_val[0]), repr(labels[key_val[0]]))
        return labels


def figure_out_dockerfile(absolute_path, local_path=None):
    """
    try to figure out dockerfile from provided path and optionally from relative local path
    this is meant to be used with git repo: absolute_path is path to git repo,
    local_path is path to dockerfile within git repo

    :param absolute_path:
    :param local_path:
    :return: tuple, (dockerfile_path, dir_with_dockerfile_path)
    """
    logger.info("find dockerfile")
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
    logger.debug("dockerfile found: '%s'", df_path)
    return df_path, df_dir


class CommandResult(object):
    def __init__(self, logs, error=None, error_detail=None):
        self._logs = logs
        self._error = error
        self._error_detail = error_detail

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
    # FIXME: this function is getting pretty big, let's break it down a bit
    #        and merge it into CommandResult
    logger.info("wait_for_command")
    logs = []
    error = None
    error_message = None
    while True:
        try:
            parsed_item = None
            item = next(logs_generator)  # py2 & 3 compat
            item = item.decode("utf-8")
            try:
                parsed_item = json.loads(item)
            except ValueError:
                pass

            # make sure the json is an object
            if isinstance(parsed_item, dict):
                line = parsed_item.get("stream", "")
            else:
                parsed_item = None
                line = item

            for l in re.split(r"\r?\n", line, re.MULTILINE):
                # line = line.replace("\r\n", " ").replace("\n", " ").strip()
                l = l.strip()
                if l:
                    logger.debug(l)
            logs.append(item)
            if parsed_item is not None:
                error = parsed_item.get("error", None)
                error_message = parsed_item.get("errorDetail", None)
                if error:
                    logger.error(item.strip())
        except StopIteration:
            logger.info("no more logs")
            break
    cr = CommandResult(logs=logs, error=error, error_detail=error_message)
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
    logger.info("clone git repo")
    logger.debug("url = '%s', dir = '%s', commit = '%s'",
                 git_url, target_dir, commit)

    # http://stackoverflow.com/questions/1911109/clone-a-specific-git-branch/4568323#4568323
    # -b takes only refs, not SHA-1
    cmd = ["git", "clone", "-b", commit, "--single-branch", git_url, quote(target_dir)]
    logger.debug("Cloning single branch: %s", cmd)
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as ex:
        logger.warning(repr(ex))
        # let's try again with plain `git clone $url && git checkout`
        cmd = ["git", "clone", git_url, quote(target_dir)]
        logger.debug("Cloning: %s", cmd)
        subprocess.check_call(cmd)
        cmd = ["git", "checkout", commit]
        logger.debug("Checking out branch: %s", cmd)
        subprocess.check_call(cmd, cwd=target_dir)
    cmd = ["git", "rev-parse", "HEAD"]
    logger.debug("getting SHA-1 of provided ref: %s", cmd)
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
    logger.info("rendered repo: %s", repr(rendered_repo))
    return rendered_repo
