"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from contextlib import contextmanager
from copy import deepcopy
import os
import re
import subprocess
from shutil import rmtree
from tempfile import mkdtemp

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.util import get_preferred_label_key
from dockerfile_parse import DockerfileParser


class GitRepo(object):
    """
    In an ideal world, pygit2 would be everywhere we'd need it to be,
    and we could just use that. Meanwhile, in this world, here's a
    wrapper around the git binary.
    """

    def __init__(self, workdir, log, cmd='/usr/bin/git'):
        self.workdir = workdir
        self.log = log
        self.cmd = cmd
        self.tmpdir = mkdtemp()
        self.git_wrapper = os.path.join(self.tmpdir, 'git-wrapper.sh')
        with open(self.git_wrapper, mode='wt') as gitfp:
            gitfp.write('#!/bin/sh\n'
                        'exec /usr/bin/ssh -o StrictHostKeyChecking=no "$@"\n')

        os.chmod(self.git_wrapper, 0o755)

    def __enter__(self):
        return self

    def __exit__(self, exc, value, tb):
        rmtree(self.tmpdir)

    def git(self, args):
        """
        Run git with arguments.

        :param args: list, argument strings
        :return: str, output (including stderr)
        """

        argv = [self.cmd] + args
        env = deepcopy(os.environ)
        env['GIT_SSH'] = self.git_wrapper
        self.log.debug("executing command: %r", argv)
        try:
            with open('/dev/null', 'r+') as devnull:
                output = subprocess.check_output(argv,
                                                 stdin=devnull,
                                                 stderr=subprocess.STDOUT,
                                                 cwd=self.workdir,
                                                 env=env)
        except subprocess.CalledProcessError as ex:
            self.log.debug("command failed, exit code %s", ex.returncode)
            self.log.debug("output: %r", ex.output)
            raise

        output = output.decode().rstrip()
        self.log.debug("git output: %r", output)
        return output


