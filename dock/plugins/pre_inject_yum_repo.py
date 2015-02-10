"""
Pre build plugin which injects custom yum repository in dockerfile.
"""
import fileinput
import os
import re
import uuid
import sys
from dock.plugin import PreBuildPlugin


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
        rendered_repos = ''
        for repo in self.workflow.repos.get('yum', []):
            repo.setdefault("name", str(uuid.uuid4().get_hex()[:6]))
            rendered_repo = ''
            for key, value in repo.items():
                rendered_repo += r"%s=%s\n" % (key, value)
            rendered_repo = r'[%(name)s]\n' % repo + rendered_repo
            rendered_repos += rendered_repo

        shell_cmd = lambda match: 'RUN printf "%s"' % rendered_repos + \
                    ' >%(repo_path)s && %%(yum_command)s && yum clean all && rm -f %(repo_path)s' \
                    % {'repo_path': self.repo_path, 'yum_command': match.group('yum_command').rstrip() }


        dockerfile_content = "".join([line for line in fileinput.input(self.workflow.builder.df_path, inplace=1)])
        rege = re.compile(r"RUN (?P<yum_command>yum((\s.+\\\n)+)?(.+))")
        sys.stdout.write(rege.sub(shell_cmd, dockerfile_content))
