"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import copy

import pytest
from ruamel.yaml import YAML

from atomic_reactor.util import ImageName, chain_get
from atomic_reactor.operator_util import OperatorCSV, OperatorManifest, NotOperatorCSV


yaml = YAML()


class PullSpec(object):
    def __init__(self, name, value, replace, path):
        self._name = name
        self._value = ImageName.parse(value)
        self._replace = ImageName.parse(replace)
        self._path = path

    @property
    def name(self):
        return self._name

    @property
    def value(self):
        return self._value

    @property
    def replace(self):
        return self._replace

    @property
    def path(self):
        return tuple(self._path)

    def __str__(self):
        return str(self.value)

    def find_in_data(self, data):
        return ImageName.parse(chain_get(data, self.path))


FOO = PullSpec(
    "foo", "foo:1", "r-foo:2",
    ["spec", "relatedImages", 0, "image"]
)
BAR = PullSpec(
    "bar", "registry/bar:1", "r-registry/r-bar:2",
    ["spec", "relatedImages", 1, "image"]
)
SPAM = PullSpec(
    "spam", "registry/namespace/spam:1", "r-registry/r-namespace/r-spam:2",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "spec", "containers", 0, "image"]
)
EGGS = PullSpec(
    "eggs", "eggs:1", "r-eggs:2",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "spec", "containers", 0, "env", 0, "value"]
)
HAM = PullSpec(
    "ham", "ham:1", "r-ham:2",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "spec", "containers", 1, "image"]
)
JAM = PullSpec(
    "jam", "jam:1", "r-jam:2",
    ["spec", "install", "spec", "deployments", 1,
     "spec", "template", "spec", "containers", 0, "image"]
)

PULLSPECS = {p.name: p for p in [FOO, BAR, SPAM, EGGS, HAM, JAM]}


ORIGINAL_CONTENT = """\
# A meaningful comment
kind: ClusterServiceVersion
spec:
  relatedImages:
  - name: foo
    image: {foo}
  - name: bar
    image: {bar}
  install:
    spec:
      deployments:
      - spec:
          template:
            spec:
              containers:
              - name: spam
                image: {spam}
                env:
                - name: RELATED_IMAGE_EGGS
                  value: {eggs}
                - name: UNRELATED_IMAGE
                  value: {eggs}
              - name: ham
                image: {ham}
      - spec:
          template:
            spec:
              containers:
              - name: jam
                image: {jam}
random:
  nested:
    dict:
      a: {foo}
      b: {bar}
      c: {spam}
      d: {eggs}
      e: {ham}
      f: {jam}
    list:
    - {foo}
    - {bar}
    - {spam}
    - {eggs}
    - {ham}
    - {jam}
""".format(**PULLSPECS)

REPLACED_CONTENT = """\
# A meaningful comment
kind: ClusterServiceVersion
spec:
  relatedImages:
  - name: foo
    image: {foo.replace}
  - name: bar
    image: {bar.replace}
  install:
    spec:
      deployments:
      - spec:
          template:
            spec:
              containers:
              - name: spam
                image: {spam.replace}
                env:
                - name: RELATED_IMAGE_EGGS
                  value: {eggs.replace}
                - name: UNRELATED_IMAGE
                  value: {eggs}
              - name: ham
                image: {ham.replace}
      - spec:
          template:
            spec:
              containers:
              - name: jam
                image: {jam.replace}
random:
  nested:
    dict:
      a: {foo}
      b: {bar}
      c: {spam}
      d: {eggs}
      e: {ham}
      f: {jam}
    list:
    - {foo}
    - {bar}
    - {spam}
    - {eggs}
    - {ham}
    - {jam}
""".format(**PULLSPECS)

REPLACED_EVERYWHERE_CONTENT = """\
# A meaningful comment
kind: ClusterServiceVersion
spec:
  relatedImages:
  - name: foo
    image: {foo.replace}
  - name: bar
    image: {bar.replace}
  install:
    spec:
      deployments:
      - spec:
          template:
            spec:
              containers:
              - name: spam
                image: {spam.replace}
                env:
                - name: RELATED_IMAGE_EGGS
                  value: {eggs.replace}
                - name: UNRELATED_IMAGE
                  value: {eggs.replace}
              - name: ham
                image: {ham.replace}
      - spec:
          template:
            spec:
              containers:
              - name: jam
                image: {jam.replace}
random:
  nested:
    dict:
      a: {foo.replace}
      b: {bar.replace}
      c: {spam.replace}
      d: {eggs.replace}
      e: {ham.replace}
      f: {jam.replace}
    list:
    - {foo.replace}
    - {bar.replace}
    - {spam.replace}
    - {eggs.replace}
    - {ham.replace}
    - {jam.replace}
""".format(**PULLSPECS)


