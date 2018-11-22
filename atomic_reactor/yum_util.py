"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Utility functions to manipulate a yum repo. Guarantees that a yum repo will have a unique
name and the .repo suffix.

"""
from atomic_reactor.constants import YUM_REPOS_DIR
from atomic_reactor.util import get_retrying_requests_session
from hashlib import md5
import os
import os.path
import logging

try:
    # py2
    from urlparse import unquote, urlsplit
    import ConfigParser as configparser
    # We import BytesIO as StringIO as configparser can't properly write
    from io import BytesIO, BytesIO as StringIO
except ImportError:
    # py3
    from urllib.parse import unquote, urlsplit
    import configparser
    from io import BytesIO, StringIO


logger = logging.getLogger(__name__)

REPO_SUFFIX = ".repo"


class YumRepo(object):
    def __init__(self, repourl, content='', dst_repos_dir=YUM_REPOS_DIR, add_hash=True):
        self.add_hash = add_hash
        self.repourl = repourl
        self.dst_repos_dir = dst_repos_dir

        self.content = content

    @property
    def filename(self):
        '''Returns the filename to be used for saving the repo file.

        The filename is derived from the repo url by injecting a suffix
        after the name and before the file extension. This suffix is a
        partial md5 checksum of the full repourl. This avoids multiple
        repos from being written to the same file.
        '''
        urlpath = unquote(urlsplit(self.repourl, allow_fragments=False).path)
        basename = os.path.basename(urlpath)
        if not basename.endswith(REPO_SUFFIX):
            basename += REPO_SUFFIX
        if self.add_hash:
            suffix = '-' + md5(self.repourl.encode('utf-8')).hexdigest()[:5]
        else:
            suffix = ''
        final_name = suffix.join(os.path.splitext(basename))
        return final_name

    @property
    def dst_filename(self):
        return os.path.join(self.dst_repos_dir, self.filename)

    def fetch(self):
        session = get_retrying_requests_session()
        response = session.get(self.repourl)
        response.raise_for_status()
        self.content = response.content

    def is_valid(self):
        # Using BytesIO as configparser in 2.7 can't work with unicode
        # see http://bugs.python.org/issue11597
        with BytesIO(self.content) as buf:
            self.config = configparser.ConfigParser()
            try:
                # Try python2 method
                try:
                    self.config.read_string(self.content.decode('unicode_escape'))
                except AttributeError:
                    # Fallback to py3 method
                    self.config.readfp(buf)
            except configparser.Error:
                logger.warn("Invalid repo file found: '%s'", self.content)
                return False
            else:
                return True

    def set_proxy_for_all_repos(self, proxy_name):
        for section in self.config.sections():
            self.config.set(section, 'proxy', proxy_name)

        with StringIO() as output:
            self.config.write(output)
            self.content = output.getvalue()

    def write_and_return_content(self):
        logger.info("writing repo to '%s'", self.dst_filename)
        with open(self.dst_filename, "wb") as fp:
            fp.write(self.content.encode("utf-8"))
        logger.debug("%s\n%s", self.repourl, self.content.strip())
