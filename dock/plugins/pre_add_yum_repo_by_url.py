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
import os.path
import re

try:
    # py2
    from urlparse import unquote, urlsplit
except ImportError:
    # py3
    from urllib.parse import unquote, urlsplit


class YumRepo(object):
    def __init__(self, url, yum_repos_dir):
        self.url = url
        self.yum_repos_dir = yum_repos_dir

    @property
    def quoted_filename(self):
        urlpath = unquote(urlsplit(self.url, allow_fragments=False).path)
        filename = os.path.join(self.yum_repos_dir, os.path.basename(urlpath))
        return "'%s'" % filename


def add_yum_repos_to_dockerfile(yumrepos, df):
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
    cmds = ["RUN wget -O %s %s\n" % (yumrepo.quoted_filename, yumrepo.url)
            for yumrepo in yumrepos]
    newdf.extend(cmds)
    newdf.extend(df[preinsert:postinsert])
    newdf.append("RUN rm -f " +
                 " ".join([yumrepo.quoted_filename for yumrepo in yumrepos]) +
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
        self.yum_repos_dir = '/etc/yum.repos.d/'

    def run(self):
        """
        run the plugin
        """
        yumrepos = [YumRepo(repourl, self.yum_repos_dir)
                    for repourl in self.repourls]
        if yumrepos:
            with open(self.workflow.builder.df_path, "r+") as fp:
                df = fp.readlines()
                df = add_yum_repos_to_dockerfile(yumrepos, df)
                fp.seek(0)
                fp.truncate()
                fp.writelines(df)
