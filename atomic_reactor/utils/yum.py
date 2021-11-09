"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Utility functions to manipulate a yum repo. Guarantees that a yum repo will have a unique
name and the .repo suffix.

"""
from atomic_reactor.constants import YUM_REPOS_DIR
from atomic_reactor.util import get_retrying_requests_session, sha256sum
import os
import os.path
import logging

from urllib.parse import unquote, urlsplit
import configparser
from io import StringIO


logger = logging.getLogger(__name__)

REPO_SUFFIX = ".repo"


class YumRepo(object):
    def __init__(self, repourl, content='', dst_repos_dir=YUM_REPOS_DIR, add_hash=True):
        self.add_hash = add_hash
        self.repourl = repourl
        self.dst_repos_dir = dst_repos_dir

        self._content = None
        self.content = content
        self.config = None

    @property
    def filename(self):
        '''Returns the filename to be used for saving the repo file.

        The filename is derived from the repo url by injecting a suffix
        after the name and before the file extension. This suffix is a
        partial sha256 checksum of the full repourl. This avoids multiple
        repos from being written to the same file.
        '''
        urlpath = unquote(urlsplit(self.repourl, allow_fragments=False).path).rstrip(os.sep)
        basename = os.path.basename(urlpath)

        if not basename:
            raise RuntimeError('basename is empty for yum repo url: %s' % self.repourl)

        if not basename.endswith(REPO_SUFFIX):
            basename += REPO_SUFFIX
        if self.add_hash:
            suffix = '-' + sha256sum(self.repourl, abbrev_len=5)
        else:
            suffix = ''
        final_name = suffix.join(os.path.splitext(basename))
        return final_name

    @property
    def dst_filename(self):
        return os.path.join(self.dst_repos_dir, self.filename)

    @property
    def content(self):
        return self._content.encode('utf-8')

    @content.setter
    def content(self, content):
        try:
            self._content = content.decode('unicode_escape')
        except AttributeError:
            self._content = content

    def fetch(self):
        session = get_retrying_requests_session()
        response = session.get(self.repourl)
        response.raise_for_status()
        self.content = response.content

    def is_valid(self):
        try:
            self.config = configparser.ConfigParser()
            self.config.read_string(self._content)
        except configparser.Error:
            logger.warning("Invalid repo file found: '%s'", self.content)
            return False
        return True

    def set_proxy_for_all_repos(self, proxy_name):
        for section in self.config.sections():
            self.config.set(section, 'proxy', proxy_name)

        with StringIO() as output:
            self.config.write(output)
            self.content = output.getvalue()

    def write_content(self):
        logger.info("writing repo to '%s'", self.dst_filename)
        with open(self.dst_filename, "wb") as fp:
            fp.write(self.content)
            fp.flush()
        logger.debug("%s\n%s", self.repourl, self.content.strip())
