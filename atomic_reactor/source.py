"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Code for getting source code to put inside container.
"""

import logging
import copy
import os
import shutil
import tempfile
import collections
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

from atomic_reactor import util
from atomic_reactor.constants import CONTAINER_BUILD_METHODS, REPO_CONTAINER_CONFIG
from atomic_reactor.util import read_yaml_from_file_path

logger = logging.getLogger(__name__)


# Intended for use as vcs-type, vcs-url and vcs-ref docker labels as defined
# in https://github.com/projectatomic/ContainerApplicationGenericLabels
VcsInfo = collections.namedtuple('VcsInfo', ['vcs_type', 'vcs_url', 'vcs_ref'])


class SourceConfig(object):
    """ read container.yaml file from build source and store as attrs """

    def __init__(self, build_path):
        self.data = {}
        file_path = os.path.join(build_path, REPO_CONTAINER_CONFIG)
        if os.path.exists(file_path):
            try:
                # read file and validate against schema
                self.data = read_yaml_from_file_path(file_path, 'schemas/container.json') or {}
            except Exception:
                logger.exception("Failed to load and validate source config YAML from " + file_path)
                raise

        self.autorebuild = self.data.get('autorebuild') or {}
        self.flatpak = self.data.get('flatpak')
        self.compose = self.data.get('compose')
        self.image_build_method = meth = self.data.get('image_build_method')
        assert meth is None or meth in CONTAINER_BUILD_METHODS, (
               "unknown build method '{}' specified in {}; also, schema validated it."
               .format(meth, REPO_CONTAINER_CONFIG)
        )


class Source(object):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        self._config = None
        self.provider = provider
        self.uri = uri
        self.dockerfile_path = dockerfile_path
        self.provider_params = provider_params or {}
        # TODO: do we want to delete tmpdir when destroying the object?
        self.tmpdir = tmpdir or tempfile.mkdtemp()
        logger.debug("workdir is %r", self.tmpdir)
        parsed_uri = urlparse(uri)
        git_reponame = os.path.basename(parsed_uri.path)
        if git_reponame.endswith('.git'):
            git_reponame = git_reponame[:-4]

        self.source_path = os.path.join(self.tmpdir, git_reponame)
        logger.debug("source path is %r", self.source_path)

    @property
    def path(self):
        return self.get()

    @property
    def workdir(self):
        return self.tmpdir

    def get(self):
        """Run this to get source and save it to `tmpdir` or a newly created tmpdir."""
        raise NotImplementedError('Must override in subclasses!')

    def get_build_file_path(self):
        # TODO: will we need figure_out_build_file as a separate method?
        return util.figure_out_build_file(self.path, self.dockerfile_path)

    @property
    def config(self):
        # contents of container.yaml
        self._config = self._config or SourceConfig(self.path)
        return self._config

    def remove_tmpdir(self):
        shutil.rmtree(self.tmpdir)

    def get_vcs_info(self):
        """Returns VcsInfo namedtuple or None if not applicable."""
        return None


class GitSource(Source):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        super(GitSource, self).__init__(provider, uri, dockerfile_path,
                                        provider_params, tmpdir)
        self.git_commit = self.provider_params.get('git_commit', None)
        self.lg = util.LazyGit(self.uri, self.git_commit, self.source_path)

    @property
    def commit_id(self):
        return self.lg.commit_id

    def get(self):
        return self.lg.git_path

    def get_vcs_info(self):
        return VcsInfo(
            vcs_type='git',
            vcs_url=self.lg.git_url,
            vcs_ref=self.lg.commit_id
        )

    def reset(self, git_reference):
        self.lg.reset(git_reference)


class PathSource(Source):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        super(PathSource, self).__init__(provider, uri, dockerfile_path,
                                         provider_params, tmpdir)
        # make sure we have canonical URI representation even if we got path without "file://"
        if not self.uri.startswith('file://'):
            self.uri = 'file://' + self.uri
        self.schemeless_path = self.uri[len('file://'):]
        os.makedirs(self.source_path)

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


def get_source_instance_for(source, tmpdir=None):
    validate_source_dict_schema(source)
    klass = None
    provider = source['provider'].lower()
    if provider == 'git':
        klass = GitSource
    elif provider == 'path':
        klass = PathSource
    else:
        raise ValueError('unknown source provider "{0}"'.format(provider))

    # don't modify original source
    args = copy.deepcopy(source)
    args['tmpdir'] = tmpdir
    return klass(**args)


def validate_source_dict_schema(sd):
    if not isinstance(sd, dict):
        raise ValueError('"source" must be a dict')
    for k in ['provider', 'uri']:
        if k not in sd:
            raise ValueError('"source" must contain "{0}" key'.format(k))
