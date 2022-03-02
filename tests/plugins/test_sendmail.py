import smtplib
from collections import namedtuple

from flexmock import flexmock
import koji
import pytest
import json
from atomic_reactor.constants import DOCKERFILE_FILENAME

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.exit_sendmail import SendMailPlugin, validate_address
from atomic_reactor.plugins.exit_store_metadata import StoreMetadataPlugin
from atomic_reactor.plugins.post_koji_import import KojiImportPlugin
from atomic_reactor.utils.koji import get_koji_task_owner
from atomic_reactor.config import Configuration
from tests.util import add_koji_map_in_workflow
from osbs.exceptions import OsbsException
from smtplib import SMTPException

MS, MF = SendMailPlugin.MANUAL_SUCCESS, SendMailPlugin.MANUAL_FAIL
MC = SendMailPlugin.MANUAL_CANCELED

MOCK_EMAIL_DOMAIN = "domain.com"
MOCK_KOJI_TASK_ID = 12345
MOCK_KOJI_MISSING_TASK_ID = -12345
MOCK_KOJI_BUILD_ID = 98765
MOCK_KOJI_PACKAGE_ID = 123
MOCK_KOJI_TAG_ID = 456
MOCK_KOJI_OWNER_ID = 789
MOCK_KOJI_OWNER_NAME = "foo"
MOCK_KOJI_OWNER_EMAIL = "foo@bar.com"
MOCK_KOJI_OWNER_GENERATED = "@".join([MOCK_KOJI_OWNER_NAME, MOCK_EMAIL_DOMAIN])
MOCK_KOJI_SUBMITTER_ID = 123456
MOCK_KOJI_MISSING_SUBMITTER_ID = -123456
MOCK_KOJI_SUBMITTER_NAME = "baz"
MOCK_KOJI_SUBMITTER_EMAIL = "baz@bar.com"
MOCK_KOJI_SUBMITTER_GENERATED = "@".join([MOCK_KOJI_SUBMITTER_NAME, MOCK_EMAIL_DOMAIN])
MOCK_ADDITIONAL_EMAIL = "spam@bar.com"
MOCK_NAME_LABEL = 'foo/bar_in_df'
MOCK_DOCKERFILE = ('FROM base\n'
                   'LABEL Name={name}\n'
                   .format(name=MOCK_NAME_LABEL))

LogEntry = namedtuple('LogEntry', ['platform', 'line'])

pytestmark = pytest.mark.usefixtures('user_params')


class mock_source(object):
    def get_vcs_info(self):
        return None


class mock_builder(object):
    df_path = None


