"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Takes a reference to a module, and looks up or triggers a compose in the on-demand compose
server (ODCS). In addition to retrieving the URL for a composed yum repository, the module
and all its dependencies are resolved to particular versions matching the ones that the
repository is built from.


Example configuration:
{
    'name': 'resolve_module_compose',
    'args': {'module_name': 'myapp',
             'module_stream': 'f26',
             'module_version': '20170629185228',
             'odcs_url': 'https://odcs.fedoraproject.org/odcs/1'},
             'pdc_url': 'https://pdc.fedoraproject.org/rest_api/v1',}
}
"""

import os
import re
from modulemd import ModuleMetadata
from pdc_client import PDCClient

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.odcs_util import ODCSClient


class ModuleInfo(object):
    def __init__(self, name, stream, version, mmd, rpms):
        self.name = name
        self.stream = stream
        self.version = version
        self.mmd = mmd
        self.rpms = rpms


class ComposeInfo(object):
    def __init__(self, source_spec, compose_id, base_module, modules, repo_url):
        self.source_spec = source_spec
        self.compose_id = compose_id
        self.base_module = base_module
        self.modules = modules
        self.repo_url = repo_url

    def koji_metadata(self):
        sorted_modules = [self.modules[k] for k in sorted(self.modules.keys())]

        return {
            'source_modules': [self.source_spec],
            'modules': ['-'.join((m.name, m.stream, m.version)) for
                        m in sorted_modules]
        }


WORKSPACE_SOURCE_KEY = 'compose_info'


def get_compose_info(workflow):
    key = ResolveModuleComposePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_SOURCE_KEY, None)


def set_compose_info(workflow, source):
    key = ResolveModuleComposePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_SOURCE_KEY] = source


class ResolveModuleComposePlugin(PreBuildPlugin):
    key = "resolve_module_compose"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 module_name, module_stream, module_version=None,
                 compose_id=None,
                 odcs_url=None, odcs_insecure=False,
                 odcs_openidc_secret_path=None,
                 pdc_url=None, pdc_insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param module_name: Module name to look up in PDC
        :param module_stream: Module stream to look up in PDC
        :param module_version: Module version to look up in PDC (optional)
        :param compose_id: ID of compose in ODCS (optional - will only be set for workers)
        :param odcs_url: URL of ODCS (On Demand Compose Service)
        :param odcs_insecure: If True, don't check SSL certificates for `odcs_url`
        :param odcs_openidc_secret_path: directory to look in for a `token` file (optional)
        :param pdc_url: URL of PDC (Product Definition center))
        :param pdc_insecure: If True, don't check SSL certificates for `pdc_url`
        :
        """
        # call parent constructor
        super(ResolveModuleComposePlugin, self).__init__(tasker, workflow)

        if not pdc_url:
            raise RuntimeError("pdc_url is required")
        if not odcs_url:
            raise RuntimeError("odcs_url is required")
        self.module_name = module_name
        self.module_stream = module_stream

        if module_version is not None and re.match(r'^\d{14}$', module_version) is None:
            raise RuntimeError("module_version should be 14 digits")
        self.module_version = module_version

        self.compose_id = compose_id
        self.odcs_url = odcs_url
        self.odcs_insecure = odcs_insecure
        self.odcs_openidc_secret_path = odcs_openidc_secret_path
        self.pdc_url = pdc_url
        self.pdc_insecure = pdc_insecure

    def _resolve_compose(self):
        if self.odcs_openidc_secret_path:
            token_path = os.path.join(self.odcs_openidc_secret_path, 'token')
            with open(token_path, "r") as f:
                odcs_token = f.read().strip()
        else:
            odcs_token = None

        odcs_client = ODCSClient(self.odcs_url, insecure=self.odcs_insecure, token=odcs_token)
        # The effect of develop=True is that requests to the PDC are made without authentication;
        # since we our interaction with the PDC is read-only, this is fine for our needs and
        # makes things simpler.
        pdc_client = PDCClient(server=self.pdc_url, ssl_verify=not self.pdc_insecure, develop=True)

        fmt = '{n}-{s}' if self.module_version is None else '{n}-{s}-{v}'
        source_spec = fmt.format(n=self.module_name, s=self.module_stream, v=self.module_version)

        if self.compose_id is None:
            self.compose_id = odcs_client.start_compose(source_type='module',
                                                        source=source_spec)['id']

        compose_info = odcs_client.wait_for_compose(self.compose_id)
        if compose_info['state_name'] != "done":
            raise RuntimeError("Compose cannot be retrieved, state='%s'" %
                               compose_info['state_name'])

        compose_source = compose_info['source']
        self.log.info("Resolved list of modules: %s", compose_source)

        resolved_modules = {}

        for module_spec in compose_source.strip().split():
            m = re.match(r'^(.*)-([^-]+)-(\d{14})$', module_spec)
            if not m:
                raise RuntimeError("Cannot parse resolved module in compose: %s" % module_spec)

            module_name = m.group(1)
            module_stream = m.group(2)
            module_version = m.group(3)

            query = {
                'variant_id': module_name,
                'variant_version': module_stream,
                'variant_release': module_version,
                'active': True,
            }

            self.log.info("Looking up module metadata for '%s' in the PDC", module_spec)
            retval = pdc_client['unreleasedvariants/'](page_size=-1,
                                                       fields=['modulemd', 'rpms'], **query)
            # Error handling
            if not retval:
                raise RuntimeError("Failed to find module in PDC %r" % query)
            if len(retval) != 1:
                raise RuntimeError("Multiple modules in the PDC matched %r" % query)

            mmd = ModuleMetadata()
            mmd.loads(retval[0]['modulemd'])
            rpms = set(retval[0]['rpms'])

            resolved_modules[module_name] = ModuleInfo(module_name, module_stream, module_version,
                                                       mmd, rpms)

        base_module = resolved_modules[self.module_name]
        assert base_module.stream == self.module_stream
        if self.module_version is not None:
            assert base_module.version == self.module_version

        return ComposeInfo(source_spec=source_spec,
                           compose_id=self.compose_id,
                           base_module=base_module,
                           modules=resolved_modules,
                           repo_url=compose_info['result_repo'] + '/$basearch/os/')

    def run(self):
        """
        run the plugin
        """

        self.log.info("Resolving module compose for name=%s, stream=%s, version=%s",
                      self.module_name, self.module_stream, self.module_version)

        compose_info = self._resolve_compose()
        set_compose_info(self.workflow, compose_info)