class BumpReleasePlugin(PreBuildPlugin):
    """Git branch management plugin

    For rebuilds, push a new commit incrementing the Release label.

    For initial builds, verify the branch is at the specified commit
    hash.

    When this plugin is configured by osbs-client, the Build's source
    git ref is actually the branch (from --git-branch), not the
    original SHA-1. The SHA-1 specified by --git-commit is stored in
    the configuration for this plugin.

    Some developers will want to specify the release as an ENV
    variable and reference it in the LABEL. This is supported for
    simple situations in which the $VAR reference is at the very
    beginning of the label. In this case, the ENV variable will be
    modified instead of the LABEL.

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
    is_allowed_to_fail = False  # We really want to stop the process

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
        self.committer_name = committer_name or author_name
        self.committer_email = committer_email or author_email
        self.push_url = push_url
        self.commit_message = (commit_message or
                               "Bumped release for automated rebuild")

    @contextmanager
    def no_env_replace(self, parser):
        """
        Context manager for temporarily disabling env_replace
        """
        old_val = parser.env_replace
        parser.env_replace = False
        try:
            yield
        finally:
            parser.env_replace = old_val

    def find_current_release(self, parser, label_key):
        """
        Find the attribute set and key name containing the current release.

        This is either the set of labels and the `label_key` we were
        given, or the set of environment variables and the name of the
        environment variable holding the value to change.

        :param parser: DockerfileParser instance
        :param label_key: str, name of release label
        :returns: 2-tuple, attribute set and key name
        """

        attrs = parser.labels
        key = label_key
        value_subst = attrs[key]

        with self.no_env_replace(parser):
            value_nosubst = parser.labels[key]

        # Is the real value stored in an environment variable?
        if value_subst != value_nosubst and value_nosubst.startswith('$'):
            # Yes, but which one?

            # Braced form: a dollar sign, an open brace, the group
            # we're interested in (letters numbers, underscores),
            # followed by a closing brace.
            braced_re = r"\$\{([A-Za-z0-9_]+)\}"

            # Unbraced form: a dollar sign, followed by the group
            # we're interested in (letters, numbers, underscores).
            unbraced_re = r"\$([A-Za-z0-9_]+)"

            match = None
            for pattern in [braced_re, unbraced_re]:
                match = re.match(pattern, value_nosubst)
                self.log.debug("Match %r against %r: %s",
                               value_nosubst, pattern, bool(match))
                if match:
                    break

            if match:
                attrs = parser.envs
                key = match.groups()[0]

        return attrs, key

    @staticmethod
    def get_next_release(current_release):
        """
        Calculate the incremented value.

        :param current_release: str, current value
        :return: str, incremented value
        """

        try:
            return str(int(current_release) + 1)
        except ValueError:
            isdigit = type(current_release).isdigit
            first_nondigit = [isdigit(x) for x in current_release].index(False)
            next_int = str(int(current_release[:first_nondigit]) + 1)
            return next_int + current_release[first_nondigit:]

    def bump(self, repo, remote):
        """
        Push a commit with an incremented release value.

        :param repo: str, GitRepo instance
        :param remote: str, git remote to push to
        """

        # Set up configuration
        repo.git(['config', 'push.default', 'simple'])
        repo.git(['config', 'user.email', self.committer_email])
        repo.git(['config', 'user.name', self.committer_name])
        if self.push_url:
            repo.git(['remote', 'set-url', '--push', remote, self.push_url])

        # Bump the Release label
        df_path = self.workflow.builder.df_path
        parser = DockerfileParser(df_path)
        label_key = get_preferred_label_key(parser.labels, 'release')

        attrs, key = self.find_current_release(parser, label_key)
        next_release = self.get_next_release(attrs[key])
        self.log.info("New Release: %s", next_release)
        attrs[key] = next_release  # this modifies the file

        # Stage it
        repo.git(['add', os.path.basename(df_path)])

        # Commit the change
        repo.git(['commit',
                  '--author={name} <{email}>'.format(name=self.author_name,
                                                     email=self.author_email),
                  '--message={message}'.format(message=self.commit_message)])

        # Push it
        self.log.info("Pushing to git repository")
        repo.git(['push', remote])

    def verify_branch(self, branch, branch_sha):
        """
        Raise exception if the branch is not at the correct commit.

        :param branch: str, branch name
        :param branch_sha: str, commit hash expected
        """

        if branch_sha != self.git_ref:
            self.log.error("Branch '%s' is at commit %s (expected %s)",
                           branch, branch_sha, self.git_ref)
            raise RuntimeError("Not at expected commit")

        self.log.info("Branch '%s' is at expected commit (%s)",
                      branch, self.git_ref)

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
        with GitRepo(source.get(), self.log) as repo:
            # Note: when this plugin is configured by osbs-client,
            # source.git_commit (the Build's source git ref) comes from
            # --git-branch not --git-commit. The value from --git-commit
            # went into our self.git_ref.
            branch = source.git_commit
            try:
                branch_sha = repo.git(['rev-parse', branch])
            except subprocess.CalledProcessError:
                self.log.error("Branch '%s' not found in git repo",
                               source.git_commit)
                raise RuntimeError("Branch '%s' not found" % branch)

            # We checked out the right branch
            assert repo.git(['rev-parse', 'HEAD']) == branch_sha

            # We haven't reset it to an earlier commit
            remote = repo.git(['config', '--get',
                               'branch.{branch}.remote'.format(branch=branch)])
            upstream = '{remote}/{branch}'.format(remote=remote, branch=branch)
            upstream_sha = repo.git(['rev-parse', upstream])
            assert branch_sha == upstream_sha

            if is_rebuild(self.workflow):
                self.log.info("Incrementing release label")
                self.bump(repo, remote)
            else:
                self.log.info("Verifying branch is at specified commit")
                self.verify_branch(branch, branch_sha)