class MockedClientSession(object):
    def __init__(self, hub, opts=None, has_kerberos=True):
        self.has_kerberos = has_kerberos

    def krb_login(self, principal=None, keytab=None, proxyuser=None):
        raise RuntimeError('No certificates provided')

    def ssl_login(self, cert=None, ca=None, serverca=None, proxyuser=None):
        return True

    def getBuild(self, build_id):
        assert build_id == MOCK_KOJI_BUILD_ID
        return {'package_id': MOCK_KOJI_PACKAGE_ID}

    def listTags(self, build_id):
        assert build_id == MOCK_KOJI_BUILD_ID
        return [{"id": MOCK_KOJI_TAG_ID}]

    def getPackageConfig(self, tag_id, package_id):
        assert tag_id == MOCK_KOJI_TAG_ID
        assert package_id == MOCK_KOJI_PACKAGE_ID
        return {"owner_id": MOCK_KOJI_OWNER_ID}

    def getUser(self, user_id):
        if user_id == MOCK_KOJI_OWNER_ID:
            if self.has_kerberos:
                return {"krb_principal": MOCK_KOJI_OWNER_EMAIL}
            else:
                return {"krb_principal": "",
                        "name": MOCK_KOJI_OWNER_NAME}

        elif user_id == MOCK_KOJI_SUBMITTER_ID:
            if self.has_kerberos:
                return {"krb_principal": MOCK_KOJI_SUBMITTER_EMAIL}
            else:
                return {"krb_principal": "",
                        "name": MOCK_KOJI_SUBMITTER_NAME}

        elif user_id == MOCK_KOJI_MISSING_SUBMITTER_ID:
            return {}
        else:
            assert False, "Don't know user with id %s" % user_id

    def getTaskInfo(self, task_id):
        assert (task_id == MOCK_KOJI_TASK_ID
                or task_id == MOCK_KOJI_MISSING_TASK_ID)
        if task_id == MOCK_KOJI_TASK_ID:
            return {"owner": MOCK_KOJI_SUBMITTER_ID}
        elif task_id == MOCK_KOJI_MISSING_TASK_ID:
            return {"owner": MOCK_KOJI_MISSING_SUBMITTER_ID}

    def listTaskOutput(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return ["openshift-final.log", "build.log"]


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def work(self):
        return "{}/work".format(self.topdir)

    def taskrelpath(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return "tasks/%s" % task_id


DEFAULT_ANNOTATIONS = {
    'repositories': {
        'unique': ['foo/bar:baz'],
        'primary': ['foo/bar:spam'],
    }
}


def mock_store_metadata_results(workflow, annotations=None):
    annotations = DEFAULT_ANNOTATIONS if annotations is None else annotations
    result = {}
    if annotations:
        result['annotations'] = {key: json.dumps(value) for key, value in annotations.items()}
    workflow.data.exit_results[StoreMetadataPlugin.key] = result


@pytest.mark.parametrize(('address', 'valid'), [
    ('me@example.com', True),
    ('me1@example.com', True),
    ('me+@example.com', True),
    ('me_@example.com', True),
    ('me-@example.com', True),
    ('me.me@example.com', True),
    ('me@www-1.example.com', True),
    (None, None),
    ('', None),
    ('invalid', None),
    ('me@example', None),
    ('me@@example.com', None),
    ('me/me@example.com', None),
    ('1me@example.com', None),
    ('me@www/example.com', None),
    ('me@www_example.com', None),
    ('me@www+example.com', None),
])
def test_valid_address(address, valid):
    assert validate_address(address) == valid


class TestSendMailPlugin(object):
    def test_fails_with_unknown_states(self, workflow):
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.spam.com',
        }

        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow,
                           smtp_host='smtp.bar.com', from_address='foo@bar.com',
                           send_on=['unknown_state', MS])
        with pytest.raises(PluginFailedException) as e:
            p.run()
        assert str(e.value) == 'Unknown state(s) "unknown_state" for sendmail plugin'

    @pytest.mark.parametrize('success, manual_canceled, send_on, expected', [
        # make sure that right combinations only succeed for the specific state
        (True, False, [MS], True),
        (False, True, [MS], False),
        (False, False, [MS], False),
        (True, False, [MF], False),
        (False, True, [MF], True),
        (False, False, [MF], True),
        (True, False, [MC], False),
        (False, True, [MC], True),
        (False, False, [MC], False),
        (True, False, [MS, MF], True),
        (False, True, [MS, MF], True),
        (False, False, [MS, MF], True),
    ])
    def test_should_send(self, success, manual_canceled, send_on, expected, workflow):
        kwargs = {
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'send_on': send_on,
        }

        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.spam.com',
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)
        assert p._should_send(success, manual_canceled) == expected

    @pytest.mark.parametrize(('has_kerberos',
                              'email_domain', 'expected_email'), [
        (True, None, "baz@bar.com"),
        (False, 'example.com', "baz@example.com"),
        (False, None, ""),
        (False, 'example.com', ""),
    ])
    def test_get_email_from_koji_obj(self, has_kerberos,
                                     email_domain, expected_email,
                                     workflow):
        session = MockedClientSession('', has_kerberos=has_kerberos)
        flexmock(koji, ClientSession=lambda hub, opts: session)

        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'domain': email_domain,
        }

        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, email_domain=email_domain)
        koji_task_owner = get_koji_task_owner(p.session, p.koji_task_id)

        try:
            found_email = p._get_email_from_koji_obj(koji_task_owner)
            assert expected_email == found_email
        except RuntimeError as exc:
            if not email_domain:
                assert str(exc) == "Empty email_domain specified"
            else:
                assert str(exc) == "Koji task owner name is missing"

    @pytest.mark.parametrize(('additional_addresses', 'expected_receivers'), [
        ('', None),
        ([], None),
        ([''], []),
        (['', ''], []),
        (['not/me@example.com'], []),
        (['me@example.com'], ['me@example.com']),
        (['me@example.com', 'me@example.com'], ['me@example.com']),
        (['me@example.com', '', 'me@example.com'], ['me@example.com']),
        (['not/me@example.com', 'me@example.com'], ['me@example.com']),
        (['me@example.com', 'us@example.com'], ['me@example.com', 'us@example.com']),
        (['not/me@example.com', '', 'me@example.com', 'us@example.com'],
         ['me@example.com', 'us@example.com']),
    ])
    def test_get_receiver_list(self, additional_addresses, expected_receivers, workflow):
        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo('https://koji')

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'additional_addresses': additional_addresses
        }

        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': False,
            'send_to_pkg_owner': False,
            'additional_addresses': additional_addresses
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url=None, root_url='https://koji/',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)
        if expected_receivers is not None:
            assert sorted(expected_receivers) == sorted(p._get_receivers_list())
        else:
            with pytest.raises(RuntimeError) as ex:
                p._get_receivers_list()
                assert str(ex.value) == 'No recipients found'

    @pytest.mark.parametrize('success', (True, False))
    @pytest.mark.parametrize(('has_store_metadata_results', 'annotations', 'has_repositories',
                              'expect_error'), [
        (True, True, True, False),
        (True, True, False, False),
        (True, False, False, True),
        (False, False, False, True)
    ])
    @pytest.mark.parametrize('koji_integration', (True, False))
    @pytest.mark.parametrize(('manual_cancel', 'to_koji_submitter'), [
        (True, True),
        (False, True),
        (True, False),
        (False, False),
    ])
    def test_render_mail(self, workflow, source_dir,
                         manual_cancel, to_koji_submitter,
                         koji_integration, success, has_store_metadata_results,
                         annotations, has_repositories, expect_error):
        git_source_url = 'git_source_url'
        git_source_ref = '123423431234123'
        VcsInfo = namedtuple('VcsInfo', ['vcs_type', 'vcs_url', 'vcs_ref'])
        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo('https://koji')
        (flexmock(pathinfo)
            .should_receive('work')
            .and_raise(RuntimeError, "xyz"))

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': to_koji_submitter,
            'to_koji_pkgowner': False
        }

        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID
        workflow.user_params['koji_task_id'] = MOCK_KOJI_TASK_ID

        dockerfile = source_dir / DOCKERFILE_FILENAME
        dockerfile.write_text(MOCK_DOCKERFILE, "utf-8")
        flexmock(workflow, df_path=str(dockerfile))

        flexmock(workflow.source, get_vcs_info=VcsInfo(vcs_type='git',
                                                       vcs_url=git_source_url,
                                                       vcs_ref=git_source_ref))

        workflow.data.build_canceled = manual_cancel

        if has_store_metadata_results:
            if annotations:
                if has_repositories:
                    mock_store_metadata_results(workflow)
                else:
                    mock_store_metadata_results(workflow, {'repositories': {}})
            else:
                mock_store_metadata_results(workflow, {})

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': to_koji_submitter,
            'send_to_pkg_owner': False,
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow,
                                 hub_url='/' if koji_integration else None,
                                 root_url='https://koji/',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)

        # Submitter is updated in _get_receivers_list
        try:
            p._get_receivers_list()
        except RuntimeError as ex:
            # Only valid exception is a RuntimeError when there are no
            # recipients available
            assert str(ex) == 'No recipients found'

        if expect_error:
            with pytest.raises(ValueError):
                p._render_mail(success, manual_cancel)
            return

        subject, body, logs = p._render_mail(success, manual_cancel)

        if manual_cancel:
            status = 'Canceled'
            assert not logs
        elif success:
            status = 'Succeeded'
            assert not logs
        else:
            status = 'Failed'

        if has_repositories:
            exp_subject = '%s building image foo/bar' % status
            exp_body = [
                'Image Name: foo/bar',
                'Repositories: ',
                '    foo/bar:baz',
                '    foo/bar:spam',
            ]
        else:
            exp_subject = '%s building image %s' % (status, MOCK_NAME_LABEL)
            exp_body = [
                'Image Name: ' + MOCK_NAME_LABEL,
                'Repositories: ',
            ]

        common_body = [
            'Status: ' + status,
            'Submitted by: ',
            'Task id: ' + str(MOCK_KOJI_TASK_ID),
            'Source url: ' + git_source_url,
            'Source ref: ' + git_source_ref,
        ]
        exp_body.extend(common_body)

        if koji_integration and to_koji_submitter:
            exp_body[-4] += MOCK_KOJI_SUBMITTER_EMAIL
        else:
            exp_body[-4] += SendMailPlugin.DEFAULT_SUBMITTER

        if koji_integration:
            exp_body.insert(-2, "Logs: <not available>")

        assert subject == exp_subject
        assert body == '\n'.join(exp_body)

    @pytest.mark.parametrize('error_type', [
        TypeError,
        OsbsException, 'unable to get build logs from OSBS',
    ])
    def test_failed_logs(self, workflow, source_dir, error_type):
        # just test a random combination of the method inputs and hope it's ok for other
        #   combinations
        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo('https://koji')

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': False,
        }

        mock_store_metadata_results(workflow)
        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID

        dockerfile = source_dir / DOCKERFILE_FILENAME
        dockerfile.write_text(MOCK_DOCKERFILE, "utf-8")
        flexmock(workflow, df_path=str(dockerfile))

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': True,
            'send_to_pkg_owner': False,
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)
        _, _, fail_logs = p._render_mail(False, False)
        assert not fail_logs

    @pytest.mark.parametrize(('has_addit_address', 'to_koji_submitter',
                              'to_koji_pkgowner', 'expected_receivers'), [
            (True, True, True,
                [MOCK_ADDITIONAL_EMAIL, MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, True, False, [MOCK_ADDITIONAL_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, True, [MOCK_ADDITIONAL_EMAIL, MOCK_KOJI_OWNER_EMAIL]),
            (True, False, False, [MOCK_ADDITIONAL_EMAIL]),
            (False, True, True, [MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (False, True, False, [MOCK_KOJI_SUBMITTER_EMAIL]),
            (False, False, True, [MOCK_KOJI_OWNER_EMAIL]),
            (False, False, False, []),
        ])
    def test_recepients_from_koji(self, has_addit_address, to_koji_submitter,
                                  to_koji_pkgowner, expected_receivers,
                                  workflow):
        session = MockedClientSession('', has_kerberos=True)
        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': to_koji_submitter,
            'to_koji_pkgowner': to_koji_pkgowner,
            'email_domain': MOCK_EMAIL_DOMAIN
        }
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': to_koji_submitter,
            'send_to_pkg_owner': to_koji_pkgowner,
            'domain': MOCK_EMAIL_DOMAIN,
        }
        if has_addit_address:
            kwargs['additional_addresses'] = [MOCK_ADDITIONAL_EMAIL]
            smtp_map['additional_addresses'] = [MOCK_ADDITIONAL_EMAIL]

        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID
        workflow.user_params['koji_task_id'] = MOCK_KOJI_TASK_ID
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)

        if not expected_receivers:
            with pytest.raises(RuntimeError):
                p._get_receivers_list()
        else:
            receivers = p._get_receivers_list()
            assert sorted(receivers) == sorted(expected_receivers)

    @pytest.mark.parametrize('has_kerberos, expected_receivers', [
        (True, [MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
        (False, [MOCK_KOJI_OWNER_GENERATED, MOCK_KOJI_SUBMITTER_GENERATED])])
    def test_generated_email(self, has_kerberos, expected_receivers, workflow):
        session = MockedClientSession('', has_kerberos=has_kerberos)
        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': True,
            'email_domain': MOCK_EMAIL_DOMAIN
        }

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': True,
            'send_to_pkg_owner': True,
            'domain': MOCK_EMAIL_DOMAIN,
        }

        workflow.data.postbuild_results[KojiImportPlugin.key] = MOCK_KOJI_BUILD_ID
        workflow.user_params['koji_task_id'] = MOCK_KOJI_TASK_ID
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)
        receivers = p._get_receivers_list()
        assert sorted(receivers) == sorted(expected_receivers)

        if has_kerberos:
            assert p.submitter == MOCK_KOJI_SUBMITTER_EMAIL
        else:
            assert p.submitter == MOCK_KOJI_SUBMITTER_GENERATED

    @pytest.mark.parametrize('exception_location, expected_receivers', [
        ('koji_connection', []),
        ('submitter', [MOCK_KOJI_OWNER_EMAIL]),
        ('empty_submitter', [MOCK_KOJI_OWNER_EMAIL]),
        ('owner', [MOCK_KOJI_SUBMITTER_EMAIL]),
        ('empty_owner', [MOCK_KOJI_SUBMITTER_EMAIL]),
        ('empty_email_domain', [])])
    def test_koji_recepients_exception(self, exception_location, expected_receivers, workflow):
        if exception_location == 'empty_owner':
            koji_build_id = None
        else:
            koji_build_id = MOCK_KOJI_BUILD_ID

        has_kerberos = exception_location != 'empty_email_domain'
        session = MockedClientSession('', has_kerberos=has_kerberos)
        if exception_location == 'koji_connection':
            (flexmock(session)
                .should_receive('ssl_login')
                .and_raise(RuntimeError, "xyz"))
        elif exception_location == 'submitter':
            (flexmock(session)
                .should_receive('getTaskInfo')
                .and_raise(RuntimeError, "xyz"))
        elif exception_location == 'owner':
            (flexmock(session)
                .should_receive('getPackageConfig')
                .and_raise(RuntimeError, "xyz"))

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': True
        }
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': True,
            'send_to_pkg_owner': True,
        }
        if exception_location != 'empty_email_domain':
            kwargs['email_domain'] = MOCK_EMAIL_DOMAIN
            smtp_map['domain'] = MOCK_EMAIL_DOMAIN

        workflow.data.postbuild_results[KojiImportPlugin.key] = koji_build_id
        if exception_location == 'empty_submitter':
            workflow.user_params['koji_task_id'] = None
        else:
            workflow.user_params['koji_task_id'] = MOCK_KOJI_TASK_ID
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, **kwargs)
        if not expected_receivers:
            with pytest.raises(RuntimeError):
                p._get_receivers_list()
        else:
            receivers = p._get_receivers_list()
            assert sorted(receivers) == sorted(expected_receivers)

    @pytest.mark.parametrize('throws_exception', [False, True])
    def test_send_mail(self, throws_exception, workflow):
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow, from_address='foo@bar.com', smtp_host='smtp.spam.com')

        class SMTP(object):
            def sendmail(self, from_addr, to, msg):
                pass

            def quit(self):
                pass

        smtp_inst = SMTP()
        flexmock(smtplib).should_receive('SMTP').and_return(smtp_inst)
        sendmail_chain = (flexmock(smtp_inst).should_receive('sendmail').
                          with_args('foo@bar.com', ['spam@spam.com'], str))
        if throws_exception:
            sendmail_chain.and_raise(smtplib.SMTPException, "foo")
        flexmock(smtp_inst).should_receive('quit')

        if throws_exception:
            with pytest.raises(SMTPException) as e:
                p._send_mail(['spam@spam.com'], 'subject', 'body')
            assert str(e.value) == 'foo'
        else:
            p._send_mail(['spam@spam.com'], 'subject', 'body')

    def test_run_ok(self, tmpdir, workflow, source_dir):
        receivers = ['foo@bar.com', 'x@y.com']

        dockerfile = source_dir / DOCKERFILE_FILENAME
        dockerfile.write_text(MOCK_DOCKERFILE, "utf-8")
        flexmock(workflow, df_path=str(dockerfile))

        mock_store_metadata_results(workflow)

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[])

        (flexmock(p).should_receive('_should_send')
         .with_args(False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(p).should_receive('_send_mail').with_args(receivers,
                                                           str, str, None)

        p.run()

    def test_run_ok_and_send(self, workflow):
        class SMTP(object):
            def sendmail(self, from_addr, to, msg):
                pass

            def quit(self):
                pass

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.spam.com',
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        receivers = ['foo@bar.com', 'x@y.com']
        p = SendMailPlugin(workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[])

        (flexmock(p).should_receive('_should_send')
            .with_args(False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(p).should_receive('_get_image_name_and_repos').and_return(('foobar',
                                                                           ['foo/bar:baz',
                                                                            'foo/bar:spam']))

        smtp_inst = SMTP()
        flexmock(smtplib).should_receive('SMTP').and_return(smtp_inst)
        p.run()

    def test_run_fails_to_obtain_receivers(self, workflow):
        error_addresses = ['error@address.com']
        mock_store_metadata_results(workflow)

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'error_addresses': ['error@address.com'],
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[], error_addresses=error_addresses)

        (flexmock(p).should_receive('_should_send')
            .with_args(False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_raise(RuntimeError())
        flexmock(p).should_receive('_get_image_name_and_repos').and_return(('foobar',
                                                                           ['foo/bar:baz',
                                                                            'foo/bar:spam']))
        flexmock(p).should_receive('_send_mail').with_args(error_addresses, str, str, None)

        p.run()

    def test_run_invalid_receivers(self, caplog, workflow):
        error_addresses = ['error@address.com']

        mock_store_metadata_results(workflow)

        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'error_addresses': ['error@address.com'],
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[], error_addresses=error_addresses)

        (flexmock(p).should_receive('_should_send')
            .with_args(False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return([])
        flexmock(p).should_receive('_get_image_name_and_repos').and_return(('foobar',
                                                                           ['foo/bar:baz',
                                                                            'foo/bar:spam']))
        p.run()
        assert 'no valid addresses in requested addresses. Doing nothing' in caplog.text

    def test_run_does_nothing_if_conditions_not_met(self, workflow):
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.spam.com',
        }
        rcm = {'version': 1, 'smtp': smtp_map, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[MS])

        (flexmock(p).should_receive('_should_send')
            .with_args(False, False).and_return(False))
        flexmock(p).should_receive('_get_receivers_list').times(0)
        flexmock(p).should_receive('_send_mail').times(0)

        p.run()

    def test_skip_plugin(self, caplog, workflow):
        rcm = {'version': 1, 'openshift': {'url': 'https://something.com'}}
        workflow.conf = Configuration(raw_config=rcm)
        add_koji_map_in_workflow(workflow, hub_url='/', root_url='',
                                 ssl_certs_dir='/certs')

        p = SendMailPlugin(workflow)
        p.run()
        log_msg = 'no smtp configuration, skipping plugin'
        assert log_msg in caplog.text
