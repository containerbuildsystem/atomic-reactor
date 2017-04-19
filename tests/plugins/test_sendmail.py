import os
import smtplib

from flexmock import flexmock
import pytest
import six
import json

try:
    import koji
except ImportError:
    import inspect
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji


from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.exit_sendmail import SendMailPlugin
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor import util
from smtplib import SMTPException

MS, MF = SendMailPlugin.MANUAL_SUCCESS, SendMailPlugin.MANUAL_FAIL
AS, AF = SendMailPlugin.AUTO_SUCCESS, SendMailPlugin.AUTO_FAIL
MC, AC = SendMailPlugin.MANUAL_CANCELED, SendMailPlugin.AUTO_CANCELED

MOCK_EMAIL_DOMAIN = "domain.com"
MOCK_KOJI_TASK_ID = 12345
MOCK_KOJI_BUILD_ID = 98765
MOCK_KOJI_PACKAGE_ID = 123
MOCK_KOJI_TAG_ID = 456
MOCK_KOJI_OWNER_ID = 789
MOCK_KOJI_OWNER_NAME = "foo"
MOCK_KOJI_OWNER_EMAIL = "foo@bar.com"
MOCK_KOJI_OWNER_GENERATED = "@".join([MOCK_KOJI_OWNER_NAME, MOCK_EMAIL_DOMAIN])
MOCK_KOJI_SUBMITTER_ID = 123456
MOCK_KOJI_SUBMITTER_NAME = "baz"
MOCK_KOJI_SUBMITTER_EMAIL = "baz@bar.com"
MOCK_KOJI_SUBMITTER_GENERATED = "@".join([MOCK_KOJI_SUBMITTER_NAME, MOCK_EMAIL_DOMAIN])
MOCK_ADDITIONAL_EMAIL = "spam@bar.com"