class CSVFile(object):
    def __init__(self, content):
        self.content = content
        self._data = yaml.load(content)

    @property
    def data(self):
        return copy.deepcopy(self._data)


ORIGINAL = CSVFile(ORIGINAL_CONTENT)
REPLACED = CSVFile(REPLACED_CONTENT)
REPLACED_EVERYWHERE = CSVFile(REPLACED_EVERYWHERE_CONTENT)


class TestOperatorCSV(object):
    _original_pullspecs = {p.value for p in PULLSPECS.values()}
    _replacement_pullspecs = {p.value: p.replace for p in PULLSPECS.values()}

    def test_wrong_kind(self):
        data = ORIGINAL.data

        del data["kind"]
        with pytest.raises(NotOperatorCSV) as exc_info:
            OperatorCSV("original.yaml", data)
        assert str(exc_info.value) == "Not a ClusterServiceVersion"

        data["kind"] = "ClusterResourceDefinition"
        with pytest.raises(NotOperatorCSV) as exc_info:
            OperatorCSV("original.yaml", data)
        assert str(exc_info.value) == "Not a ClusterServiceVersion"

    def test_from_file(self, tmpdir):
        path = tmpdir.join("original.yaml")
        path.write(ORIGINAL.content)

        csv = OperatorCSV.from_file(str(path))
        assert csv.path == str(path)
        assert csv.data == ORIGINAL.data

    def test_get_pullspecs(self, caplog):
        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        pullspecs = csv.get_pullspecs()
        assert pullspecs == self._original_pullspecs

        expected_logs = [
            "original.yaml - Found pullspec for related image foo: {foo}",
            "original.yaml - Found pullspec for related image bar: {bar}",
            "original.yaml - Found pullspec in RELATED_IMAGE_EGGS var: {eggs}",
            "original.yaml - Found pullspec for container spam: {spam}",
            "original.yaml - Found pullspec for container ham: {ham}",
            "original.yaml - Found pullspec for container jam: {jam}"
        ]
        for log in expected_logs:
            assert log.format(**PULLSPECS) in caplog.text

    def test_replace_pullspecs(self, caplog):
        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        csv.replace_pullspecs(self._replacement_pullspecs)
        assert csv.data == REPLACED.data

        expected_logs = [
            "{file} - Replaced pullspec for related image foo: {foo} -> {foo.replace}",
            "{file} - Replaced pullspec for related image bar: {bar} -> {bar.replace}",
            "{file} - Replaced pullspec in RELATED_IMAGE_EGGS var: {eggs} -> {eggs.replace}",
            "{file} - Replaced pullspec for container spam: {spam} -> {spam.replace}",
            "{file} - Replaced pullspec for container ham: {ham} -> {ham.replace}",
            "{file} - Replaced pullspec for container jam: {jam} -> {jam.replace}"
        ]
        for log in expected_logs:
            assert log.format(file="original.yaml", **PULLSPECS) in caplog.text

    def test_replace_pullspecs_everywhere(self, caplog):
        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        csv.replace_pullspecs_everywhere(self._replacement_pullspecs)
        assert csv.data == REPLACED_EVERYWHERE.data

        expected_logs = {
            "original.yaml - Replaced pullspec: {foo} -> {foo.replace}": 3,
            "original.yaml - Replaced pullspec: {bar} -> {bar.replace}": 3,
            "original.yaml - Replaced pullspec: {eggs} -> {eggs.replace}": 4,
            "original.yaml - Replaced pullspec: {spam} -> {spam.replace}": 3,
            "original.yaml - Replaced pullspec: {ham} -> {ham.replace}": 3,
            "original.yaml - Replaced pullspec: {jam} -> {jam.replace}": 3
        }
        for log, count in expected_logs.items():
            assert caplog.text.count(log.format(**PULLSPECS)) == count

    def test_dump(self, tmpdir):
        path = tmpdir.join("original.yaml")
        csv = OperatorCSV(str(path), ORIGINAL.data)
        csv.dump()

        content = path.read()
        # Formatting does not necessarily have to match, at least check the data...
        assert yaml.load(content) == csv.data
        # ...and that the comment was preserved
        assert content.startswith('# A meaningful comment')

    def test_replace_only_some_pullspecs(self, caplog):
        replacement_pullspecs = self._replacement_pullspecs.copy()

        # Foo won't be replaced because replacement is identical
        replacement_pullspecs[FOO.value] = FOO.value
        # Bar won't be replaced because no replacement available
        del replacement_pullspecs[BAR.value]

        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        csv.replace_pullspecs(replacement_pullspecs)

        assert FOO.find_in_data(csv.data) == FOO.value
        assert BAR.find_in_data(csv.data) == BAR.value

        foo_log = "original.yaml - Replaced pullspec for related image foo: {foo}"
        bar_log = "original.yaml - Replaced pullspec for related image bar: {bar}"

        assert foo_log.format(foo=FOO) not in caplog.text
        assert bar_log.format(bar=BAR) not in caplog.text

    @pytest.mark.parametrize("rel_images, rel_envs, containers, expected", [
        (False, False, False, set()),
        (True, False, False, {FOO.value, BAR.value}),
        # (False, True, False) - Cannot have envs without containers
        (False, False, True, {SPAM.value, HAM.value, JAM.value}),
        # (True, True, False) - Cannot have envs without containers
        (True, False, True, {FOO.value, BAR.value, SPAM.value, HAM.value, JAM.value}),
        (False, True, True, {SPAM.value, EGGS.value, HAM.value, JAM.value}),
    ])
    def test_get_pullspecs_some_locations(self, rel_images, rel_envs, containers, expected):
        data = ORIGINAL.data
        if not rel_images:
            del data["spec"]["relatedImages"]
        deployments = chain_get(data, ["spec", "install", "spec", "deployments"])
        if not rel_envs:
            for d in deployments:
                for c in chain_get(d, ["spec", "template", "spec", "containers"]):
                    c.pop("env", None)
        if not containers:
            for d in deployments:
                del d["spec"]["template"]["spec"]["containers"]

        csv = OperatorCSV("x.yaml", data)
        assert csv.get_pullspecs() == expected

    def test_valuefrom_references_not_allowed(self):
        data = ORIGINAL.data
        env_path = EGGS.path[:-1]
        env = chain_get(data, env_path)
        env["valueFrom"] = "somewhere"

        csv = OperatorCSV("original.yaml", data)
        with pytest.raises(RuntimeError) as exc_info:
            csv.get_pullspecs()

        assert '"valueFrom" references are not supported' in str(exc_info.value)


