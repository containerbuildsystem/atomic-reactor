"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import shlex
try:
    # py3
    from shlex import quote
except ImportError:
    from pipes import quote

import os
from pygit2 import init_repository, Signature
import subprocess

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.constants import PY2
from dockerfile_parse import DockerfileParser


def shlex_splits(value):
    if PY2 and isinstance(value, unicode):
        value = value.encode('utf-8')
        splits = shlex.split(value)
        return [split.decode('utf-8') for split in splits]
    else:
        return shlex.split(value)


class BumpReleasePlugin(PreBuildPlugin):
    """Git branch management plugin

    For rebuilds, push a new commit incrementing the Release label.

    For initial builds, verify the branch is at the specified commit
    hash.

    When this plugin is configured by osbs-client, the Build's source
    git ref is actually the branch (from --git-branch), not the
    original SHA-1. The SHA-1 specified by --git-commit is stored in
    the configuration for this plugin.

    Example configuration:

    {
      "name": "bump_release",
      "args": {
        "git_ref": "12345678....",
        "author_name": "OSBS Build System",
        "author_email": "root@example.com"
      }
    }

    Additional optional arguments:
    - committer_name
    - committer_email
    - commit_message
    - push_url

    """

    key = "bump_release"
    can_fail = False  # We really want to stop the process

    commit_message = "Bumped release for automated rebuild"

    def __init__(self, tasker, workflow,
                 git_ref,
                 author_name, author_email,
                 committer_name=None, committer_email=None,
                 commit_message=None,
                 push_url=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param git_ref: str, commit hash expected on first build
        :param author_name: str, name to use for git commits
        :param author_email: str, email address for git commits
        :param committer_name: str, name to use for git commits (else author's)
        :param committer_email: str, email address for git commits (else
                                     author's)
        :param commit_message: str, git commit message
        :param push_url: str, URL for push
        """
        # call parent constructor
        super(BumpReleasePlugin, self).__init__(tasker, workflow)
        self.git_ref = git_ref
        self.author_name = author_name
        self.author_email = author_email
        self.committer_name = committer_name
        self.commiter_email = committer_email
        if committer_name is None:
            self.committer_name = author_name
        if committer_email is None:
            self.committer_email = author_email

        self.push_url = push_url
        if commit_message is not None:
            self.commit_message = commit_message

    def bump_local_file(self, parser, label_key, next_release):
        """
        Rewrite a local Dockerfile with an incremented Release label
        """

        # Find where in the file to put the next release
        content = startline = endline = None
        cmd = 'LABEL'
        for candidate in [insn for insn in parser.structure
                          if insn['instruction'] == cmd]:
            splits = shlex_splits(candidate['value'])

            # LABEL syntax is one of two types:
            if '=' not in splits[0]:  # LABEL name value
                # remove (double-)quotes
                value = candidate['value'].replace("'", "").replace('"', '')
                words = value.split(None, 1)
                if words[0] == label_key:
                    # Adjust label value
                    words[1] = next_release

                    # Now reconstruct the line
                    content = " ".join([cmd] + words) + '\n'
                    startline = candidate['startline']
                    endline = candidate['endline']
                    break
            else:  # LABEL "name"="value"
                for token in splits:
                    words = token.split("=", 1)
                    if words[0] == label_key:
                        # Adjust label value
                        words[1] = next_release
                        n = splits.index(token)
                        splits[n] = "=".join(map(quote, words))

                        # Now reconstruct the line
                        content = " ".join([cmd] + splits) + '\n'
                        startline = candidate['startline']
                        endline = candidate['endline']
                        break

        # We know the label we're looking for is there
        assert content and startline and endline

        # Re-write the Dockerfile
        lines = parser.lines
        del lines[startline:endline + 1]
        lines.insert(startline, content)
        parser.lines = lines

    @staticmethod
    def get_next_release(current_release):
        try:
            return str(int(current_release) + 1)
        except ValueError:
            isdigit = type(current_release).isdigit
            first_nondigit = [isdigit(x) for x in current_release].index(False)
            n = str(int(current_release[:first_nondigit]) + 1)
            return n + current_release[first_nondigit:]

    def bump(self, repo, branch):
        # Look in the git repository
        remote = repo.remotes['origin']
        if self.push_url:
            repo.config['push.default'] = 'simple'
            remote.push_url = self.push_url
            remote.save()

        # Bump the Release label
        label_key = 'Release'
        df_path = self.workflow.builder.df_path
        parser = DockerfileParser(df_path)
        current_release = parser.labels[label_key]
        next_release = self.get_next_release(current_release)
        self.log.info("New Release: %s", next_release)
        self.bump_local_file(parser, label_key, next_release)

        # Stage it
        index = repo.index
        index.add(os.path.basename(df_path))

        # Commit the change
        author = Signature(self.author_name, self.author_email)
        committer = Signature(self.committer_name, self.committer_email)
        repo.create_commit(branch.name, author, committer, self.commit_message,
                           index.write_tree(), [repo.head.peel().hex])

        # Push it
        self.log.info("Pushing to git repository")
        ssh_command = '/usr/bin/ssh -o StrictHostKeyChecking=no'
        os.environ['GIT_SSH_COMMAND'] = ssh_command

        # This doesn't seem to work:
        #   remote.push([branch.name])
        # because it uses libssh rather than /usr/bin/ssh and so krb5
        # auth fails.
        # Instead, run the git command to do it
        cmd = ['/usr/bin/git', 'push', 'origin']
        with open('/dev/null', 'r+') as devnull:
            p = subprocess.Popen(cmd,
                                 stdin=devnull,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 cwd=repo.workdir)

        (output, dummy) = p.communicate()
        status = p.wait()
        if status != 0:
            self.log.error("git (%d): %r", status, output)
            raise RuntimeError("exit code %d" % status)

        self.log.debug("git (success): %r", output)

    def verify_branch(self, branch):
        commit = branch.target.hex
        if commit != self.git_ref:
            self.log.error("Branch '%s' is at commit %s (expected %s)",
                           branch.shorthand, commit, self.git_ref)
            raise RuntimeError("Not at expected commit")

        self.log.info("Branch '%s' is at expected commit (%s)",
                      branch.shorthand, self.git_ref)

    def run(self):
        """
        run the plugin
        """

        if self.workflow.build_process_failed:
            self.log.info("Build already failed, not incrementing release")
            return

        # Ensure we can use the git repository already checked out for us
        source = self.workflow.source
        assert isinstance(source, GitSource)
        repo = init_repository(source.get())

        # Note: when this plugin is configured by osbs-client,
        # source.git_commit (the Build's source git ref) comes from
        # --git-branch not --git-commit. The value from --git-commit
        # went into our self.git_ref.
        branch = repo.lookup_branch(source.git_commit)

        if branch is None:
            self.log.error("Branch '%s' not found in git repo",
                           source.git_commit)
            raise RuntimeError("Branch '%s' not found" % source.git_commit)

        # We checked out the right branch
        assert repo.head.peel().hex == branch.target.hex

        # We haven't reset it to an earlier commit
        assert branch.target.hex == branch.upstream.target.hex

        if is_rebuild(self.workflow):
            self.log.info("Incrementing release label")
            self.bump(repo, branch)
        else:
            self.log.info("Verifying branch is at specified commit")
            self.verify_branch(branch)
