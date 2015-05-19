"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Code for getting source code to put inside container.
"""

import copy
import os
import shutil
import tempfile

from dock import util

class Source(object):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        self.provider = provider
        self.uri = uri
        self.dockerfile_path = dockerfile_path
        self.provider_params = provider_params or {}
        # TODO: do we want to delete tmpdir when destroying the object?
        self.tmpdir = tmpdir or tempfile.mkdtemp()

    @property
    def path(self):
        return self.get()

    def get(self):
        """Run this to get source and save it to `tmpdir` or a newly created tmpdir."""
        raise NotImplementedError('Must override in subclasses!')

    def get_dockerfile_path(self):
        # TODO: will we need figure_out_dockerfile as a separate method
        return util.figure_out_dockerfile(self.path, self.dockerfile_path)


class GitSource(Source):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        super(GitSource, self).__init__(provider, uri, dockerfile_path,
                provider_params, tmpdir)
        self.git_commit = self.provider_params.get('git_commit', None)
        self.lg = util.LazyGit(self.uri, self.git_commit, self.tmpdir)

    def get(self):
        return self.lg.git_path


class PathSource(Source):
    def __init__(self, provider, uri, dockerfile_path=None, provider_params=None, tmpdir=None):
        super(PathSource, self).__init__(provider, uri, dockerfile_path,
                provider_params, tmpdir)
        self.schemeless_path = self.uri[len('file://'):]

    def get(self):
        # TODO: check that self.schemeless_path is a directory?
        base = os.path.basename(self.schemeless_path)
        copy_to = os.path.join(self.tmpdir, base)
        if os.path.exists(copy_to):
            return copy_to
        else:
            shutil.copytree(self.schemeless_path, copy_to)
        return copy_to


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
