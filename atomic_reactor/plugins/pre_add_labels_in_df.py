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
from atomic_reactor.util import get_docker_architecture, df_parser
from osbs.utils import Labels
import json
import datetime
import string


class AddLabelsPlugin(PreBuildPlugin):
    key = "add_labels_in_dockerfile"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, labels,
                 dont_overwrite=("Architecture", "architecture"),
                 auto_labels=("build-date",
                              "architecture",
                              "vcs-type",
                              "vcs-ref",
                              "com.redhat.build-host"),
                 aliases=None,
                 dont_overwrite_if_in_dockerfile=("distribution-scope",),
                 info_url_format=None,
                 equal_labels=None):
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
        :param info_url_format : string, format for url dockerfile label
        :param equal_labels: list, with equal labels groups as lists
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
        self.aliases = aliases or Labels.get_new_names_by_old()
        self.info_url_format = info_url_format
        self.equal_labels = equal_labels or []
        if not isinstance(self.equal_labels, list):
            raise RuntimeError("equal_labels have to be list")

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
                        # set old label with value from new label if old was in base
                        add_as_an_alias(new, old, is_old_inherited)
                        continue
                    self.log.warning("labels %r=%r and %r=%r should probably have same value",
                                     old, all_labels[old], new, all_labels[new])

                self.log.debug("alias label %r for %r already exists, skipping", new, old)
                continue

            # new label is in base or doesn't exists and we have somewhere old label
            add_as_an_alias(new, old, is_old_inherited)
            applied_alias = True
            self.log.info(self.labels)

        # warn if we applied only some aliases
        if applied_alias and not_applied:
            self.log.debug("applied only some aliases, following old labels were not found: %s",
                           ", ".join(not_applied))

        def check_if_all_same(labels_to_check, source):
            """
            checks if all specified equal labels have same values
            within same scope (base or new_labels)
            """
            if labels_to_check:
                list_to_check = list(labels_to_check)

                for equal_name in list_to_check[1:]:
                    if source[list_to_check[0]] != source[equal_name]:
                        return False
            return True

        def set_missing_labels(labels_set, all_labels, value_from, not_in=(), not_value=None):
            labels_to_set = all_labels.difference(labels_set)
            list_labels_set = list(labels_set)

            for set_label in labels_to_set:
                if set_label in not_in and value_from[list_labels_set[0]] == not_value[set_label]:
                    self.log.debug("skipping label %r because it is set correctly in base image",
                                   set_label)
                else:
                    self.labels[set_label] = value_from[list_labels_set[0]]
                    self.log.warning("adding equal label %r with value %r",
                                     set_label, value_from[list_labels_set[0]])

        fail_build = False
        for equal_list in self.equal_labels:
            all_equal = set(equal_list)
            found_labels_base = set()
            found_labels_new = set()
            for equal_label in equal_list:
                if equal_label in new_labels:
                    found_labels_new.add(equal_label)
                elif equal_label in base_labels:
                    found_labels_base.add(equal_label)

            if found_labels_new:
                if not check_if_all_same(found_labels_new, new_labels):
                    self.log.error("labels in dockerfile don't have same values %s", equal_list)
                    fail_build = True
                    continue

                if not fail_build:
                    set_missing_labels(found_labels_new, all_equal, new_labels,
                                       found_labels_base, base_labels)

            elif found_labels_base:
                if not check_if_all_same(found_labels_base, base_labels):
                    self.log.error("labels in parent don't have same values %s", equal_list)
                    fail_build = True
                    continue

                if not fail_build:
                    set_missing_labels(found_labels_base, all_equal, base_labels)

        if fail_build:
            raise RuntimeError("equal labels have different values")

    def add_info_url(self, base_labels, df_labels, plugin_labels):
        all_labels = base_labels.copy()
        all_labels.update(df_labels)
        all_labels.update(plugin_labels)

        class MyFormatter(string.Formatter):
            """
            using this because str.format can't handle keys with dots and dashes
            which are included in some of the labels, such as
            'authoritative-source-url', 'com.redhat.component', etc
            """
            def get_field(self, field_name, args, kwargs):
                return (self.get_value(field_name, args, kwargs), field_name)

        info_url = MyFormatter().vformat(self.info_url_format, [], all_labels)
        self.labels['url'] = info_url

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
        if self.info_url_format:
            self.add_info_url(base_image_labels.copy(), dockerfile.labels.copy(), self.labels.copy())

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
