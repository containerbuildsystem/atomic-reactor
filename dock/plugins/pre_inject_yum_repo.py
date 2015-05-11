"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which injects custom yum repository in dockerfile.
"""
import os
import re
import uuid
from dock.plugin import PreBuildPlugin


def alter_yum_commands(df, wrap_str):
    regex = re.compile(r"RUN\s+(?P<yum_command>yum((\s.+\\\n)+)?(.+))", re.MULTILINE)
    sub_func = lambda match: wrap_str % {'yum_command': match.group('yum_command').rstrip()}
    return regex.sub(sub_func, df)


class InjectYumRepoPlugin(PreBuildPlugin):
    key = "inject_yum_repo"

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(InjectYumRepoPlugin, self).__init__(tasker, workflow)
        self.yum_repos_dir = '/etc/yum.repos.d/'
        self.repofile_name = 'dock-injected.repo'
        self.repo_path = os.path.join(self.yum_repos_dir, self.repofile_name)

    def run(self):
        """
        run the plugin
        """
        def escape_dollar(v):
            if isinstance(v, str):
                return v.replace('$', '\$')
            else:
                return v

        rendered_repos = ''
        for repo in self.workflow.repos.get('yum', []):
            repo.setdefault("name", str(uuid.uuid4().hex[:6]))
            rendered_repo = ''
            for key, value in repo.items():
                rendered_repo += r"%s=%s\n" % (key, escape_dollar(value))
            rendered_repo = r'[%(name)s]\n' % repo + rendered_repo
            rendered_repos += rendered_repo

        wrap_cmd = 'RUN printf "%s"' % rendered_repos + \
            ' >%(repo_path)s && %%(yum_command)s && yum clean all && rm -f %(repo_path)s' \
            % {'repo_path': self.repo_path}

        with open(self.workflow.builder.df_path, "r+") as fd:
            df = fd.read()
            out = alter_yum_commands(df, wrap_cmd)
            fd.seek(0)
            fd.truncate()
            fd.write(out)
