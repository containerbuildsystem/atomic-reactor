"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which adds labels to dockerfile. Labels have to be specified either
as a dict:

{
    "name": "add_labels_in_dockerfile",
    "args": {
        "labels": {
            "label1": "value1",
            "label 2": "some value"
        }
    }
}

Or as a string, which must be a dict serialised as JSON.

this will add turn this dockerfile:

```dockerfile
FROM fedora
CMD date
```

into this:

```dockerfile
FROM fedora
LABEL "label1"="value1" "label 2"="some value"
CMD date
```


By default there is parameter:
    dont_overwrite=("Architecture", "architecture")
which disallows to overwrite labels in the list if they are in parent image.

After that is also another check via parameter :
    dont_overwrite_if_in_dockerfile=("distribution-scope",)
which disallows to overwrite labels in the list if they are in dockerfile

Keys and values are quoted as necessary.
"""

from __future__ import unicode_literals

from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.util import get_docker_architecture, _PREFERRED_LABELS, df_parser
import json
import datetime


class AddLabelsPlugin(PreBuildPlugin):
    key = "add_labels_in_dockerfile"

    DEFAULT_ALIASES = {
        'Name': 'name',
        'Version': 'version',
        'Release': 'release',
        'Architecture': 'architecture',
        'Vendor': 'vendor',
        'RUN': 'run',
        'INSTALL': 'install',
        'UNINSTALL': 'uninstall',
        'Authoritative_Registry': 'authoritative-source-url',
        'BZComponent': 'com.redhat.component',
        'Build_Host': 'com.redhat.build-host',
    }

    def __init__(self, tasker, workflow, labels,
                 dont_overwrite=("Architecture", "architecture"),
                 auto_labels=("build-date",
                              "architecture",
                              "vcs-type",
                              "vcs-ref",
                              "com.redhat.build-host"),
                 aliases=None,
                 dont_overwrite_if_in_dockerfile=("distribution-scope",)):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param labels: dict, key value pairs to set as labels; or str, JSON-encoded dict
        :param dont_overwrite: iterable, list of label keys which should not be overwritten
                               if they are present in parent image
        :param auto_labels: iterable, list of labels to be determined automatically, if supported
        :param aliases: dict, maps old label names to new label names - for each old name found in
                        base image, dockerfile, or labels argument, a label with the new name is
                        added (with the same value)
        :param dont_overwrite_if_in_dockerfile : iterable, list of label keys which should not be
                                                 overwritten if they are present in dockerfile
        """
        # call parent constructor
        super(AddLabelsPlugin, self).__init__(tasker, workflow)
        if isinstance(labels, str):
            labels = json.loads(labels)
        if not isinstance(labels, dict):
            raise RuntimeError("labels have to be dict")
        self.labels = labels
        self.dont_overwrite = dont_overwrite
        self.dont_overwrite_if_in_dockerfile = dont_overwrite_if_in_dockerfile
        self.aliases = aliases or self.DEFAULT_ALIASES

        self.generate_auto_labels(auto_labels)

    def generate_auto_labels(self, auto_labels):
        generated = {}

        # build date
        dt = datetime.datetime.fromtimestamp(atomic_reactor_start_time)
        generated['build-date'] = dt.isoformat()

        # architecture - assuming host and image architecture is the same
        generated['architecture'], _ = get_docker_architecture(self.tasker)

        # build host
        docker_info = self.tasker.get_info()
        generated['com.redhat.build-host'] = docker_info['Name']

        # VCS info
        vcs = self.workflow.source.get_vcs_info()
        if vcs:
            generated['vcs-type'] = vcs.vcs_type
            generated['vcs-url'] = vcs.vcs_url
            generated['vcs-ref'] = vcs.vcs_ref

        for old, new in self.aliases.items():
            self.log.info("old=%r new=%r", old, new)
            if new in generated and old not in generated:
                self.log.info("adding %r for compatibility", old)
                generated[old] = generated[new]

        for lbl in auto_labels:
            if lbl in self.labels:
                self.log.info("label %r is set explicitly, not using generated value", lbl)
                continue

            if lbl in generated:
                self.labels[lbl] = generated[lbl]
            else:
                self.log.warning("requested automatic label %r is not available", lbl)

    def add_aliases(self, base_labels, df_labels, plugin_labels):
        all_labels = base_labels.copy()
        all_labels.update(df_labels)
        all_labels.update(plugin_labels)

        new_labels = df_labels.copy()
        new_labels.update(plugin_labels)

        applied_alias = False
        not_applied = []

        def add_as_an_alias(new, old, is_old_inherited):
            self.log.warning("adding label %r as an alias for label %r", new, old)
            if is_old_inherited and new in all_labels:
                self.labels[old] = all_labels[new]
            else:
                self.labels[new] = all_labels[old]

        for old, new in self.aliases.items():
            if old not in all_labels:
                not_applied.append(old)
                continue

            is_old_inherited = old in base_labels and \
                old not in df_labels and \
                old not in plugin_labels

            if new in new_labels:
                if all_labels[old] != all_labels[new]:
                    if is_old_inherited:
                        add_as_an_alias(new, old, is_old_inherited)
                        continue
                    self.log.warning("labels %r=%r and %r=%r should probably have same value",
                                     old, all_labels[old], new, all_labels[new])

                self.log.debug("alias label %r for %r already exists, skipping", new, old)
                continue

            add_as_an_alias(new, old, is_old_inherited)
            applied_alias = True
            self.log.info(self.labels)

        # warn if we applied only some aliases
        if applied_alias and not_applied:
            self.log.debug("applied only some aliases, following old labels were not found: %s",
                           ", ".join(not_applied))

    def run(self):
        """
        run the plugin
        """
        try:
            config = self.workflow.base_image_inspect[INSPECT_CONFIG]
        except (AttributeError, TypeError):
            message = "base image was not inspected"
            self.log.error(message)
            raise RuntimeError(message)
        else:
            base_image_labels = config["Labels"] or {}

        dockerfile = df_parser(self.workflow.builder.df_path, workflow=self.workflow)

        lines = dockerfile.lines

        # changing dockerfile.labels writes out modified Dockerfile - err on
        # the safe side and make a copy
        self.add_aliases(base_image_labels.copy(), dockerfile.labels.copy(), self.labels.copy())

        # correct syntax is:
        #   LABEL "key"="value" "key2"="value2"

        # Make sure to escape '\' and '"' characters.
        try:
            # py3
            env_trans = str.maketrans({'\\': '\\\\',
                                       '"': '\\"'})
        except AttributeError:
            # py2
            env_trans = None

        def escape(s):
            if env_trans:
                return s.translate(env_trans)
            return s.replace('\\', '\\\\').replace('"', '\\"')

        labels = []
        for key, value in self.labels.items():
            try:
                base_image_value = base_image_labels[key]
            except KeyError:
                self.log.info("label %r not present in base image", key)
            else:
                if base_image_value == value:
                    self.log.info("label %r is already set to %r", key, value)
                    continue
                else:
                    self.log.info("base image has label %r set to %r", key, base_image_value)
                    if key in self.dont_overwrite:
                        self.log.info("denying overwrite of label %r", key)
                        continue

            if (key in self.dont_overwrite_if_in_dockerfile) and (key in dockerfile.labels):
                self.log.info("denying overwrite of label %r, using from Dockerfile", key)
                continue

            label = '"%s"="%s"' % (escape(key), escape(value))
            self.log.info("setting label %r", label)
            labels.append(label)

        content = ""
        if labels:
            content = 'LABEL ' + " ".join(labels)
            # put labels at the end of dockerfile (since they change metadata and do not interact
            # with FS, this should cause no harm)
            lines.append('\n' + content + '\n')
            dockerfile.lines = lines

        return content
