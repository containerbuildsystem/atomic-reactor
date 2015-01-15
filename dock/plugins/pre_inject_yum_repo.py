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


        shell_cmd = 'printf "%s"' % rendered_repos + \
                    ' >%(repo_path)s && %%(yum_command)s && yum clean all && rm -f %(repo_path)s' \
                    % {'repo_path': self.repo_path}
        for line in fileinput.input(self.workflow.builder.df_path, inplace=1):
            re_match = re.match(r"RUN (?P<yum_command>yum (install|update).+$)", line)
            if re_match:
                re_match_dict = re_match.groupdict()
                sys.stdout.write(line.replace(re_match_dict['yum_command'], shell_cmd % re_match_dict))
            else:
                sys.stdout.write(line)


