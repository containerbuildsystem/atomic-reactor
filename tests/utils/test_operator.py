"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import copy

from collections import Counter

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from atomic_reactor.util import chain_get
from atomic_reactor.utils.operator import (
    OperatorCSV,
    OperatorManifest,
    NotOperatorCSV,
    default_pullspec_heuristic,
)
from osbs.utils import ImageName


yaml = YAML()


SHA = "5d141ae1081640587636880dbe8489439353df883379158fa8742d5a3be75475"


@pytest.mark.parametrize("text, expected", [
    # Trivial cases
    ("a.b/c:1", ["a.b/c:1"]),
    ("a.b/c/d:1", ["a.b/c/d:1"]),
    # Digests in tag
    ("a.b/c@sha256:{sha}".format(sha=SHA), ["a.b/c@sha256:{sha}".format(sha=SHA)]),
    ("a.b/c/d@sha256:{sha}".format(sha=SHA), ["a.b/c/d@sha256:{sha}".format(sha=SHA)]),
    # Port in registry
    ("a.b:1/c:1", ["a.b:1/c:1"]),
    ("a.b:5000/c/d:1", ["a.b:5000/c/d:1"]),
    # Special characters everywhere
    ("a-b.c_d/e-f.g_h/i-j.k_l@sha256:{sha}".format(sha=SHA),
     ["a-b.c_d/e-f.g_h/i-j.k_l@sha256:{sha}".format(sha=SHA)]),
    ("a-._b/c-._d/e-._f:g-._h", ["a-._b/c-._d/e-._f:g-._h"]),
    ("1.2-3_4/5.6-7_8/9.0-1_2:3.4-5_6", ["1.2-3_4/5.6-7_8/9.0-1_2:3.4-5_6"]),
    # Multiple namespaces
    ("a.b/c/d/e:1", ["a.b/c/d/e:1"]),
    ("a.b/c/d/e/f/g/h/i:1", ["a.b/c/d/e/f/g/h/i:1"]),
    # Enclosed in various non-pullspec characters
    (" a.b/c:1 ", ["a.b/c:1"]),
    ("\na.b/c:1\n", ["a.b/c:1"]),
    ("\ta.b/c:1\t", ["a.b/c:1"]),
    (",a.b/c:1,", ["a.b/c:1"]),
    (";a.b/c:1;", ["a.b/c:1"]),
    ("'a.b/c:1'", ["a.b/c:1"]),
    ('"a.b/c:1"', ["a.b/c:1"]),
    ("<a.b/c:1>", ["a.b/c:1"]),
    ("`a.b/c:1`", ["a.b/c:1"]),
    ("*a.b/c:1*", ["a.b/c:1"]),
    ("(a.b/c:1)", ["a.b/c:1"]),
    ("[a.b/c:1]", ["a.b/c:1"]),
    ("{a.b/c:1}", ["a.b/c:1"]),
    # Enclosed in various pullspec characters
    (".a.b/c:1.", ["a.b/c:1"]),
    ("-a.b/c:1-", ["a.b/c:1"]),
    ("_a.b/c:1_", ["a.b/c:1"]),
    ("/a.b/c:1/", ["a.b/c:1"]),
    ("@a.b/c:1@", ["a.b/c:1"]),
    (":a.b/c:1:", ["a.b/c:1"]),
    # Enclosed in multiple pullspec characters
    ("...a.b/c:1...", ["a.b/c:1"]),
    # Redundant but important interaction of ^ with tags
    ("a.b/c:latest:", ["a.b/c:latest"]),
    ("a.b/c@sha256:{sha}:".format(sha=SHA), ["a.b/c@sha256:{sha}".format(sha=SHA)]),
    ("a.b/c@sha256:{sha}...".format(sha=SHA), ["a.b/c@sha256:{sha}".format(sha=SHA)]),
    ("a.b/c:v1.1...", ["a.b/c:v1.1"]),

    # Empty-ish strings
    ("", []),
    ("!", []),
    (".", []),
    ("!!!", []),
    ("...", []),
    # Not enough parts
    ("a.bc:1", []),
    # No '.' in registry
    ("ab/c:1", []),
    # No tag
    ("a.b/c", []),
    ("a.b/c:", []),
    ("a.b/c:...", []),
    # Invalid digest
    ("a.b/c:@123", []),
    ("a.b/c:@:123", []),
    ("a.b/c:@sha256", []),
    ("a.b/c:@sha256:", []),
    ("a.b/c:@sha256:...", []),
    ("a.b/c:@sha256:123456", []),   # Must be 64 characters
    ("a.b/c:@sha256:{not_b16}".format(not_b16=("a" * 63 + "g")), []),
    # Empty part
    ("a.b//c:1", []),
    ("https://a.b/c:1", []),
    # '@' in registry
    ("a@b.c/d:1", []),
    ("a.b@c/d:1", []),
    # '@' or ':' in namespace
    ("a.b/c@d/e:1", []),
    ("a.b/c:d/e:1", []),
    ("a.b/c/d@e/f:1", []),
    ("a.b/c/d:e/f:1", []),
    # Invalid port in registry
    ("a:b.c/d:1", []),
    ("a.b:c/d:1", []),
    ("a.b:/c:1", []),
    ("a.b:11ff/c:1", []),
    # Some part does not start/end with an alphanumeric character
    ("a.b-/c:1", []),
    ("a.b/-c:1", []),
    ("a.b/c-:1", []),
    ("a.b/c:-1", []),
    ("a.b/-c/d:1", []),
    ("a.b/c-/d:1", []),
    ("a.b/c/-d:1", []),
    ("a.b/c/d-:1", []),
    ("a.b/c/d:-1", []),

    # Separated by various non-pullspec characters
    ("a.b/c:1 d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1\td.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1\nd.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1\n\t d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1,d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1;d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1, d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1; d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 , d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 ; d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    # Separated by pullspec characters
    # Note the space on at least one side of the separator, will not work otherwise
    ("a.b/c:1/ d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 /d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1- d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 -d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1: d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 :d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1. d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 .d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1_ d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 _d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1@ d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("a.b/c:1 @d.e/f:1", ["a.b/c:1", "d.e/f:1"]),

    # Sentences
    ("First is a.b/c:1. Second is d.e/f:1.", ["a.b/c:1", "d.e/f:1"]),
    ("My pullspecs are a.b/c:1 and d.e/f:1.", ["a.b/c:1", "d.e/f:1"]),
    ("There is/are some pullspec(s) in registry.io: a.b/c:1, d.e/f:1", ["a.b/c:1", "d.e/f:1"]),
    ("""
     Find more info on https://my-site.com/here.
     Some pullspec are <i>a.b/c:1<i> and __d.e/f:1__.
     There is also g.h/i:latest: that one is cool.
     And you can email me at name@server.com for info
     about the last one: j.k/l:v1.1.
     """, ["a.b/c:1", "d.e/f:1", "g.h/i:latest", "j.k/l:v1.1"]),
    ("""
     I might also decide to do some math: 50.0/2 = 25.0.
     Perhaps even with variables: 0.5x/2 = x/4.
     And, because I am a psychopath, I will write this: 0.5/2:2 = 1/8,
     Which will be a false positive.
     """, ["0.5/2:2"]),

    # JSON/YAML strings
    ('["a.b/c:1","d.e/f:1", "g.h/i:1"]', ["a.b/c:1", "d.e/f:1", "g.h/i:1"]),
    ('{"a":"a.b/c:1","b": "d.e/f:1", "c": "g.h/i:1"}', ["a.b/c:1", "d.e/f:1", "g.h/i:1"]),
    ("[a.b/c:1,d.e/f:1, g.h/i:1]", ["a.b/c:1", "d.e/f:1", "g.h/i:1"]),
    ("{a: a.b/c:1,b: d.e/f:1, c: g.h/i:1}", ["a.b/c:1", "d.e/f:1", "g.h/i:1"]),
    ("""
     a: a.b/c:1
     b: d.e/f:1
     c: g.h/i:1
     """, ["a.b/c:1", "d.e/f:1", "g.h/i:1"]),
])
def test_pullspec_heuristic(text, expected):
    pullspecs = [text[i:j] for i, j in default_pullspec_heuristic(text)]
    assert pullspecs == expected


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

    @property
    def key(self):
        return self.path[-1]

    def __str__(self):
        return str(self.value)

    def find_in_data(self, data):
        return ImageName.parse(chain_get(data, self.path))


# Names based on location of pullspec:
#   RI = relatedImages
#   C = containers
#   CE = containers env
#   IC = initContainers
#   ICE = initContainers env
#   AN = annotations

RI1 = PullSpec(
    "ri1", "foo:1", "r-foo:2",
    ["spec", "relatedImages", 0, "image"]
)
RI2 = PullSpec(
    "ri2", "registry/bar:1", "r-registry/r-bar:2",
    ["spec", "relatedImages", 1, "image"]
)
C1 = PullSpec(
    "c1", "registry/namespace/spam:1", "r-registry/r-namespace/r-spam:2",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "spec", "containers", 0, "image"]
)
CE1 = PullSpec(
    "ce1", "eggs:1", "r-eggs:2",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "spec", "containers", 0, "env", 0, "value"]
)
C2 = PullSpec(
    "c2", "ham:1", "r-ham:2",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "spec", "containers", 1, "image"]
)
C3 = PullSpec(
    "c3", "jam:1", "r-jam:2",
    ["spec", "install", "spec", "deployments", 1,
     "spec", "template", "spec", "containers", 0, "image"]
)
AN1 = PullSpec(
    "an1", "registry/namespace/baz:latest", "r-registry/r-namespace/r-baz:latest",
    ["metadata", "annotations", "containerImage"]
)
IC1 = PullSpec(
    "ic1", "pullspec:1", "r-pullspec:1",
    ["spec", "install", "spec", "deployments", 1,
     "spec", "template", "spec", "initContainers", 0, "image"]
)
ICE1 = PullSpec(
    "ice1", "pullspec:2", "r-pullspec:2",
    ["spec", "install", "spec", "deployments", 1,
     "spec", "template", "spec", "initContainers", 0, "env", 0, "value"]
)
AN2 = PullSpec(
    "an2", "registry.io/an2:1", "registry.io/r-an2:1",
    ["metadata", "annotations", "some_pullspec"]
)
AN3 = PullSpec(
    "an3", "registry.io/an3:1", "registry.io/r-an3:1",
    ["metadata", "annotations", "two_pullspecs"]
)
AN4 = PullSpec(
    "an4", "registry.io/an4:1", "registry.io/r-an4:1",
    ["metadata", "annotations", "two_pullspecs"]
)
AN5 = PullSpec(
    "an5", "registry.io/an5:1", "registry.io/r-an5:1",
    ["spec", "install", "spec", "deployments", 0,
     "spec", "template", "metadata", "annotations", "some_other_pullspec"]
)
AN6 = PullSpec(
    "an6", "registry.io/an6:1", "registry.io/r-an6:1",
    ["random", "annotations", 0, "metadata", "annotations", "duplicate_pullspecs"]
)
AN7 = PullSpec(
    "an7", "registry.io/an7:1", "registry.io/r-an7:1",
    ["random", "annotations", 0, "metadata", "annotations", "duplicate_pullspecs"]
)


