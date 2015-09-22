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

from atomic_reactor import util
from atomic_reactor.constants import SOURCE_DIRECTORY_NAME


logger = logging.getLogger(__name__)


# Intended for use as vcs-type, vcs-url and vcs-ref docker labels as defined
# in https://github.com/projectatomic/ContainerApplicationGenericLabels
VcsInfo = collections.namedtuple('VcsInfo', ['vcs_type', 'vcs_url', 'vcs_ref'])


class Source(object):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        self.provider = provider
        self.uri = uri
        self.dockerfile_path = dockerfile_path
        self.provider_params = provider_params or {}
        # TODO: do we want to delete tmpdir when destroying the object?
        self.tmpdir = tmpdir or tempfile.mkdtemp()
        logger.debug("workdir is %r", self.tmpdir)
        self.source_path = os.path.join(self.tmpdir, SOURCE_DIRECTORY_NAME)
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

    def get_dockerfile_path(self):
        # TODO: will we need figure_out_dockerfile as a separate method?
        return util.figure_out_dockerfile(self.path, self.dockerfile_path)

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
