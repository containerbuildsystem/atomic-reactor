"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which injects custom yum repositories in dockerfile.
"""
import os
import shutil
from collections import defaultdict
from io import StringIO
from urllib.parse import urlparse

from atomic_reactor.config import get_koji_session
from atomic_reactor.constants import YUM_REPOS_DIR, RELATIVE_REPOS_PATH, INSPECT_CONFIG, \
    PLUGIN_RESOLVE_COMPOSES_KEY
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import is_scratch_build, map_to_user_params, \
    allow_repo_dir_in_dockerignore, render_yum_repo, get_platforms
from atomic_reactor.utils.yum import YumRepo


class InjectYumReposPlugin(PreBuildPlugin):
    key = "inject_yum_repos"
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params(
        "target:koji_target",
    )

    def __init__(self, workflow, target=None, inject_proxy=None):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param target: string, koji target to use as a source
        :param inject_proxy: set proxy server for this repo
        """
        super().__init__(workflow)
        self.target = target

        self.repourls = {}
        self.inject_proxy = inject_proxy
        self.yum_repos = defaultdict(list)
        self.allowed_domains = self.workflow.conf.yum_repo_allowed_domains
        self.include_koji_repo = False
        self._builder_ca_bundle = None
        self._ca_bundle_pem = None
        self.platforms = get_platforms(workflow)

        resolve_comp_result = self.workflow.data.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        self.include_koji_repo = resolve_comp_result['include_koji_repo']
        self.repourls = resolve_comp_result['yum_repourls']

    def validate_yum_repo_files_url(self):
        if not self.allowed_domains:
            return
        errors = []

        checked = set()

        for platform in self.platforms:
            for repourl in self.repourls.get(platform, []):
                if repourl in checked:
                    continue
                repo_domain = urlparse(repourl).netloc
                checked.add(repourl)
                if repo_domain not in self.allowed_domains:
                    errors.append('Yum repo URL {} is not in list of allowed domains: {}'
                                  .format(repourl, self.allowed_domains))

        if errors:
            raise ValueError('Errors found while checking yum repo urls: \n{}'
                             .format('\n'.join(errors)))

    def _final_user_line(self):
        user = self._find_final_user()
        if user:
            return user

        if not self.workflow.data.dockerfile_images.base_from_scratch:
            # Inspect any platform: the User should be equal for all platforms
            inspect = self.workflow.imageutil.base_image_inspect()
            user = inspect.get(INSPECT_CONFIG, {}).get('User')
            if user:
                return f'USER {user}'

        return ''

    def _find_final_user(self):
        """Find the user in USER instruction in the last build stage"""
        dockerfile = self.workflow.build_dir.any_platform.dockerfile_with_parent_env(
            self.workflow.imageutil.base_image_inspect())
        for insndesc in reversed(dockerfile.structure):
            if insndesc['instruction'] == 'USER':
                return insndesc['content']  # we will reuse the line verbatim
            if insndesc['instruction'] == 'FROM':
                break  # no USER specified in final stage

    def _cleanup_lines(self, platform):
        lines = [
            "RUN rm -f " + " ".join(
                (f"'{repo.dst_filename}'" for repo in self.yum_repos[platform])
            )
        ]
        if self._builder_ca_bundle:
            lines.append(f'RUN rm -f /tmp/{self._ca_bundle_pem}')

        final_user_line = self._final_user_line()
        if final_user_line:
            lines.insert(0, "USER root")
            lines.append(final_user_line)

        return lines

    def add_koji_repo(self):
        xmlrpc = get_koji_session(self.workflow.conf)
        pathinfo = self.workflow.conf.koji_path_info
        proxy = self.workflow.conf.yum_proxy

        if not self.target:
            self.log.info('no target provided, not adding koji repo')
            return

        target_info = xmlrpc.getBuildTarget(self.target)
        if target_info is None:
            self.log.error("provided target '%s' doesn't exist", self.target)
            raise RuntimeError("Provided target '%s' doesn't exist!" % self.target)
        tag_info = xmlrpc.getTag(target_info['build_tag_name'])

        if not tag_info or 'name' not in tag_info:
            self.log.warning("No tag info was retrieved")
            return

        repo_info = xmlrpc.getRepo(tag_info['id'])

        if not repo_info or 'id' not in repo_info:
            self.log.warning("No repo info was retrieved")
            return

        # to use urljoin, we would have to append '/', so let's append everything
        baseurl = pathinfo.repo(repo_info['id'], tag_info['name']) + "/$basearch"

        self.log.info("baseurl = '%s'", baseurl)

        repo = {
            'name': 'atomic-reactor-koji-target-%s' % self.target,
            'baseurl': baseurl,
            'enabled': 1,
            'gpgcheck': 0,
        }

        # yum doesn't accept a certificate path in sslcacert - it requires a db with added cert
        # dnf ignores that option completely
        # we have to fall back to sslverify=0 everytime we get https repo from brew so we'll surely
        # be able to pull from it

        if baseurl.startswith("https://"):
            self.log.info("Ignoring certificates in the repo")
            repo['sslverify'] = 0

        if proxy:
            self.log.info("Setting yum proxy to %s", proxy)
            repo['proxy'] = proxy

        yum_repo = YumRepo(os.path.join(YUM_REPOS_DIR, self.target))
        path = yum_repo.dst_filename
        self.log.info("yum repo of koji target: '%s'", path)
        yum_repo.content = render_yum_repo(repo, escape_dollars=False)
        for platform in self.platforms:
            self.yum_repos[platform].append(yum_repo)

    def _inject_into_repo_files(self, build_dir: BuildDir):
        """Inject repo files into a relative directory inside the build context"""
        host_repos_path = build_dir.path / RELATIVE_REPOS_PATH
        self.log.info("creating directory for yum repos: %s", host_repos_path)
        os.mkdir(host_repos_path)
        allow_repo_dir_in_dockerignore(build_dir.path)

        for repo in self.yum_repos[build_dir.platform]:
            # Update every repo accordingly in a repofile
            # input_buf ---- updated ----> updated_buf
            with StringIO(repo.content.decode()) as input_buf, StringIO() as updated_buf:
                for line in input_buf:
                    updated_buf.write(line)
                    # Apply sslcacert to every repo in a repofile
                    if line.lstrip().startswith('[') and self._builder_ca_bundle:
                        updated_buf.write(f'sslcacert=/tmp/{self._ca_bundle_pem}\n')

                yum_repo = YumRepo(repourl=repo.dst_filename,
                                   content=updated_buf.getvalue(),
                                   dst_repos_dir=host_repos_path,
                                   add_hash=False)
                yum_repo.write_content()

    def _inject_into_dockerfile(self, build_dir: BuildDir):
        build_dir.dockerfile.add_lines(
            "ADD %s* %s" % (RELATIVE_REPOS_PATH, YUM_REPOS_DIR),
            all_stages=True, at_start=True, skip_scratch=True
        )

        if self._builder_ca_bundle:
            shutil.copyfile(
                self._builder_ca_bundle,
                build_dir.path / self._ca_bundle_pem
            )
            build_dir.dockerfile.add_lines(
                f'ADD {self._ca_bundle_pem} /tmp/{self._ca_bundle_pem}',
                all_stages=True, at_start=True, skip_scratch=True
            )

        if not self.workflow.data.dockerfile_images.base_from_scratch:
            build_dir.dockerfile.add_lines(*self._cleanup_lines(build_dir.platform))

    def run(self):
        """
        run the plugin
        """
        if not self.workflow.data.dockerfile_images:
            self.log.info("Skipping plugin, from scratch stage(s) can't add repos")
            return

        if self.include_koji_repo:
            self.add_koji_repo()
        else:
            self.log.info("'include_koji_repo parameter is set to '%s', not including koji repo",
                          self.include_koji_repo)

        if self.repourls and not is_scratch_build(self.workflow):
            self.validate_yum_repo_files_url()

        fetched_yum_repos = {}
        for platform in self.platforms:
            for repourl in self.repourls.get(platform, []):
                if repourl in fetched_yum_repos:
                    yum_repo = fetched_yum_repos[repourl]
                    self.yum_repos[platform].append(yum_repo)
                    continue
                yum_repo = YumRepo(repourl)
                self.log.info("fetching yum repo from '%s'", yum_repo.repourl)
                try:
                    yum_repo.fetch()
                except Exception as e:
                    msg = "Failed to fetch yum repo {repo}: {exc}".format(
                        repo=yum_repo.repourl, exc=e)
                    raise RuntimeError(msg) from e
                else:
                    self.log.info("fetched yum repo from '%s'", yum_repo.repourl)

                if self.inject_proxy:
                    if yum_repo.is_valid():
                        yum_repo.set_proxy_for_all_repos(self.inject_proxy)
                self.log.debug("saving yum repo '%s', length %d", yum_repo.dst_filename,
                               len(yum_repo.content))
                self.yum_repos[platform].append(yum_repo)
                fetched_yum_repos[repourl] = yum_repo

        if not self.yum_repos:
            return

        self._builder_ca_bundle = self.workflow.conf.builder_ca_bundle
        if self._builder_ca_bundle:
            self._ca_bundle_pem = os.path.basename(self._builder_ca_bundle)

        self.workflow.build_dir.for_each_platform(self._inject_into_repo_files)
        self.workflow.build_dir.for_each_platform(self._inject_into_dockerfile)

        for platform in self.platforms:
            for repo in self.yum_repos[platform]:
                self.log.info("injected yum repo: %s for '%s' platform", repo.dst_filename,
                              platform)
