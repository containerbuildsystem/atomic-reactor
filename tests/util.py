# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import requests
import uuid


def add_koji_map_in_workflow(workflow, hub_url, root_url=None, reserve_build=None,
                             delegate_task=None, delegated_priority=None,
                             proxyuser=None, ssl_certs_dir=None,
                             krb_principal=None, krb_keytab=None):
    reactor_config = workflow.conf.conf
    if not reactor_config:
        reactor_config = {}

    koji_map = reactor_config['koji'] = {
        'hub_url': hub_url,
        'auth': {},
    }

    if root_url is not None:
        koji_map['root_url'] = root_url

    if reserve_build is not None:
        koji_map['reserve_build'] = reserve_build

    if delegate_task is not None:
        koji_map['delegate_task'] = delegate_task

    if delegated_priority:
        koji_map['delegated_task_priority'] = delegated_priority

    if proxyuser:
        koji_map['auth']['proxyuser'] = proxyuser

    if ssl_certs_dir:
        koji_map['auth']['ssl_certs_dir'] = ssl_certs_dir

    if krb_principal:
        koji_map['auth']['krb_principal'] = str(krb_principal)

    if krb_keytab:
        koji_map['auth']['krb_keytab_path'] = str(krb_keytab)


def uuid_value():
    return uuid.uuid4().hex


def is_string_type(obj):
    """
    Test whether obj is a string type

    :param obj: object to test
    :return: bool, whether obj is a string type
    """

    return isinstance(obj, str)


def has_connection():
    try:
        requests.head("http://github.com/")
        return True
    except requests.ConnectionError:
        return False


FAKE_CSV = '''\
apiVersion: operators.coreos.com/v1alpha1
kind: ClusterServiceVersion
metadata: {}
spec:
    install: {}
'''

OPERATOR_MANIFESTS_DIR = 'operator-manifests'


def mock_manifests_dir(repo_dir, dirname=None):
    """Create fake manifests_dir for testing the CSV verification

    :param repo_dir: the repository directory inside which to create the
        manifests directory and CSV file.
    :type: py.path.LocalPath
    :param str dirname: the manifests directory name. Defaults to operator-manifests.
    :return: the manifests directory
    :rtype: py.path.LocalPath
    """
    manifests_dir = repo_dir.join(dirname or OPERATOR_MANIFESTS_DIR).mkdir()
    fake_csv = (manifests_dir
                .join('1.0.0').mkdir()
                .join('operator.clusterserviceversion.yaml'))
    fake_csv.write(FAKE_CSV)
    return manifests_dir
