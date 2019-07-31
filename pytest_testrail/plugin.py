"""Reference: http://docs.gurock.com/testrail-api2/reference-statuses"""
from datetime import datetime
from operator import itemgetter
from typing import List, Dict, Any

import pytest
import re
import warnings

TESTRAIL_TEST_STATUS = {"passed": 1, "blocked": 2, "untested": 3, "retest": 4, "failed": 5}

PYTEST_TO_TESTRAIL_STATUS = {
    "passed": TESTRAIL_TEST_STATUS["passed"],
    "failed": TESTRAIL_TEST_STATUS["failed"],
    "skipped": TESTRAIL_TEST_STATUS["blocked"],
}

DT_FORMAT = '%d-%m-%Y %H:%M:%S'

TR_PREFIX = 'testrail'

ADD_RESULTS_URL = 'add_results_for_cases/{}'
ADD_RESULT_URL = 'add_result_for_case'
ADD_TESTRUN_URL = 'add_run/{}'
CLOSE_TESTRUN_URL = 'close_run/{}'
CLOSE_TESTPLAN_URL = 'close_plan/{}'
GET_TESTRUN_URL = 'get_run/{}'
GET_TESTPLAN_URL = 'get_plan/{}'
GET_TESTS_URL = 'get_tests/{}'

COMMENT_SIZE_LIMIT = 4000


class TestsNotFoundException(Exception):
    pass


class DeprecatedTestDecorator(DeprecationWarning):
    pass


warnings.simplefilter(action='once', category=DeprecatedTestDecorator, lineno=0)


class pytestrail:
    @staticmethod
    def case(*ids):
        """Decorator to mark tests with test case ids. i.e. @pytestrail.case('C123', 'C12345')."""
        return pytest.mark.testrail(ids=ids)


def converter(text, encoding='utf-8'):
    return str(bytes(text, 'utf-8'), encoding)


def get_test_outcome(outcome: str) -> int:
    """Return numerical value of test outcome."""
    return PYTEST_TO_TESTRAIL_STATUS[outcome]


def clean_test_ids(test_ids: List) -> List[int]:
    """Clean pytest marker containing testrail testcase ids."""
    return [int(re.search('(?P<test_id>[0-9]+$)', test_id).groupdict().get('test_id')) for test_id in test_ids]


def get_testrail_keys(items):
    """Return list of Pytest nodes and TestRail ids from pytest markers."""
    test_case_ids = []
    for item in items:
        if item.get_closest_marker(TR_PREFIX):
            test_case_ids.append((item, clean_test_ids(item.get_closest_marker(TR_PREFIX).kwargs.get('ids'))))
    return test_case_ids


