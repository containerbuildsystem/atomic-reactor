"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which injects custom yum repository in dockerfile.
"""
import os
import shutil
from io import StringIO

from atomic_reactor.constants import YUM_REPOS_DIR, RELATIVE_REPOS_PATH, INSPECT_CONFIG
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_builder_ca_bundle
from atomic_reactor.util import df_parser
from atomic_reactor.utils.yum import YumRepo


class InjectYumRepoPlugin(PreBuildPlugin):
    key = "inject_yum_repo"
    is_allowed_to_fail = False

    def _final_user_line(self):
        user = self._find_final_user()
        if user:
            return user

        builder = self.workflow.builder
        if not builder.dockerfile_images.base_from_scratch:
            inspect = builder.base_image_inspect
            user = inspect.get(INSPECT_CONFIG).get('User')
            if user:
                return f'USER {user}'

        return ''

    def _find_final_user(self):
        """Find the user in USER instruction in the last build stage"""
        for insndesc in reversed(self._dockerfile.structure):
            if insndesc['instruction'] == 'USER':
                return insndesc['content']  # we will reuse the line verbatim
            if insndesc['instruction'] == 'FROM':
                break  # no USER specified in final stage

    def _cleanup_lines(self):
        lines = [
            "RUN rm -f " + " ".join(
                (f"'{repo_file}'" for repo_file in self.workflow.files)
            )
        ]
        if self._builder_ca_bundle:
            lines.append(f'RUN rm -f /tmp/{self._ca_bundle_pem}')

        final_user_line = self._final_user_line()
        if final_user_line:
            lines.insert(0, "USER root")
            lines.append(final_user_line)

        return lines

    def __init__(self, tasker, workflow, *args, **kwargs):
        super().__init__(tasker, workflow, *args, **kwargs)
        self._builder_ca_bundle = None
        self._ca_bundle_pem = None
        self._dockerfile = None

    def _inject_into_repo_files(self):
        """Inject repo files into a relative directory inside the build context"""
        host_repos_path = os.path.join(self.workflow.builder.df_dir, RELATIVE_REPOS_PATH)
        self.log.info("creating directory for yum repos: %s", host_repos_path)
        os.mkdir(host_repos_path)

        for repo_filename, repo_content in self.workflow.files.items():
            # Update every repo accordingly in a repofile
            # input_buf ---- updated ----> updated_buf
            with StringIO(repo_content) as input_buf, StringIO() as updated_buf:
                for line in input_buf:
                    updated_buf.write(line)
                    # Apply sslcacert to every repo in a repofile
                    if line.lstrip().startswith('[') and self._builder_ca_bundle:
                        updated_buf.write(f'sslcacert=/tmp/{self._ca_bundle_pem}\n')

                yum_repo = YumRepo(repourl=repo_filename,
                                   content=updated_buf.getvalue(),
                                   dst_repos_dir=host_repos_path,
                                   add_hash=False)
                yum_repo.write_content()

    def _inject_into_dockerfile(self):
        self._dockerfile.add_lines(
            "ADD %s* %s" % (RELATIVE_REPOS_PATH, YUM_REPOS_DIR),
            all_stages=True, at_start=True, skip_scratch=True
        )

        if self._builder_ca_bundle:
            shutil.copyfile(
                self._builder_ca_bundle,
                os.path.join(self.workflow.builder.df_dir, self._ca_bundle_pem)
            )
            self._dockerfile.add_lines(
                f'ADD {self._ca_bundle_pem} /tmp/{self._ca_bundle_pem}',
                all_stages=True, at_start=True, skip_scratch=True
            )

        if not self.workflow.builder.dockerfile_images.base_from_scratch:
            self._dockerfile.add_lines(*self._cleanup_lines())

    def run(self):
        """
        run the plugin
        """
        yum_repos = {k: v for k, v in self.workflow.files.items() if k.startswith(YUM_REPOS_DIR)}
        if not yum_repos:
            return

        self._dockerfile = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        if self._dockerfile.baseimage is None:
            raise RuntimeError("No FROM line in Dockerfile")

        self._builder_ca_bundle = get_builder_ca_bundle(self.workflow, None)
        if self._builder_ca_bundle:
            self._ca_bundle_pem = os.path.basename(self._builder_ca_bundle)

        self._inject_into_repo_files()
        self._inject_into_dockerfile()

        for repo in self.workflow.files:
            self.log.info("injected yum repo: %s", repo)
