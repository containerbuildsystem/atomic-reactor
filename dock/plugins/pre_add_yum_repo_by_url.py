"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Add arbitrary yum repo, specified by URL of repo file, to a list of
repos which should be injected into built image.

Example configuration to add content of repo file at URL:

{
    "name": "add_yum_repo_by_url",
    "args": {
        "repourls": ["http://example.com/myrepo/myrepo.repo"]
    }
}

"""
from dock.plugin import PreBuildPlugin
import os
import os.path
import re
import requests

try:
    # py2
    from urlparse import unquote, urlsplit
except ImportError:
    # py3
    from urllib.parse import unquote, urlsplit


class YumRepo(object):
    def __init__(self, repourl, src_repos_dir, dst_repos_dir):
        self.repourl = repourl
        self.src_repos_dir = src_repos_dir
        self.dst_repos_dir = dst_repos_dir

    @property
    def filename(self):
        urlpath = unquote(urlsplit(self.repourl, allow_fragments=False).path)
        return os.path.basename(urlpath)

    @property
    def src_filename(self):
        return os.path.join(self.src_repos_dir, self.filename)

    @property
    def dst_filename(self):
        return os.path.join(self.dst_repos_dir, self.filename)

    def fetch(self, relative_to):
        response = requests.get(self.repourl)
        response.raise_for_status()
        with open(os.path.join(relative_to, self.src_filename), "wb") as fp:
            fp.write(response.content)


def add_yum_repos_to_dockerfile(yumrepos, df, src_repos_dir, dst_repos_dir):
    num_lines = len(df)
    if num_lines == 0:
        raise RuntimeError("Empty Dockerfile")

    # Find where to insert commands

    def first_word_is(word):
        return re.compile(r"^\s*" + word + r"\s", flags=re.IGNORECASE)

    fromre = first_word_is("FROM")
    maintainerre = first_word_is("MAINTAINER")
    preinsert = None
    for n in range(num_lines):
        if maintainerre.match(df[n]):
            # MAINTAINER line: stop looking
            preinsert = n + 1
            break
        elif fromre.match(df[n]):
            # FROM line: can use this, but keep looking in case there
            # is a MAINTAINER line
            preinsert = n + 1

    if preinsert is None:
        raise RuntimeError("No FROM line in Dockerfile")

    cmdre = first_word_is("(CMD|ENTRYPOINT)")
    postinsert = None  # append by default
    for n in range(preinsert, num_lines):
        if cmdre.match(df[n]):
            postinsert = n
            break

    newdf = df[:preinsert]
    newdf.append("ADD '%s'/* '%s'\n" % (src_repos_dir, dst_repos_dir))
    newdf.extend(df[preinsert:postinsert])
    newdf.append("RUN rm -f " +
                 " ".join(["'%s'" % yumrepo.dst_filename
                           for yumrepo in yumrepos]) +
                 "\n")
    if postinsert is not None:
        newdf.extend(df[postinsert:])

    return newdf


class AddYumRepoByUrlPlugin(PreBuildPlugin):
    key = "add_yum_repo_by_url"
    can_fail = False

    def __init__(self, tasker, workflow, repourls):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param repourls: list of str, URLs to the repo files
        """
        # call parent constructor
        super(AddYumRepoByUrlPlugin, self).__init__(tasker, workflow)
        self.repourls = repourls
        self.src_repos_dir = 'repos'
        self.dst_repos_dir = '/etc/yum.repos.d/'

    def run(self):
        """
        run the plugin
        """
        yumrepos = [YumRepo(repourl, self.src_repos_dir, self.dst_repos_dir)
                    for repourl in self.repourls]
        if yumrepos:
            os.mkdir(os.path.join(self.workflow.builder.df_dir,
                                  self.src_repos_dir))
            for yumrepo in yumrepos:
                yumrepo.fetch(self.workflow.builder.df_dir)

            with open(self.workflow.builder.df_path, "r+") as fp:
                df = fp.readlines()
                df = add_yum_repos_to_dockerfile(yumrepos, df,
                                                 self.src_repos_dir,
                                                 self.dst_repos_dir)
                fp.seek(0)
                fp.truncate()
                fp.writelines(df)