# noinspection PyUnusedLocal
class PyTestRailPlugin(object):
    def __init__(self, client, assign_user_id, project_id, suite_id, include_all, cert_check, tr_name, run_id=0,
                 plan_id=0, version='', close_on_complete=False, publish_blocked=True, skip_missing=False):
        self.assign_user_id = assign_user_id
        self.cert_check = cert_check
        self.client = client
        self.project_id = project_id
        self.results = []
        self.suite_id = suite_id
        self.include_all = include_all
        self.testrun_name = tr_name
        self.testrun_id = run_id
        self.testplan_id = plan_id
        self.version = version
        self.close_on_complete = close_on_complete
        self.publish_blocked = publish_blocked
        self.skip_missing = skip_missing

    # pytest hooks

    def pytest_report_header(self, config, startdir):
        """ Add extra-info in header """
        prefix = 'pytest-testrail: '
        if self.testplan_id:
            message = f'existing testplan #{self.testplan_id} selected'
        elif self.testrun_id:
            message = f'existing testrun #{self.testrun_id} selected'
        else:
            message = 'a new testrun will be created'
        return f'{prefix}{message}'

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        items_with_tr_keys = get_testrail_keys(items)
        tr_keys = [case_id for item in items_with_tr_keys for case_id in item[1]]

        if self.testplan_id and self.is_testplan_available:
            self.testrun_id = 0
        elif self.testrun_id and self.is_testrun_available:
            self.testplan_id = 0
            if self.skip_missing:
                tests_list = [
                    test.get('case_id') for test in self.get_tests(self.testrun_id)
                ]
                for item, case_id in items_with_tr_keys:
                    if not set(case_id).intersection(set(tests_list)):
                        mark = pytest.mark.skip('Test is not present in testrun.')
                        item.add_marker(mark)
        else:
            if self.testrun_name is None:
                self.testrun_name = f'Automated Run {datetime.utcnow().strftime(DT_FORMAT)}'

            self.create_test_run(
                self.assign_user_id,
                self.project_id,
                self.suite_id,
                self.include_all,
                self.testrun_name,
                tr_keys
            )

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        """Collect result and associated testcases (TestRail) of an execution."""
        outcome = yield
        rep = outcome.get_result()
        if item.get_closest_marker(TR_PREFIX):
            test_case_ids = item.get_closest_marker(TR_PREFIX).kwargs.get('ids')

            if rep.when == 'call' and test_case_ids:
                self.add_result(
                    clean_test_ids(test_case_ids),
                    get_test_outcome(outcome.get_result().outcome),
                    comment=rep.longrepr,
                    duration=rep.duration
                )

    def pytest_sessionfinish(self, session, exitstatus):
        """Publish results in TestRail."""
        print(f'[{TR_PREFIX}] Start publishing')
        if self.results:
            tests_list = [str(result['case_id']) for result in self.results]
            print(f'[{TR_PREFIX}] Testcases to publish: {", ".join(tests_list)}')

            if self.testrun_id:
                self.publish_results(self.testrun_id)
            elif self.testplan_id:
                testruns = self.get_available_testruns(self.testplan_id)
                print(f'[{TR_PREFIX}] Testruns to update: {", ".join([str(elt) for elt in testruns])}')
                for testrun_id in testruns:
                    self.publish_results(testrun_id)
            else:
                print(f'[{TR_PREFIX}] No data published')

            if self.close_on_complete and self.testrun_id:
                self.close_test_run(self.testrun_id)
            elif self.close_on_complete and self.testplan_id:
                self.close_test_plan(self.testplan_id)
        print(f'[{TR_PREFIX}] End publishing')

    # plugin
    def add_result(self, test_ids, status, comment='', duration=0):
        """
        Add a new result to results dict to be submitted at the end.

        :param list test_ids: list of test_ids.
        :param int status: status code of test (pass or fail).
        :param comment: None or a failure representation.
        :param duration: Time it took to run just the test.
        """
        for test_id in test_ids:
            data = {
                'case_id': test_id,
                'status_id': status,
                'comment': comment,
                'duration': duration
            }
            self.results.append(data)

    def publish_results(self, testrun_id: int):
        """Add results one by one to improve errors handling."""
        # Results are sorted by 'case_id' and by 'status_id' (worst result at the end)
        self.results.sort(key=itemgetter('status_id'))
        self.results.sort(key=itemgetter('case_id'))

        if not self.publish_blocked:  # Manage case of "blocked" testcases.
            print(f'[{TR_PREFIX}] Option "Don\'t publish blocked testcases" activated')
            blocked_tests_list = [test.get('case_id') for test in self.get_tests(testrun_id)
                                  if test.get('status_id') == TESTRAIL_TEST_STATUS["blocked"]]
            print(f'[{TR_PREFIX}] Blocked testcases excluded: {", ".join(str(elt) for elt in blocked_tests_list)}')
            self.results = [result for result in self.results if result.get('case_id') not in blocked_tests_list]
        if self.include_all:  # Prompt enabling include all test cases from test suite when creating test run.
            print(f'[{TR_PREFIX}] Option "Include all testcases from test suite for test run" activated')

        for result in self.results:
            self.publish_result(result, testrun_id)

    def publish_result(self, result: Dict[str, Any], testrun_id: int):
        entry = {'status_id': result['status_id'], 'case_id': result['case_id']}
        if self.version:
            entry['version'] = self.version
        comment = result.get('comment', '')
        if comment:
            entry['comment'] = self.__formatted_comment(comment)
        duration = result.get('duration')
        if duration:
            duration = 1 if (duration < 1) else int(round(duration))  # TestRail API doesn't manage milliseconds
            entry['elapsed'] = f'{duration}s'

        response = self.client.send_post(
            f'{ADD_RESULT_URL}/{testrun_id}/{entry.get("case_id")}',
            entry,
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Info: Testcases not published for following reason: "{error}"')

    @staticmethod
    def __formatted_comment(comment: str) -> str:
        # Indent text to avoid string formatting by TestRail. Limit size of comment.
        _comment = u"# Pytest result: #\n"
        _comment += u'Log truncated\n...\n' if len(str(comment)) > COMMENT_SIZE_LIMIT else u''
        _comment += u"    " + converter(str(comment), "utf-8")[-COMMENT_SIZE_LIMIT:].replace('\n', '\n    ')
        return _comment

    def create_test_run(self, assign_user_id, project_id, suite_id, include_all, testrun_name, tr_keys):
        data = {
            'suite_id': suite_id,
            'name': testrun_name,
            'assignedto_id': assign_user_id,
            'include_all': include_all,
            'case_ids': tr_keys,
        }

        response = self.client.send_post(
            ADD_TESTRUN_URL.format(project_id),
            data,
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to create testrun: "{}"'.format(TR_PREFIX, error))
        else:
            self.testrun_id = response['id']
            print(f'[{TR_PREFIX}] New testrun created with name "{testrun_name}" and ID={self.testrun_id}')

    def close_test_run(self, testrun_id):
        """Close testrun."""
        response = self.client.send_post(
            CLOSE_TESTRUN_URL.format(testrun_id),
            data={},
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Failed to close test run: "{error}"')
        else:
            print(f'[{TR_PREFIX}] Test run with ID={self.testrun_id} was closed')

    def close_test_plan(self, testplan_id):
        """Close test plan."""
        response = self.client.send_post(
            CLOSE_TESTPLAN_URL.format(testplan_id),
            data={},
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Failed to close test plan: "{error}"')
        else:
            print(f'[{TR_PREFIX}] Test plan with ID={self.testplan_id} was closed')

    @property
    def is_testrun_available(self) -> bool:
        """Ask if testrun is available in TestRail."""
        response = self.client.send_get(
            GET_TESTRUN_URL.format(self.testrun_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Failed to retrieve testrun: "{error}"')
            return False
        return response['is_completed'] is False

    @property
    def is_testplan_available(self) -> bool:
        """Ask if testplan is available in TestRail."""
        response = self.client.send_get(
            GET_TESTPLAN_URL.format(self.testplan_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Failed to retrieve testplan: "{error}"')
            return False
        return response['is_completed'] is False

    def get_available_testruns(self, plan_id: int) -> List[int]:
        """
        :return: a list of available testruns associated to a testplan in TestRail.

        """
        testruns_list = []
        response = self.client.send_get(
            GET_TESTPLAN_URL.format(plan_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Failed to retrieve testplan: "{error}"')
        else:
            for entry in response['entries']:
                for run in entry['runs']:
                    if not run['is_completed']:
                        testruns_list.append(run['id'])
        return testruns_list

    def get_tests(self, run_id: int) -> List[Dict[str, Any]]:
        response = self.client.send_get(
            GET_TESTS_URL.format(run_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print(f'[{TR_PREFIX}] Failed to get tests: "{error}"')
            raise TestsNotFoundException
        return response
