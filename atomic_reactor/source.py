"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Code for getting source code to put inside container.
"""

import functools
import logging
import os
import shutil
import tempfile
from textwrap import dedent
from typing import Any, List, Callable, TypeVar
import collections
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

from osbs.utils import clone_git_repo, get_commit_id

from atomic_reactor import util
from atomic_reactor.constants import REPO_CONTAINER_CONFIG
from atomic_reactor.util import read_yaml_from_file_path

logger = logging.getLogger(__name__)


# Intended for use as vcs-type, vcs-url and vcs-ref docker labels as defined
# in https://github.com/projectatomic/ContainerApplicationGenericLabels
VcsInfo = collections.namedtuple('VcsInfo', ['vcs_type', 'vcs_url', 'vcs_ref'])


def make_list(value: Any) -> List[Any]:
    if not isinstance(value, list):
        return [value]
    return value


class SourceConfig(object):
    """ read container.yaml file from build source and store as attrs """

    def __init__(self, build_path):
        self.data = {}
        self.file_path = os.path.join(build_path, REPO_CONTAINER_CONFIG)
        if os.path.exists(self.file_path):
            try:
                # read file and validate against schema
                self.data = read_yaml_from_file_path(
                    self.file_path, 'schemas/container.json', 'osbs'
                ) or {}
            except Exception:
                logger.exception(
                    "Failed to load and validate source config YAML from %s",
                    self.file_path
                )
                raise

        self.release_env_var = self.data.get('set_release_env')
        self.flatpak = self.data.get('flatpak')
        self.compose = self.data.get('compose')
        self.go = self.data.get('go') or {}
        self.inherit = self.compose.get('inherit', False) if self.compose else False
        if self.compose:
            # removing inherit from compose so it can be evaluated as a bool in order
            # to decide whether any ODCS composes will be created
            self.compose.pop('inherit', None)
        self.remote_source = self.data.get('remote_source')
        self.remote_sources = self.data.get('remote_sources')
        self.operator_manifests = self.data.get('operator_manifests')

        self.platforms = self.data.get('platforms') or {'not': [], 'only': []}
        self.platforms['not'] = make_list(self.platforms.get('not', []))
        self.platforms['only'] = make_list(self.platforms.get('only', []))

    @property
    def excluded_platforms(self) -> List[str]:
        return self.platforms['not']

    @property
    def only_platforms(self) -> List[str]:
        return self.platforms['only']


SourceT = TypeVar('SourceT', bound='Source')
T = TypeVar('T')


def path_must_exist(method: Callable[[SourceT], T]) -> Callable[[SourceT], T]:
    """Make a Source method check that self.path exists before doing anything else."""

    @functools.wraps(method)
    def method_with_path_check(self: SourceT) -> T:
        if not os.path.exists(self.path):
            raise RuntimeError(f'Expected source path {self.path} does not exist')
        return method(self)

    return method_with_path_check


class Source(object):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, workdir=None):
        self._config = None
        self.provider = provider
        self.uri = uri
        self.dockerfile_path = dockerfile_path
        self.provider_params = provider_params or {}
        self._workdir = workdir or tempfile.mkdtemp()
        logger.debug("workdir is %r", self.workdir)
        parsed_uri = urlparse(uri)
        git_reponame = os.path.basename(parsed_uri.path)
        if git_reponame.endswith('.git'):
            git_reponame = git_reponame[:-4]

        self.source_path = os.path.join(self.workdir, git_reponame)
        logger.debug("source path is %r", self.source_path)

    @property
    def path(self):
        return self.source_path

    @property
    def workdir(self):
        return self._workdir

    def get(self) -> str:
        """Run this to get source and save it to `workdir` or a newly created workdir.

        Return the path to the saved source.
        """
        raise NotImplementedError('Must override in subclasses!')

    @path_must_exist
    def get_build_file_path(self):
        return util.figure_out_build_file(self.path, self.dockerfile_path)

    # mypy does not support decorated properties; https://github.com/python/mypy/issues/1362
    @property  # type: ignore
    @path_must_exist
    def config(self):
        # contents of container.yaml
        self._config = self._config or SourceConfig(self.path)
        return self._config

    def remove_workdir(self):
        for entry_name in os.listdir(self.workdir):
            entry_path = os.path.join(self.workdir, entry_name)
            if os.path.isfile(entry_path):
                os.unlink(entry_path)
            else:
                shutil.rmtree(entry_path)

    def get_vcs_info(self):
        """Returns VcsInfo namedtuple or None if not applicable."""
        return None


class GitSource(Source):
    @property  # type: ignore
    @path_must_exist
    def commit_id(self):
        return get_commit_id(self.path)

    def get(self) -> str:
        if not os.path.exists(self.path):
            commit = self.provider_params.get('git_commit', None)
            branch = self.provider_params.get('git_branch', None)
            depth = self.provider_params.get('git_commit_depth', None)
            clone_git_repo(
                self.uri, target_dir=self.path, commit=commit, branch=branch, depth=depth
            )
        return self.path

    @path_must_exist
    def get_vcs_info(self) -> VcsInfo:
        return VcsInfo(vcs_type='git', vcs_url=self.uri, vcs_ref=self.commit_id)


class PathSource(Source):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, workdir=None):
        super(PathSource, self).__init__(provider, uri, dockerfile_path,
                                         provider_params, workdir)
        # make sure we have canonical URI representation even if we got path without "file://"
        if not self.uri.startswith('file://'):
            self.uri = 'file://' + self.uri
        self.schemeless_path = self.uri[len('file://'):]
        os.makedirs(self.source_path)

    @property
    def path(self):
        return self.get()

    def get(self):
        # work around the weird behaviour of copytree, which requires the top dir
        #  to *not* exist
        for f in os.listdir(self.schemeless_path):
            old = os.path.join(self.schemeless_path, f)
            new = os.path.join(self.source_path, f)
            if os.path.exists(new):
                # this is the second invocation of this method; just break the loop
                break
            else:
                if os.path.isdir(old):
                    shutil.copytree(old, new)
                else:
                    shutil.copy2(old, new)
        return self.source_path


class DummySource(Source):
    """Dummy source that just provides defaults in cases where we don't expect
     operations with real data
    """
    def __init__(
        self, provider, uri, dockerfile_path=None,
        provider_params=None, workdir=None
    ):
        # intentionally not calling `super()`
        self._config = None
        self.provider = provider
        self.uri = uri
        self.dockerfile_path = dockerfile_path
        self.provider_params = provider_params or {}
        self._workdir = workdir or tempfile.mkdtemp()
        logger.debug("workdir is %r", self.workdir)
        self.source_path = tempfile.mkdtemp(dir=self.workdir)
        logger.debug("source path is %r", self.source_path)
        self._add_fake_dockerfile()

    def _add_fake_dockerfile(self):
        dockerfile = dedent("""\
            FROM scratch
        """)
        with open(os.path.join(self.source_path, 'Dockerfile'), 'w') as f:
            f.write(dockerfile)

    def get(self):
        return self.source_path