PULLSPECS = {
    p.name: p for p in [
        RI1, RI2, C1, CE1, C2, C3, AN1, IC1, ICE1, AN2, AN3, AN4, AN5, AN6, AN7
    ]
}


ORIGINAL_CONTENT = """\
# A meaningful comment
kind: ClusterServiceVersion
metadata:
  annotations:
    containerImage: {an1}
    some_pullspec: {an2}
    two_pullspecs: {an3}, {an4}
spec:
  relatedImages:
  - name: ri1
    image: {ri1}
  - name: ri2
    image: {ri2}
  install:
    spec:
      deployments:
      - spec:
          template:
            metadata:
              annotations:
                some_other_pullspec: {an5}
            spec:
              containers:
              - name: c1
                image: {c1}
                env:
                - name: RELATED_IMAGE_CE1
                  value: {ce1}
                - name: UNRELATED_IMAGE
                  value: {ce1}
              - name: c2
                image: {c2}
      - spec:
          template:
            spec:
              containers:
              - name: c3
                image: {c3}
              initContainers:
              - name: ic1
                image: {ic1}
                env:
                - name: RELATED_IMAGE_ICE1
                  value: {ice1}
random:
  annotations:
  - metadata:
      annotations:
        duplicate_pullspecs: {an6}, {an7}, {an6}, {an7}
  nested:
    dict:
      a: {ri1}
      b: {ri2}
      c: {c1}
      d: {ce1}
      e: {c2}
      f: {c3}
      g: {an1}
      h: {ic1}
      i: {ice1}
    list:
    - {ri1}
    - {ri2}
    - {c1}
    - {ce1}
    - {c2}
    - {c3}
    - {an1}
    - {ic1}
    - {ice1}
""".format(**PULLSPECS)

