"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Takes a flatpak.json and a reference to a module, and outputs
a Dockerfile that will build a filesystem image for the module
at /var/tmp/flatpak-build.

Example configuration:
{
    'name': 'flatpak_create_dockerfile',
    'args': {'module_name': 'myapp',
             'module_stream': 'f26',
             'module_version': '20170629185228',
             'pdc_url': 'https://pdc.fedoraproject.org/rest_api/v1'}
}
"""

import json
import os
from pdc_client import PDCClient
from modulemd import ModuleMetadata

from atomic_reactor.constants import FLATPAK_FILENAME, DOCKERFILE_FILENAME, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import render_yum_repo

DOCKERFILE_TEMPLATE = '''FROM {base_image}

LABEL name="{name}"
LABEL com.redhat.component="{name}"
LABEL version="{stream}"
LABEL release="{version}"

RUN dnf -y --nogpgcheck --disablerepo=* --enablerepo=atomic-reactor-module-* \\
    --installroot=/var/tmp/flatpak-build install {packages}
COPY cleanup.sh /var/tmp/flatpak-build/tmp/
RUN chroot /var/tmp/flatpak-build/ /bin/sh /tmp/cleanup.sh
'''


class FlatpakSourceInfo(object):
    def __init__(self, flatpak_json, module_name, module_stream, module_version, mmd):
        self.flatpak_json = flatpak_json
        self.module_name = module_name
        self.module_stream = module_stream
        self.module_version = module_version
        self.mmd = mmd

        self.runtime = 'runtime' in mmd.profiles


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
                 module_name, module_stream, module_version=None,
                 base_image=None,
                 pdc_url=None, pdc_insecure=False,
                 compose_url=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param module_name: Module name to look up in PDC
        :param module_stream: Module stream to look up in PDC
        :param module_version: Module version to look up in PDC (optional)
        :param base_image: host image used to install packages when creating the Flatpak
        :param pdc_url: URL of PDC
        """
        # call parent constructor
        super(FlatpakCreateDockerfilePlugin, self).__init__(tasker, workflow)

        if not pdc_url:
            raise RuntimeError("pdc_url is required")
        if not base_image:
            raise RuntimeError("base_image is required")
        if not compose_url:
            raise RuntimeError("compose_url is currently required")
        self.module_name = module_name
        self.module_stream = module_stream
        self.module_version = module_version
        self.base_image = base_image
        self.pdc_url = pdc_url
        self.pdc_insecure = pdc_insecure
        self.compose_url = compose_url

    def _load_source(self):
        flatpak_path = os.path.join(self.workflow.builder.df_dir, FLATPAK_FILENAME)
        with open(flatpak_path, 'r') as fp:
            flatpak_json = json.load(fp)

        pdc_client = PDCClient(server=self.pdc_url, ssl_verify=not self.pdc_insecure, develop=True)

        query = {
            'variant_id': self.module_name,
            'variant_version': self.module_stream,
            'active': True,
        }

        if self.module_version:
            query['variant_release'] = self.module_version
        else:
            # Ordering doesn't work
            # https://github.com/product-definition-center/product-definition-center/issues/439,
            # so if a release isn't specified, we have to get all builds and sort ourselves.
            # We do this two-step to avoid downloading modulemd for all builds.
            retval = pdc_client['unreleasedvariants/'](page_size=-1,
                                                       fields=['variant_release'], **query)
            if not retval:
                raise RuntimeError("Failed to find module in PDC %r" % query)
            self.module_version = str(max({int(d['variant_release']) for d in retval}))
            self.log.info("Using most-recent module version {}".format(self.module_version))
            query['variant_release'] = self.module_version

        retval = pdc_client['unreleasedvariants/'](page_size=-1,
                                                   fields=['modulemd'], **query)
        # Error handling
        if not retval:
            raise RuntimeError("Failed to find module in PDC %r" % query)

        assert len(retval) == 1

        mmd = ModuleMetadata()
        mmd.loads(retval[0]['modulemd'])

        return FlatpakSourceInfo(flatpak_json,
                                 self.module_name, self.module_stream, self.module_version,
                                 mmd)

    def run(self):
        """
        run the plugin
        """

        source = self._load_source()

        set_flatpak_source_info(self.workflow, source)

        # Create the dockerfile

        if source.runtime:
            profile = 'runtime'
        else:
            profile = 'default'

        packages = ' '.join(source.mmd.profiles[profile].rpms)

        df_path = os.path.join(self.workflow.builder.df_dir, DOCKERFILE_FILENAME)
        with open(df_path, 'w') as fp:
            fp.write(DOCKERFILE_TEMPLATE.format(name=self.module_name,
                                                stream=self.module_stream,
                                                version=self.module_version,
                                                base_image=self.base_image,
                                                packages=packages))

        self.workflow.builder.set_df_path(df_path)

        # Create the cleanup script

        cleanupscript = os.path.join(self.workflow.builder.df_dir, "cleanup.sh")
        with open(cleanupscript, 'w') as f:
            for line in source.flatpak_json.get('cleanup-commands', []):
                f.write(line)
                f.write("\n")
        os.chmod(cleanupscript, 0o0755)

        # Add a yum-repository pointing to the compose

        compose_url = self.compose_url
        replacements = {
            "name": source.module_name,
            "stream": source.module_stream,
            "version": source.module_version,
        }

        for k, v in replacements.items():
            compose_url = compose_url.replace('{' + k + '}', v)

        repo_name = 'atomic-reactor-module-{name}-{stream}-{version}'.format(**replacements)

        repo = {
            'name': repo_name,
            'baseurl': compose_url,
            'enabled': 1,
            'gpgcheck': 0,
        }

        path = os.path.join(YUM_REPOS_DIR, repo_name + '.repo')
        self.workflow.files[path] = render_yum_repo(repo, escape_dollars=False)
