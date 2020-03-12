"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which injects custom yum repository in dockerfile.
"""
from __future__ import absolute_import

import os
from atomic_reactor.constants import YUM_REPOS_DIR, RELATIVE_REPOS_PATH, INSPECT_CONFIG
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser
from atomic_reactor.utils.yum import YumRepo


def add_yum_repos_to_dockerfile(yumrepos, df, inherited_user, base_from_scratch):
    if df.baseimage is None:
        raise RuntimeError("No FROM line in Dockerfile")

    # Determine the USER the final image should end with
    final_user_line = "USER " + inherited_user if inherited_user else None
    # Look for the last USER after the last FROM... by looking in reverse
    for insndesc in reversed(df.structure):
        if insndesc['instruction'] == 'USER':
            final_user_line = insndesc['content']  # we will reuse the line verbatim
            break
        if insndesc['instruction'] == 'FROM':
            break  # no USER specified in final stage

    # Insert the ADD line at the beginning of each stage
    df.add_lines(
        "ADD %s* %s" % (RELATIVE_REPOS_PATH, YUM_REPOS_DIR),
        all_stages=True, at_start=True, skip_scratch=True
    )

    # Insert line(s) to remove the repos
    cleanup_lines = [
        "RUN rm -f " +
        " ".join(["'%s'" % repo for repo in yumrepos])
    ]
    # If needed, change to root in order to RUN rm, then change back.
    if final_user_line:
        cleanup_lines.insert(0, "USER root")
        cleanup_lines.append(final_user_line)

    if not base_from_scratch:
        df.add_lines(*cleanup_lines)


class InjectYumRepoPlugin(PreBuildPlugin):
    key = "inject_yum_repo"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(InjectYumRepoPlugin, self).__init__(tasker, workflow)
        self.host_repos_path = os.path.join(self.workflow.builder.df_dir, RELATIVE_REPOS_PATH)

    def run(self):
        """
        run the plugin
        """
        yum_repos = {k: v for k, v in self.workflow.files.items() if k.startswith(YUM_REPOS_DIR)}
        if not yum_repos:
            return
        # absolute path in containers -> relative path within context
        host_repos_path = os.path.join(self.workflow.builder.df_dir, RELATIVE_REPOS_PATH)
        self.log.info("creating directory for yum repos: %s", host_repos_path)
        os.mkdir(host_repos_path)

        for repo, repo_content in self.workflow.files.items():
            yum_repo = YumRepo(repourl=repo, content=repo_content, dst_repos_dir=host_repos_path,
                               add_hash=False)
            yum_repo.write_content()

        # Find out the USER inherited from the base image
        inspect = self.workflow.builder.base_image_inspect
        inherited_user = ''
        if not self.workflow.builder.base_from_scratch:
            inherited_user = inspect.get(INSPECT_CONFIG).get('User', '')
        df = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        yum_repos = list(self.workflow.files)
        add_yum_repos_to_dockerfile(yum_repos, df, inherited_user,
                                    self.workflow.builder.base_from_scratch)
        for repo in yum_repos:
            self.log.info("injected yum repo: %s", repo)