REPLACED_CONTENT = """\
# A meaningful comment
kind: ClusterServiceVersion
metadata:
  annotations:
    containerImage: {an1.replace}
    some_pullspec: {an2.replace}
    two_pullspecs: {an3.replace}, {an4.replace}
spec:
  relatedImages:
  - name: ri1
    image: {ri1.replace}
  - name: ri2
    image: {ri2.replace}
  install:
    spec:
      deployments:
      - spec:
          template:
            metadata:
              annotations:
                some_other_pullspec: {an5.replace}
            spec:
              containers:
              - name: c1
                image: {c1.replace}
                env:
                - name: RELATED_IMAGE_CE1
                  value: {ce1.replace}
                - name: UNRELATED_IMAGE
                  value: {ce1}
              - name: c2
                image: {c2.replace}
      - spec:
          template:
            spec:
              containers:
              - name: c3
                image: {c3.replace}
              initContainers:
              - name: ic1
                image: {ic1.replace}
                env:
                - name: RELATED_IMAGE_ICE1
                  value: {ice1.replace}
random:
  annotations:
  - metadata:
      annotations:
        duplicate_pullspecs: {an6.replace}, {an7.replace}, {an6.replace}, {an7.replace}
  nested:
    dict:
      a: {ri1}
      b: {ri2}
      c: {c1}
      d: {ce1}
      e: {c2}
      f: {c3}
      g: {an1}
      h: {ic1}
      i: {ice1}
    list:
    - {ri1}
    - {ri2}
    - {c1}
    - {ce1}
    - {c2}
    - {c3}
    - {an1}
    - {ic1}
    - {ice1}
""".format(**PULLSPECS)