class MockedClientSession(object):
    def __init__(self, hub, opts=None, has_kerberos=True):
        self.has_kerberos = has_kerberos

    def krb_login(self, principal=None, keytab=None, proxyuser=None):
        raise RuntimeError('No certificates provided')

    def ssl_login(self, cert, ca, serverca, proxyuser=None):
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

        else:
            assert False, "Don't know user with id %s" % user_id

    def getTaskInfo(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return {"owner": MOCK_KOJI_SUBMITTER_ID}

    def listTaskOutput(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return ["openshift-final.log", "build.log"]


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def work(self):
        return "https://koji/work/"

    def taskrelpath(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return "tasks/%s" % task_id


class TestSendMailPlugin(object):
    def test_fails_with_unknown_states(self):
        p = SendMailPlugin(None, None,
                           smtp_host='smtp.bar.com', from_address='foo@bar.com',
                           send_on=['unknown_state', MS])
        with pytest.raises(PluginFailedException) as e:
            p.run()
        assert str(e.value) == 'Unknown state(s) "unknown_state" for sendmail plugin'

    @pytest.mark.parametrize('rebuild, success, auto_canceled, manual_canceled, send_on, expected', [
        # make sure that right combinations only succeed for the specific state
        (False, True, False, False, [MS], True),
        (False, True, False, True, [MS], True),
        (False, True, False, False, [MF, AS, AF, AC], False),
        (False, True, False, True, [MF, AS, AF, AC], False),
        (False, False, False, False, [MF], True),
        (False, False, False, True, [MF], True),
        (False, False, False, False, [MS, AS, AF, AC], False),
        (False, False, False, True, [MS, AS, AF, AC], False),
        (False, False, True, True, [MC], True),
        (False, True, True, True, [MC], True),
        (False, True, False, True, [MC], True),
        (False, True, False, False, [MC], False),
        (True, True, False, False, [AS], True),
        (True, True, False, False, [MS, MF, AF, AC], False),
        (True, False, False, False, [AF], True),
        (True, False, False, False, [MS, MF, AS, AC], False),
        (True, False, True, True, [AC], True),
        # auto_fail would also give us True in this case
        (True, False, True, True, [MS, MF, AS], False),
        # also make sure that a random combination of more plugins works ok
        (True, False, False, False, [AF, MS], True)
    ])
    def test_should_send(self, rebuild, success, auto_canceled, manual_canceled, send_on, expected):
        p = SendMailPlugin(None, None,
                           smtp_host='smtp.bar.com', from_address='foo@bar.com',
                           send_on=send_on)
        assert p._should_send(rebuild, success, auto_canceled, manual_canceled) == expected

    @pytest.mark.parametrize(('autorebuild', 'auto_cancel', 'manual_cancel',
                              'to_koji_submitter', 'has_koji_logs'), [
        (True, False, False, True, True),
        (True, True, False, True, True),
        (True, False, True, True, True),
        (True, False, False, True, False),
        (True, True, False, True, False),
        (True, False, True, True, False),
        (False, False, False, True, True),
        (False, True, False, True, True),
        (False, False, True, True, True),
        (False, False, False, True, False),
        (False, True, False, True, False),
        (False, False, True, True, False),
        (True, False, False, False, True),
        (True, True, False, False, True),
        (True, False, True, False, True),
        (True, False, False, False, False),
        (True, True, False, False, False),
        (True, False, True, False, False),
        (False, False, False, False, True),
        (False, True, False, False, True),
        (False, False, True, False, True),
        (False, False, False, False, False),
        (False, True, False, False, False),
        (False, False, True, False, False),
    ])
    def test_render_mail(self, monkeypatch, autorebuild, auto_cancel, manual_cancel,
                         to_koji_submitter, has_koji_logs):
        # just test a random combination of the method inputs and hope it's ok for other
        #   combinations
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            autorebuild_canceled = auto_cancel
            build_canceled = manual_cancel
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
            }
        }))

        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo()
        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': to_koji_submitter,
            'to_koji_pkgowner': False,
            'koji_hub': '',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }

        if not has_koji_logs:
            (flexmock(pathinfo)
                .should_receive('work')
                .and_raise(RuntimeError, "xyz"))

        p = SendMailPlugin(None, WF(), **kwargs)
        subject, body = p._render_mail(autorebuild, False, auto_cancel, manual_cancel)
        # Submitter is updated in _get_receivers_list
        try:
            p._get_receivers_list()
        except Exception:
            pass

        if to_koji_submitter:
            subject, body = p._render_mail(autorebuild, False, auto_cancel, manual_cancel)

        status = 'Canceled' if auto_cancel or manual_cancel else 'Failed'

        exp_subject = '%s building image foo/bar:baz' % status
        exp_body = [
            'Image: foo/bar:baz',
            'Status: ' + status,
            'Submitted by: ',
            'Logs: '
        ]
        if autorebuild:
            exp_body[2] += '<autorebuild>'
        elif to_koji_submitter:
            exp_body[2] += MOCK_KOJI_SUBMITTER_EMAIL
        else:
            exp_body[2] += SendMailPlugin.DEFAULT_SUBMITTER

        if has_koji_logs:
            exp_body[3] += "https://koji/work/tasks/12345"
        else:
            exp_body[3] += "https://something.com/builds/blablabla/log"

        assert subject == exp_subject
        assert body == '\n'.join(exp_body)

    @pytest.mark.parametrize(
        'has_koji_config, has_addit_address, to_koji_submitter, to_koji_pkgowner, expected_receivers', [
            (True, True, True, True,
                [MOCK_ADDITIONAL_EMAIL, MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, True, True, [MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, True, False, [MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, False, True, [MOCK_KOJI_OWNER_EMAIL]),
            (True, True, False, False, [MOCK_ADDITIONAL_EMAIL]),
            (True, False, False, False, []),
            (False, False, False, False, []),
            (False, True, False, True, [MOCK_ADDITIONAL_EMAIL]),
            (False, True, True, False, [MOCK_ADDITIONAL_EMAIL]),
        ])
    def test_recepients_from_koji(self, monkeypatch,
                                  has_addit_address,
                                  has_koji_config, to_koji_submitter, to_koji_pkgowner,
                                  expected_receivers):
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
            }
        }))

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
        if has_addit_address:
            kwargs['additional_addresses'] = [MOCK_ADDITIONAL_EMAIL]

        if has_koji_config:
            kwargs['koji_hub'] = ''
            kwargs['koji_proxyuser'] = None
            kwargs['koji_ssl_certs_dir'] = '/certs'
            kwargs['koji_krb_principal'] = None
            kwargs['koji_krb_keytab'] = None

        p = SendMailPlugin(None, WF(), **kwargs)

        if not expected_receivers:
            with pytest.raises(RuntimeError):
                p._get_receivers_list()
        else:
            receivers = p._get_receivers_list()
            assert sorted(receivers) == sorted(expected_receivers)

    @pytest.mark.parametrize('has_kerberos, expected_receivers', [
        (True, [MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
        (False, [MOCK_KOJI_OWNER_GENERATED, MOCK_KOJI_SUBMITTER_GENERATED])])
    def test_generated_email(self, monkeypatch, has_kerberos, expected_receivers):
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
            }
        }))

        session = MockedClientSession('', has_kerberos=has_kerberos)
        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': True,
            'email_domain': MOCK_EMAIL_DOMAIN,
            'koji_hub': '',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }
        p = SendMailPlugin(None, WF(), **kwargs)
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
    def test_koji_recepients_exception(self, monkeypatch, exception_location, expected_receivers):
        class TagConf(object):
            unique_images = []

        if exception_location == 'empty_owner':
            koji_build_id = None
        else:
            koji_build_id = MOCK_KOJI_BUILD_ID

        if exception_location == 'empty_submitter':
            koji_task_id = None
        else:
            koji_task_id = MOCK_KOJI_TASK_ID

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: koji_build_id
            }

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': koji_task_id,
                },
            }
        }))

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
            'to_koji_pkgowner': True,
            'koji_hub': '',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }
        if exception_location != 'empty_email_domain':
            kwargs['email_domain'] = MOCK_EMAIL_DOMAIN
        p = SendMailPlugin(None, WF(), **kwargs)
        if not expected_receivers:
            with pytest.raises(RuntimeError):
                p._get_receivers_list()
        else:
            receivers = p._get_receivers_list()
            assert sorted(receivers) == sorted(expected_receivers)

    @pytest.mark.parametrize('throws_exception', [False, True])
    def test_send_mail(self, throws_exception):
        p = SendMailPlugin(None, None, from_address='foo@bar.com', smtp_host='smtp.spam.com')

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

    def test_run_ok(self):
        class TagConf(object):
            unique_images = []

        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            tag_conf = TagConf()

        receivers = ['foo@bar.com', 'x@y.com']
        p = SendMailPlugin(None, WF(),
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[AF])

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(p).should_receive('_send_mail').with_args(receivers, six.text_type, six.text_type)

        p.run()

    def test_run_fails_to_obtain_receivers(self):
        class TagConf(object):
            unique_images = []

        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            tag_conf = TagConf()

        error_addresses = ['error@address.com']
        p = SendMailPlugin(None, WF(),
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[AF], error_addresses=error_addresses)

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_raise(RuntimeError())
        flexmock(p).should_receive('_send_mail').with_args(error_addresses, six.text_type,
                                                           six.text_type)

        p.run()

    def test_run_does_nothing_if_conditions_not_met(self):
        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True

        p = SendMailPlugin(None, WF(),
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[MS])

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(False))
        flexmock(p).should_receive('_get_receivers_list').times(0)
        flexmock(p).should_receive('_send_mail').times(0)

        p.run()
