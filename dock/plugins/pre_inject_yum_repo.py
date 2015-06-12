"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which injects custom yum repository in dockerfile.
"""
import os
import re
from dock.constants import YUM_REPOS_DIR, RELATIVE_REPOS_PATH
from dock.util import DockerfileParser
from dock.plugin import PreBuildPlugin


logger = None


def alter_yum_commands(df, wrap_str):
    regex = re.compile(r"RUN\s+(?P<yum_command>yum((\s.+\\\n)+)?(.+))", re.MULTILINE)
    sub_func = lambda match: wrap_str % {'yum_command': match.group('yum_command').rstrip()}
    return regex.sub(sub_func, df)


def add_yum_repos_to_dockerfile(yumrepos, df):
    df_lines = df.lines
    if len(df_lines) == 0:
        raise RuntimeError("Empty Dockerfile")

    # Find where to insert commands

    preinsert = None
    structure = df.structure
    for insndesc in structure:
        insn = insndesc['instruction'].lower()
        if insn == 'maintainer':
            # MAINTAINER line: stop looking
            preinsert = insndesc['endline'] + 1
            break
        elif insn == 'from':
            # FROM line: can use this, but keep looking in case there
            # is a MAINTAINER line
            preinsert = insndesc['endline'] + 1

    if preinsert is None:
        raise RuntimeError("No FROM line in Dockerfile")

    # Look for the last 'yum' invocation
    postinsert = None  # append by default
    yumre = re.compile(r'^.*\byum\b')
    for insndesc in structure:
        if insndesc['instruction'].lower() == 'run' and \
           yumre.match(insndesc['content']):
            postinsert = insndesc['endline'] + 1

    newdf = df_lines[:preinsert]
    newdf.append("ADD %s* '%s'\n" % (RELATIVE_REPOS_PATH, YUM_REPOS_DIR))
    newdf.extend(df_lines[preinsert:postinsert])
    newdf.append("RUN rm -f " +
                 " ".join(["'%s'" % yumrepo
                           for yumrepo in yumrepos]) +
                 "\n")
    if postinsert is not None:
        newdf.extend(df_lines[postinsert:])

    return newdf


def wrap_yum_commands(yum_repos, df_path):
    cmd_template = "RUN %(generate_repos)s&& %%(yum_command)s && yum clean all &&%(clean_repos)s"
    generate_repos = ""
    clean_repos = " rm -f"
    for repo, repo_content in yum_repos.items():
        generate_repos += 'printf "%s" >%s ' % (repo_content, repo)
        clean_repos += " %s" % repo

    wrap_cmd = cmd_template % {
        "generate_repos": generate_repos,
        "clean_repos": clean_repos,
    }

    logger.debug("wrap cmd is %s", repr(wrap_cmd))

    df = DockerfileParser(df_path)
    df_content = df.content
    df.content = alter_yum_commands(df_content, wrap_cmd)


class InjectYumRepoPlugin(PreBuildPlugin):
    key = "inject_yum_repo"
    can_fail = False

    def __init__(self, tasker, workflow, wrap_commands=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param wrap_commands: bool, wrap yum calls
        """
        # call parent constructor
        super(InjectYumRepoPlugin, self).__init__(tasker, workflow)
        self.wrap_commands = wrap_commands
        self.host_repos_path = os.path.join(self.workflow.builder.df_dir, RELATIVE_REPOS_PATH)

        global logger
        logger = self.log

    def run(self):
        """
        run the plugin
        """
        # dict comprehension is syntax error on 2.6
        yum_repos = {}
        for key, value in self.workflow.files.items():
            if key.startswith(YUM_REPOS_DIR):
                yum_repos[key] = value
        if self.wrap_commands:
            wrap_yum_commands(yum_repos, self.workflow.builder.df_path)
        else:
            if not yum_repos:
                return
            # absolute path in containers -> relative path within context
            repos_host_cont_mapping = {}
            host_repos_path = os.path.join(self.workflow.builder.df_dir, RELATIVE_REPOS_PATH)
            self.log.info("creating directory for yum repos: %s", host_repos_path)
            os.mkdir(host_repos_path)

            for repo, repo_content in self.workflow.files.items():
                repo_basename = os.path.basename(repo)
                repo_relative_path = os.path.join(RELATIVE_REPOS_PATH, repo_basename)
                repo_host_path = os.path.join(host_repos_path, repo_basename)
                self.log.info("writing repo to '%s'", repo_host_path)
                with open(repo_host_path, "wb") as fp:
                    fp.write(repo_content.encode("utf-8"))
                repos_host_cont_mapping[repo] = repo_relative_path

            df = DockerfileParser(self.workflow.builder.df_path)
            df_lines = df.lines
            df.lines = add_yum_repos_to_dockerfile(repos_host_cont_mapping, df)