REPLACED_EVERYWHERE_CONTENT = """\
# A meaningful comment
kind: ClusterServiceVersion
metadata:
  annotations:
    containerImage: {an1.replace}
    some_pullspec: {an2.replace}
    two_pullspecs: {an3.replace}, {an4.replace}
spec:
  relatedImages:
  - name: ri1
    image: {ri1.replace}
  - name: ri2
    image: {ri2.replace}
  install:
    spec:
      deployments:
      - spec:
          template:
            metadata:
              annotations:
                some_other_pullspec: {an5.replace}
            spec:
              containers:
              - name: c1
                image: {c1.replace}
                env:
                - name: RELATED_IMAGE_CE1
                  value: {ce1.replace}
                - name: UNRELATED_IMAGE
                  value: {ce1.replace}
              - name: c2
                image: {c2.replace}
      - spec:
          template:
            spec:
              containers:
              - name: c3
                image: {c3.replace}
              initContainers:
              - name: ic1
                image: {ic1.replace}
                env:
                - name: RELATED_IMAGE_ICE1
                  value: {ice1.replace}
random:
  annotations:
  - metadata:
      annotations:
        duplicate_pullspecs: {an6.replace}, {an7.replace}, {an6.replace}, {an7.replace}
  nested:
    dict:
      a: {ri1.replace}
      b: {ri2.replace}
      c: {c1.replace}
      d: {ce1.replace}
      e: {c2.replace}
      f: {c3.replace}
      g: {an1.replace}
      h: {ic1.replace}
      i: {ice1.replace}
    list:
    - {ri1.replace}
    - {ri2.replace}
    - {c1.replace}
    - {ce1.replace}
    - {c2.replace}
    - {c3.replace}
    - {an1.replace}
    - {ic1.replace}
    - {ice1.replace}
""".format(**PULLSPECS)

YAML_LIST_CONTENT = """\
- op: replace
  path: /spec/foo
  value:
    type: object
    properties:
      name:
        type: string
        enum:
        - "bar"
"""


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
YAML_LIST = CSVFile(YAML_LIST_CONTENT)