class TestOperatorManifest(object):
    def test_from_directory(self, tmpdir):
        subdir = tmpdir.mkdir("nested")

        original = tmpdir.join("original.yaml")
        original.write(ORIGINAL.content)
        replaced = subdir.join("replaced.yaml")
        replaced.write(REPLACED.content)

        manifest = OperatorManifest.from_directory(str(tmpdir))

        original_csv = manifest.files[0]
        replaced_csv = manifest.files[1]

        assert original_csv.path == str(original)
        assert replaced_csv.path == str(replaced)

        assert original_csv.data == ORIGINAL.data
        assert replaced_csv.data == REPLACED.data

    def test_from_directory_no_csvs(self, tmpdir):
        subdir = tmpdir.mkdir("nested")

        original = tmpdir.join("original.yaml")
        replaced = subdir.join("replaced.yaml")

        original_data = ORIGINAL.data
        original_data["kind"] = "IDK"
        with open(str(original), "w") as f:
            yaml.dump(original_data, f)

        replaced_data = REPLACED.data
        del replaced_data["kind"]
        with open(str(replaced), "w") as f:
            yaml.dump(replaced_data, f)

        manifest = OperatorManifest.from_directory(str(tmpdir))
        assert manifest.files == []

    def test_directory_does_not_exist(self, tmpdir):
        nonexistent = tmpdir.join("nonexistent")

        with pytest.raises(RuntimeError) as exc_info:
            OperatorManifest.from_directory(str(nonexistent))

        msg = "Path does not exist or is not a directory: {}".format(nonexistent)
        assert str(exc_info.value) == msg

        regular_file = tmpdir.join("some_file")
        regular_file.write("hello")

        with pytest.raises(RuntimeError) as exc_info:
            OperatorManifest.from_directory(str(regular_file))

        msg = "Path does not exist or is not a directory: {}".format(regular_file)
        assert str(exc_info.value) == msg
