"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Combines the module information looked up by pre_resolve_module_compose,
combines it with additional information from container.yaml, and
generates a Dockerfile that will build a filesystem image for the module
at /var/tmp/flatpak-build.

Example configuration:
{
    'name': 'flatpak_create_dockerfile',
    'args': {'base_image': 'registry.fedoraproject.org/fedora:latest'}
}
"""

import os

from atomic_reactor.constants import DOCKERFILE_FILENAME, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_resolve_module_compose import get_compose_info
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.rpm_util import rpm_qf_args
from atomic_reactor.util import render_yum_repo, split_module_spec

DOCKERFILE_TEMPLATE = '''FROM {base_image}

LABEL name="{name}"
LABEL com.redhat.component="{name}"
LABEL version="{stream}"
LABEL release="{version}"

ADD atomic-reactor-includepkgs /tmp/
RUN cat /tmp/atomic-reactor-includepkgs >> /etc/dnf/dnf.conf && \\
    dnf -y --nogpgcheck \\
    --disablerepo=* \\
    --enablerepo=atomic-reactor-koji-plugin-* \\
    --enablerepo=atomic-reactor-module-* \\
    --installroot=/var/tmp/flatpak-build install {packages}
RUN rpm --root=/var/tmp/flatpak-build {rpm_qf_args} > /var/tmp/flatpak-build.rpm_qf
COPY cleanup.sh /var/tmp/flatpak-build/tmp/
RUN chroot /var/tmp/flatpak-build/ /bin/sh /tmp/cleanup.sh
'''


class FlatpakSourceInfo(object):
    def __init__(self, flatpak_yaml, compose):
        self.flatpak_yaml = flatpak_yaml
        self.compose = compose

        mmd = compose.base_module.mmd
        # A runtime module must have a 'runtime' profile, but can have other
        # profiles for SDKs, minimal runtimes, etc.
        self.runtime = 'runtime' in mmd.props.profiles

        module_spec = split_module_spec(compose.source_spec)
        if module_spec.profile:
            self.profile = module_spec.profile
        elif self.runtime:
            self.profile = 'runtime'
        else:
            self.profile = 'default'

        assert self.profile in mmd.props.profiles

    # The module for the Flatpak runtime that this app runs against
    @property
    def runtime_module(self):
        assert not self.runtime
        compose = self.compose

        dependencies = compose.base_module.mmd.props.dependencies
        # A built module should have its dependencies already expanded
        assert len(dependencies) == 1

        for key in dependencies[0].props.buildrequires.keys():
            try:
                module = compose.modules[key]
                if 'runtime' in module.mmd.props.profiles:
                    return module
            except KeyError:
                pass

        raise RuntimeError("Failed to identify runtime module in the buildrequires for {}"
                           .format(compose.base_module.name))

    # All modules that were build against the Flatpak runtime,
    # and thus were built with prefix=/app. This is primarily the app module
    # but might contain modules shared between multiple flatpaks as well.
    @property
    def app_modules(self):
        runtime_module_name = self.runtime_module.mmd.props.name

        def is_app_module(m):
            dependencies = m.mmd.props.dependencies
            return runtime_module_name in dependencies[0].props.buildrequires

        return [m for m in self.compose.modules.values() if is_app_module(m)]

    def koji_metadata(self):
        metadata = self.compose.koji_metadata()
        metadata['flatpak'] = True

        return metadata


WORKSPACE_SOURCE_KEY = 'source_info'


def get_flatpak_source_info(workflow):
    key = FlatpakCreateDockerfilePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_SOURCE_KEY, None)


def set_flatpak_source_info(workflow, source):
    key = FlatpakCreateDockerfilePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_SOURCE_KEY] = source


class FlatpakCreateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_create_dockerfile"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 base_image=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param base_image: host image used to install packages when creating the Flatpak
        """
        # call parent constructor
        super(FlatpakCreateDockerfilePlugin, self).__init__(tasker, workflow)

        self.base_image = base_image

    def _load_source(self):
        flatpak_yaml = self.workflow.source.config.flatpak

        compose_info = get_compose_info(self.workflow)
        if compose_info is None:
            raise RuntimeError(
                "resolve_module_compose must be run before flatpak_create_dockerfile")

        return FlatpakSourceInfo(flatpak_yaml, compose_info)

    def run(self):
        """
        run the plugin
        """

        source = self._load_source()

        set_flatpak_source_info(self.workflow, source)

        module_info = source.compose.base_module

        # For a runtime, certain information is duplicated between the container.yaml
        # and the modulemd, check that it matches
        if source.runtime:
            flatpak_yaml = source.flatpak_yaml
            flatpak_xmd = module_info.mmd.props.xmd['flatpak']

            def check(condition, what):
                if not condition:
                    raise RuntimeError(
                        "Mismatch for {} betweeen module xmd and container.yaml".format(what))

            check(flatpak_yaml['branch'] == flatpak_xmd['branch'], "'branch'")
            check(source.profile in flatpak_xmd['runtimes'], 'profile name')

            profile_xmd = flatpak_xmd['runtimes'][source.profile]

            check(flatpak_yaml['id'] == profile_xmd['id'], "'id'")
            check(flatpak_yaml.get('runtime', None) ==
                  profile_xmd.get('runtime', None), "'runtime'")
            check(flatpak_yaml.get('sdk', None) == profile_xmd.get('sdk', None), "'sdk'")

        # Create the dockerfile

        install_packages = module_info.mmd.props.profiles[source.profile].props.rpms.get()
        install_packages_str = ' '.join(install_packages)

        df_path = os.path.join(self.workflow.builder.df_dir, DOCKERFILE_FILENAME)
        with open(df_path, 'w') as fp:
            fp.write(DOCKERFILE_TEMPLATE.format(name=module_info.name,
                                                stream=module_info.stream,
                                                version=module_info.version,
                                                base_image=self.base_image,
                                                packages=install_packages_str,
                                                rpm_qf_args=rpm_qf_args()))

        self.workflow.builder.set_df_path(df_path)

        # For a runtime, we want to make sure that the set of RPMs that is installed
        # into the filesystem is *exactly* the set that is listed in the runtime
        # profile. Requiring the full listed set of RPMs to be listed makes it
        # easier to catch unintentional changes in the package list that might break
        # applications depending on the runtime. It also simplifies the checking we
        # do for application flatpaks, since we can simply look at the runtime
        # modulemd to find out what packages are present in the runtime.
        #
        # For an application, we want to make sure that each RPM that is installed
        # into the filesystem is *either* an RPM that is part of the 'runtime'
        # profile of the base runtime, or from a module that was built with
        # flatpak-rpm-macros in the install root and, thus, prefix=/app.
        #
        # We achieve this by restricting the set of available packages in the dnf
        # configuration to just the ones that we want.
        #
        # The advantage of doing this upfront, rather than just checking after the
        # fact is that this makes sure that when a application is being installed,
        # we don't get a different package to satisfy a dependency than the one
        # in the runtime - e.g. aajohan-comfortaa-fonts to satisfy font(:lang=en)
        # because it's alphabetically first.

        if not source.runtime:
            runtime_module = source.runtime_module
            runtime_profile = runtime_module.mmd.props.profiles['runtime']
            available_packages = sorted(runtime_profile.props.rpms.get())

            for m in source.app_modules:
                # Strip off the '.rpm' suffix from the filename to get something
                # that DNF can parse.
                available_packages.extend(x[:-4] for x in m.rpms)
        else:
            base_module = source.compose.base_module
            runtime_profile = base_module.mmd.props.profiles['runtime']
            available_packages = sorted(runtime_profile.props.rpms.get())

        includepkgs_path = os.path.join(self.workflow.builder.df_dir, 'atomic-reactor-includepkgs')
        with open(includepkgs_path, 'w') as f:
            f.write('includepkgs = ' + ','.join(available_packages) + '\n')

        # Create the cleanup script

        cleanupscript = os.path.join(self.workflow.builder.df_dir, "cleanup.sh")
        with open(cleanupscript, 'w') as f:
            cleanup_commands = source.flatpak_yaml.get('cleanup-commands')
            if cleanup_commands is not None:
                f.write(cleanup_commands.rstrip())
                f.write("\n")
        os.chmod(cleanupscript, 0o0755)

        # Add a yum-repository pointing to the compose

        repo_name = 'atomic-reactor-module-{name}-{stream}-{version}'.format(
            name=module_info.name,
            stream=module_info.stream,
            version=module_info.version)

        repo = {
            'name': repo_name,
            'baseurl': source.compose.repo_url,
            'enabled': 1,
            'gpgcheck': 0,
        }

        path = os.path.join(YUM_REPOS_DIR, repo_name + '.repo')
        self.workflow.files[path] = render_yum_repo(repo, escape_dollars=False)

        override_build_kwarg(self.workflow, 'module_compose_id', source.compose.compose_id)