def delete_all_annotations(obj):
    if isinstance(obj, (dict, CommentedMap)):
        obj.get("metadata", {}).pop("annotations", None)
        for v in obj.values():
            delete_all_annotations(v)
    elif isinstance(obj, (list, CommentedSeq)):
        for item in obj:
            delete_all_annotations(item)


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

    def test_yaml_not_object(self):
        data = YAML_LIST.data
        with pytest.raises(NotOperatorCSV) as exc_info:
            OperatorCSV("original.yaml", data)
        assert str(exc_info.value) == "File does not contain a YAML object"

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
            "original.yaml - Found pullspec for relatedImage ri1: {ri1}",
            "original.yaml - Found pullspec for relatedImage ri2: {ri2}",
            "original.yaml - Found pullspec for RELATED_IMAGE_CE1 var: {ce1}",
            "original.yaml - Found pullspec for RELATED_IMAGE_ICE1 var: {ice1}",
            "original.yaml - Found pullspec for container c1: {c1}",
            "original.yaml - Found pullspec for container c2: {c2}",
            "original.yaml - Found pullspec for container c3: {c3}",
            "original.yaml - Found pullspec for initContainer ic1: {ic1}",
            "original.yaml - Found pullspec for {an1.key} annotation: {an1}",
            "original.yaml - Found pullspec for {an2.key} annotation: {an2}",
            "original.yaml - Found pullspec for {an2.key} annotation: {an2}",
            "original.yaml - Found pullspec for {an3.key} annotation: {an3}",
            "original.yaml - Found pullspec for {an4.key} annotation: {an4}",
            "original.yaml - Found pullspec for {an5.key} annotation: {an5}",
            "original.yaml - Found pullspec for {an6.key} annotation: {an6}",
            "original.yaml - Found pullspec for {an7.key} annotation: {an7}",
        ]
        for log in expected_logs:
            assert log.format(**PULLSPECS) in caplog.text

    def test_replace_pullspecs(self, caplog):
        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        csv.replace_pullspecs(self._replacement_pullspecs)
        assert csv.data == REPLACED.data

        expected_logs = [
            "{file} - Replaced pullspec for relatedImage ri1: {ri1} -> {ri1.replace}",
            "{file} - Replaced pullspec for relatedImage ri2: {ri2} -> {ri2.replace}",
            "{file} - Replaced pullspec for RELATED_IMAGE_CE1 var: {ce1} -> {ce1.replace}",
            "{file} - Replaced pullspec for RELATED_IMAGE_ICE1 var: {ice1} -> {ice1.replace}",
            "{file} - Replaced pullspec for container c1: {c1} -> {c1.replace}",
            "{file} - Replaced pullspec for container c2: {c2} -> {c2.replace}",
            "{file} - Replaced pullspec for container c3: {c3} -> {c3.replace}",
            "{file} - Replaced pullspec for initContainer ic1: {ic1} -> {ic1.replace}",
            "{file} - Replaced pullspec for {an1.key} annotation: {an1} -> {an1.replace}",
            "{file} - Replaced pullspec for {an2.key} annotation: {an2} -> {an2.replace}",
            "{file} - Replaced pullspec for {an3.key} annotation: {an3} -> {an3.replace}",
            "{file} - Replaced pullspec for {an4.key} annotation: {an4} -> {an4.replace}",
            "{file} - Replaced pullspec for {an5.key} annotation: {an5} -> {an5.replace}",
            "{file} - Replaced pullspec for {an6.key} annotation: {an6} -> {an6.replace}",
            "{file} - Replaced pullspec for {an7.key} annotation: {an7} -> {an7.replace}",
        ]
        for log in expected_logs:
            assert log.format(file="original.yaml", **PULLSPECS) in caplog.text

    def test_replace_pullspecs_everywhere(self, caplog):
        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        csv.replace_pullspecs_everywhere(self._replacement_pullspecs)
        assert csv.data == REPLACED_EVERYWHERE.data

        expected_logs = {
            "{file} - Replaced pullspec: {ri1} -> {ri1.replace}": 3,
            "{file} - Replaced pullspec: {ri2} -> {ri2.replace}": 3,
            "{file} - Replaced pullspec: {ce1} -> {ce1.replace}": 4,
            "{file} - Replaced pullspec: {c1} -> {c1.replace}": 3,
            "{file} - Replaced pullspec: {c2} -> {c2.replace}": 3,
            "{file} - Replaced pullspec: {c3} -> {c3.replace}": 3,
            "{file} - Replaced pullspec: {an1} -> {an1.replace}": 2,
            "{file} - Replaced pullspec: {ic1} -> {ic1.replace}": 3,
            "{file} - Replaced pullspec: {ice1} -> {ice1.replace}": 3,
            "{file} - Replaced pullspec for {an1.key} annotation: {an1} -> {an1.replace}": 1,
            "{file} - Replaced pullspec for {an2.key} annotation: {an2} -> {an2.replace}": 1,
            "{file} - Replaced pullspec for {an3.key} annotation: {an3} -> {an3.replace}": 1,
            "{file} - Replaced pullspec for {an4.key} annotation: {an4} -> {an4.replace}": 1,
            "{file} - Replaced pullspec for {an5.key} annotation: {an5} -> {an5.replace}": 1,
            "{file} - Replaced pullspec for {an6.key} annotation: {an6} -> {an6.replace}": 2,
            "{file} - Replaced pullspec for {an7.key} annotation: {an7} -> {an7.replace}": 2,
        }
        # NOTE: an1 gets replaced once as an annotation and twice in other places
        # an6 and an7 both get replaced twice in the same annotation

        for log, count in expected_logs.items():
            assert caplog.text.count(log.format(file="original.yaml", **PULLSPECS)) == count

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

        # ri1 won't be replaced because replacement is identical
        replacement_pullspecs[RI1.value] = RI1.value
        # ri2 won't be replaced because no replacement available
        del replacement_pullspecs[RI2.value]

        csv = OperatorCSV("original.yaml", ORIGINAL.data)
        csv.replace_pullspecs(replacement_pullspecs)

        assert RI1.find_in_data(csv.data) == RI1.value
        assert RI2.find_in_data(csv.data) == RI2.value

        ri1_log = "original.yaml - Replaced pullspec for relatedImage ri1: {ri1}"
        ri2_log = "original.yaml - Replaced pullspec for relatedImage ri2: {ri2}"

        assert ri1_log.format(ri1=RI1) not in caplog.text
        assert ri2_log.format(ri2=RI2) not in caplog.text

    @pytest.mark.parametrize("rel_images", [True, False])
    @pytest.mark.parametrize("rel_envs, containers", [
        (False, False),
        (False, True),
        # (True, False) - Cannot have envs without containers
        (True, True),
    ])
    @pytest.mark.parametrize("annotations", [True, False])
    @pytest.mark.parametrize("init_rel_envs, init_containers", [
        (False, False),
        (False, True),
        # (True, False) - Cannot have initContainer envs without initContainers
        (True, True),
    ])
    def test_get_pullspecs_some_locations(self, rel_images, rel_envs, containers,
                                          annotations, init_rel_envs, init_containers):
        data = ORIGINAL.data
        expected = {p.value for p in PULLSPECS.values()}

        if not rel_images:
            expected -= {RI1.value, RI2.value}
            del data["spec"]["relatedImages"]
        deployments = chain_get(data, ["spec", "install", "spec", "deployments"])
        if not rel_envs:
            expected -= {CE1.value}
            for d in deployments:
                for c in chain_get(d, ["spec", "template", "spec", "containers"]):
                    c.pop("env", None)
        if not containers:
            expected -= {C1.value, C2.value, C3.value}
            for d in deployments:
                del d["spec"]["template"]["spec"]["containers"]
        if not annotations:
            expected -= {AN1.value, AN2.value, AN3.value,
                         AN4.value, AN5.value, AN6.value, AN7.value}
            delete_all_annotations(data)
        if not init_rel_envs:
            expected -= {ICE1.value}
            for d in deployments:
                for c in chain_get(d, ["spec", "template", "spec", "initContainers"], default=[]):
                    c.pop("env", None)
        if not init_containers:
            expected -= {IC1.value}
            for d in deployments:
                d["spec"]["template"]["spec"].pop("initContainers", None)

        csv = OperatorCSV("x.yaml", data)
        assert csv.get_pullspecs() == expected

    def test_valuefrom_references_not_allowed(self):
        data = ORIGINAL.data
        env_path = CE1.path[:-1]
        env = chain_get(data, env_path)
        env["valueFrom"] = "somewhere"

        csv = OperatorCSV("original.yaml", data)
        with pytest.raises(RuntimeError) as exc_info:
            csv.get_pullspecs()

        assert '"valueFrom" references are not supported' in str(exc_info.value)

    def test_set_related_images(self, caplog):
        data = ORIGINAL.data
        csv = OperatorCSV("original.yaml", data)
        csv.set_related_images()

        # the order is:
        #   1. existing relatedImages
        #   2. known annotations
        #   3. containers
        #   4. initContainers
        #   5. container env vars
        #   6. initContainer env vars
        #   7. other annotations (in reverse order - quirky, I know)
        expected_related_images = [
            CommentedMap([("name", name), ("image", pullspec.value.to_str())])
            for name, pullspec in [
                ("ri1", RI1),
                ("ri2", RI2),
                ("baz-latest-annotation", AN1),
                ("c1", C1),
                ("c2", C2),
                ("c3", C3),
                ("ic1", IC1),
                ("ce1", CE1),
                ("ice1", ICE1),
                ("an7-1-annotation", AN7),
                ("an6-1-annotation", AN6),
                ("an5-1-annotation", AN5),
                ("an4-1-annotation", AN4),
                ("an3-1-annotation", AN3),
                ("an2-1-annotation", AN2),
            ]
        ]
        assert csv.data["spec"]["relatedImages"] == expected_related_images

        expected_logs = [
            "{path} - Set relatedImage ri1 (from relatedImage ri1): {ri1}",
            "{path} - Set relatedImage ri2 (from relatedImage ri2): {ri2}",
            "{path} - Set relatedImage baz-latest-annotation (from {an1.key} annotation): {an1}",
            "{path} - Set relatedImage c1 (from container c1): {c1}",
            "{path} - Set relatedImage c2 (from container c2): {c2}",
            "{path} - Set relatedImage c3 (from container c3): {c3}",
            "{path} - Set relatedImage ic1 (from initContainer ic1): {ic1}",
            "{path} - Set relatedImage ce1 (from RELATED_IMAGE_CE1 var): {ce1}",
            "{path} - Set relatedImage ice1 (from RELATED_IMAGE_ICE1 var): {ice1}",
            "{path} - Set relatedImage an2-1-annotation (from {an2.key} annotation): {an2}",
            "{path} - Set relatedImage an3-1-annotation (from {an3.key} annotation): {an3}",
            "{path} - Set relatedImage an4-1-annotation (from {an4.key} annotation): {an4}",
            "{path} - Set relatedImage an5-1-annotation (from {an5.key} annotation): {an5}",
            "{path} - Set relatedImage an6-1-annotation (from {an6.key} annotation): {an6}",
            "{path} - Set relatedImage an7-1-annotation (from {an7.key} annotation): {an7}",
        ]
        for log in expected_logs:
            assert log.format(path="original.yaml", **PULLSPECS) in caplog.text

    @pytest.mark.parametrize('pullspec, name', [
        ('registry.io/foo:v1', 'foo-v1-annotation'),
        ('registry.io/namespace/foo:v1', 'foo-v1-annotation'),
        ('registry.io/foo@sha256:{}'.format(SHA), 'foo-{}-annotation'.format(SHA)),
        ('registry.io/namespace/foo@sha256:{}'.format(SHA), 'foo-{}-annotation'.format(SHA)),
    ])
    def test_related_annotation_names(self, pullspec, name):
        data = {
            'kind': 'ClusterServiceVersion',
            'metadata': {
                'annotations': {
                    'foo': pullspec
                }
            }
        }
        csv = OperatorCSV("original.yaml", data)
        csv.set_related_images()
        generated_name = csv.data["spec"]["relatedImages"][0]["name"]
        assert generated_name == name

    @pytest.mark.parametrize('p1, p2, should_fail', [
        # Different tag, no conflict
        ('registry.io/foo:v1', 'registry.io/foo:v2', False),
        # Identical pullspec, no conflict
        ('registry.io/foo:v1', 'registry.io/foo:v1', False),
        # Same repo and tag but different pullspec
        ('registry.io/foo:v1', 'registry.io/namespace/foo:v1', True),
        # Sha in digest happens to be the same as the tag
        ('registry.io/foo@sha256:{0}'.format(SHA), 'registry.io/foo:{0}'.format(SHA), True),
    ])
    def test_related_annotation_name_conflicts(self, p1, p2, should_fail):
        data = {
            'kind': 'ClusterServiceVersion',
            'metadata': {
                'annotations': {
                    'foo': "{}, {}".format(p1, p2)
                }
            }
        }
        csv = OperatorCSV("original.yaml", data)
        if should_fail:
            with pytest.raises(RuntimeError) as exc_info:
                csv.set_related_images()
            msg = ("original.yaml - Found conflicts when setting relatedImages:\n"
                   "foo annotation: {} X foo annotation: {}".format(p2, p1))
            assert str(exc_info.value) == msg
        else:
            csv.set_related_images()

    @pytest.mark.parametrize("related_images, containers, err_msg", [
        (
            # conflict in original relatedImages
            [{"name": "foo", "image": "foo"}, {"name": "foo", "image": "bar"}],
            [],
            ("{path} - Found conflicts when setting relatedImages:\n"
             "relatedImage foo: foo X relatedImage foo: bar")
        ),
        (
            # conflict in new relatedImages
            [],
            [{"name": "foo", "image": "foo"}, {"name": "foo", "image": "bar"}],
            ("{path} - Found conflicts when setting relatedImages:\n"
             "container foo: foo X container foo: bar")
        ),
        (
            # conflict between original and new relatedImages
            [{"name": "foo", "image": "foo"}],
            [{"name": "foo", "image": "bar"}],
            ("{path} - Found conflicts when setting relatedImages:\n"
             "relatedImage foo: foo X container foo: bar")
        ),
        (
            # duplicate in original relatedImages, no conflict
            [{"name": "foo", "image": "foo"}, {"name": "foo", "image": "foo"}],
            [],
            None
        ),
        (
            # duplicate in new relatedImages, no conflict
            [],
            [{"name": "foo", "image": "foo"}, {"name": "foo", "image": "foo"}],
            None
        ),
        (
            # duplicate between original and new relatedImages, no conflict
            [{"name": "foo", "image": "foo"}],
            [{"name": "foo", "image": "foo"}],
            None
        ),
        (
            # multiple conflicts in original and new relatedImages
            [{"name": "foo", "image": "foo"}, {"name": "foo", "image": "bar"}],
            [{"name": "foo", "image": "baz"}, {"name": "foo", "image": "spam"}],
            # all messages should be (first found pullspec X conflicting pullspec)
            ("{path} - Found conflicts when setting relatedImages:\n"
             "relatedImage foo: foo X relatedImage foo: bar\n"
             "relatedImage foo: foo X container foo: baz\n"
             "relatedImage foo: foo X container foo: spam")
        )
    ])
    def test_set_related_images_conflicts(self, related_images, containers, err_msg):
        data = {
            "kind": "ClusterServiceVersion",
            "spec": {
                "relatedImages": related_images,
                "install": {
                    "spec": {
                        "deployments": [
                            {
                                "spec": {
                                    "template": {
                                        "spec": {
                                            "containers": containers
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        csv = OperatorCSV("original.yaml", data)

        if err_msg is not None:
            with pytest.raises(RuntimeError) as exc_info:
                csv.set_related_images()
            assert str(exc_info.value) == err_msg.format(path="original.yaml")
        else:
            csv.set_related_images()
            updated_counts = Counter(x['name'] for x in csv.data['spec']['relatedImages'])
            # check that there are no duplicates in .spec.relatedImages
            for name, count in updated_counts.items():
                assert count == 1, 'Duplicate in relatedImages: {}'.format(name)

    @pytest.mark.parametrize('pullspecs, does_have', [
        (None, False),
        ([], False),
        ({'name': 'foo', 'image': 'bar'}, True),
    ])
    def test_has_related_images(self, pullspecs, does_have):
        data = {
            'kind': 'ClusterServiceVersion',
            'spec': {}
        }
        if pullspecs is not None:
            data['spec']['relatedImages'] = pullspecs
        csv = OperatorCSV('original.yaml', data)
        assert csv.has_related_images() == does_have

    @pytest.mark.parametrize('var, does_have', [
        (None, False),
        ({'name': 'UNRELATED_IMAGE', 'value': 'foo'}, False),
        ({'name': 'RELATED_IMAGE_BAR', 'value': 'baz'}, True),
    ])
    def test_has_related_image_envs(self, var, does_have):
        data = {
            'kind': 'ClusterServiceVersion',
            'spec': {
                'install': {
                    'spec': {
                        'deployments': [
                            {
                                'spec': {
                                    'template': {
                                        'spec': {
                                            'containers': [
                                                {'name': 'spam', 'image': 'eggs', 'env': []}
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        if var is not None:
            deployment = data['spec']['install']['spec']['deployments'][0]
            deployment['spec']['template']['spec']['containers'][0]['env'].append(var)
        csv = OperatorCSV('original.yaml', data)
        assert csv.has_related_image_envs() == does_have

    def test_related_images_no_pullspecs(self, caplog):
        """If no pullspecs are detected, skip creation of relatedImages"""
        data = {
            'kind': 'ClusterServiceVersion',
            'metadata': {
                'annotations': {
                    'foo': 'test'
                }
            }
        }
        csv = OperatorCSV("original.yaml", data)
        csv.set_related_images()
        assert "" in caplog.text
        assert 'relatedImages' not in csv.data.get("spec", {})

    @pytest.mark.parametrize('pullspecs, replacements, expected', [
        # 1st is a substring of 2nd
        (['a.b/c:1', 'a.b/c:1.1'],
         {'a.b/c:1': 'foo:1', 'a.b/c:1.1': 'bar:1'},
         ['foo:1', 'bar:1']),
        # Same but reversed
        (['a.b/c:1.1', 'a.b/c:1'],
         {'a.b/c:1': 'foo:1', 'a.b/c:1.1': 'bar:1'},
         ['bar:1', 'foo:1']),
        # 2nd is 1st after replacement
        (['a.b/c:1', 'd.e/f:1'],
         {'a.b/c:1': 'd.e/f:1', 'd.e/f:1': 'g.h/i:1'},
         ['d.e/f:1', 'g.h/i:1']),
        # Same but reversed
        (['d.e/f:1', 'a.b/c:1'],
         {'a.b/c:1': 'd.e/f:1', 'd.e/f:1': 'g.h/i:1'},
         ['g.h/i:1', 'd.e/f:1']),
        # Replacement is a swap
        (['a.b/c:1', 'd.e/f:1'],
         {'a.b/c:1': 'd.e/f:1', 'd.e/f:1': 'a.b/c:1'},
         ['d.e/f:1', 'a.b/c:1']),
    ])
    def test_tricky_annotation_replacements(self, pullspecs, replacements, expected):
        replacements = {
            ImageName.parse(old): ImageName.parse(new)
            for old, new in replacements.items()
        }
        data = {
            'kind': 'ClusterServiceVersion',
            'metadata': {
                'annotations': {
                    'foo': ", ".join(pullspecs)
                }
            }
        }
        csv = OperatorCSV("original.yaml", data)
        csv.replace_pullspecs(replacements)
        assert csv.data['metadata']['annotations']['foo'] == ", ".join(expected)

    def test_known_vs_other_annotations(self):
        # All annotation must be found and replaced exactly once, heuristic
        # must not look in keys that are known pullspec sources
        data = {
            'kind': 'ClusterServiceVersion',
            'metadata': {
                'annotations': {
                    'containerImage': 'a.b/c:1',
                    'notContainerImage': 'a.b/c:1'
                }
            },
            'spec': {
                'metadata': {
                    'annotations': {
                        'containerImage': 'a.b/c:1',
                        'notContainerImage': 'a.b/c:1'
                    }
                }
            }
        }
        replacements = {
            ImageName.parse(old): ImageName.parse(new) for old, new in [
                ('a.b/c:1', 'd.e/f:1'),
                ('d.e/f:1', 'g.h/i:1'),
            ]
        }
        csv = OperatorCSV("original.yaml", data)
        csv.replace_pullspecs(replacements)

        assert csv.data["metadata"]["annotations"]["containerImage"] == 'd.e/f:1'
        assert csv.data["metadata"]["annotations"]["notContainerImage"] == 'd.e/f:1'
        assert csv.data["spec"]["metadata"]["annotations"]["containerImage"] == 'd.e/f:1'
        assert csv.data["spec"]["metadata"]["annotations"]["notContainerImage"] == 'd.e/f:1'

    def test_ignored_annotations(self):
        data = {
            'kind': 'ClusterServiceVersion',
            'metadata': {
                'annotations': {
                    'some_text': 'abcdef',
                    'some_number': 123,
                    'some_array': [],
                    'some_object': {},
                    'metadata': {
                        'annotations': {
                            'pullspec': 'metadata.annotations/nested.in:metadata.annotations'
                        }
                    }
                },
                'not_an_annotation': 'something.that/looks-like:a-pullspec',
                'not_annotations': {
                    'also_not_an_annotation': 'other.pullspec/lookalike:thing'
                },
                'metadata': {
                    'annotations': {
                        'pullspec': 'metadata.annotations/nested.in:metadata'
                    }
                }
            }
        }
        csv = OperatorCSV("original.yaml", data)
        assert csv.get_pullspecs() == set()


class TestOperatorManifest(object):
    def test_from_directory_multiple_csvs(self, tmpdir):
        """Exactly one CSV file must be in manifests"""
        subdir = tmpdir.mkdir("nested")

        original = tmpdir.join("original.yaml")
        original.write(ORIGINAL.content)
        replaced = subdir.join("replaced.yaml")
        replaced.write(REPLACED.content)

        with pytest.raises(ValueError) as exc_info:
            OperatorManifest.from_directory(str(tmpdir))
        assert "Operator bundle may contain only 1 CSV file" in str(exc_info.value)

    def test_from_directory_single_csv(self, tmpdir):

        original = tmpdir.join("original.yaml")
        original.write(ORIGINAL.content)

        manifest = OperatorManifest.from_directory(str(tmpdir))

        original_csv = manifest.files[0]

        assert original_csv.path == str(original)
        assert original_csv.data == ORIGINAL.data

        assert manifest.csv.path == str(original)
        assert manifest.csv.data == ORIGINAL.data

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

        with pytest.raises(ValueError) as exc_info:
            OperatorManifest.from_directory(str(tmpdir))
        assert "Missing ClusterServiceVersion in operator manifests" in str(exc_info.value)

    def test_from_directory_yaml_list(self, tmpdir):
        yaml_list = tmpdir.join("list.yaml")
        original = tmpdir.join("original.yaml")

        yaml_list_data = YAML_LIST.data
        with open(str(yaml_list), "w") as f:
            yaml.dump(yaml_list_data, f)
        with open(str(original), "w") as f:
            yaml.dump(ORIGINAL.data, f)

        manifest = OperatorManifest.from_directory(str(tmpdir))
        assert manifest.csv

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
