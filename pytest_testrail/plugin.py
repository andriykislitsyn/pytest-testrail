"""Reference: http://docs.gurock.com/testrail-api2/reference-statuses"""
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
from queue import Queue
from threading import Thread
from typing import List, Dict, Any

import pytest


@dataclass
class URL:
    add_results: str = 'add_results_for_cases/{}'
    add_result: str = 'add_result_for_case'
    add_testrun: str = 'add_run/{}'
    close_testrun: str = 'close_run/{}'
    close_testplan: str = 'close_plan/{}'
    get_testrun: str = 'get_run/{}'
    get_testplan: str = 'get_plan/{}'
    get_tests: str = 'get_tests/{}'


class TestsNotFoundException(Exception):
    pass


class pytestrail:
    @staticmethod
    def case(*ids):
        """Decorator to mark tests with test case ids. i.e. @pytestrail.case('C123', 'C12345')."""
        return pytest.mark.testrail(ids=ids)


# noinspection PyUnusedLocal
class PyTestRailPlugin:
    test_status = {"passed": 1, "blocked": 2, "untested": 3, "retest": 4, "failed": 5}

    tr_prefix: str = 'testrail'
    time_format: str = '%d-%m-%Y %H:%M:%S'
    max_comment_size: int = 4000
    max_concurrent_requests: int = 60

    def __init__(self, client, assign_user_id, project_id, suite_id, include_all, cert_check, tr_name, run_id=0,
                 plan_id=0, version='', close_on_complete=False, publish_blocked=True, skip_missing=False):
        self.assign_user_id = assign_user_id
        self.cert_check: bool = cert_check
        self.client = client
        self.project_id = project_id
        self.results = []
        self.suite_id: int = suite_id
        self.include_all: bool = include_all
        self.testrun_name: str = tr_name
        self.testrun_id: int = run_id
        self.testplan_id: int = plan_id
        self.version = version
        self.close_on_complete: bool = close_on_complete
        self.publish_blocked: bool = publish_blocked
        self.skip_missing: bool = skip_missing

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
        items_with_tr_keys = self.get_testrail_keys(items)
        tr_keys = [case_id for item in items_with_tr_keys for case_id in item[1]]

        if self.testplan_id and self.is_testplan_available:
            self.testrun_id = 0
        elif self.testrun_id and self.is_testrun_available:
            self.testplan_id = 0
            if self.skip_missing:
                tests_list = [test.get('case_id') for test in self.get_tests(self.testrun_id)]
                for item, case_id in items_with_tr_keys:
                    if not set(case_id).intersection(set(tests_list)):
                        mark = pytest.mark.skip('Test is not present in testrun.')
                        item.add_marker(mark)
        else:
            if self.testrun_name is None:
                self.testrun_name = f'Automated Run {datetime.utcnow().strftime(self.time_format)}'

            self.create_test_run(
                self.assign_user_id,
                self.project_id,
                self.suite_id,
                self.include_all,
                self.testrun_name,
                tr_keys
            )

    def get_testrail_keys(self, items):
        """Return list of Pytest nodes and TestRail ids from pytest markers."""
        test_case_ids = []
        for item in items:
            if item.get_closest_marker(self.tr_prefix):
                test_case_ids.append(
                    (item, self.clean_test_ids(
                        item.get_closest_marker(self.tr_prefix).kwargs.get('ids'))
                     )
                )
        return test_case_ids

    @staticmethod
    def clean_test_ids(test_ids: List) -> List[int]:
        """Clean pytest marker containing testrail testcase ids."""
        return [int(re.search('(?P<test_id>[0-9]+$)', test_id).groupdict().get('test_id')) for test_id in test_ids]

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        """Collect result and associated testcases (TestRail) of an execution."""
        outcome = yield
        rep = outcome.get_result()
        if item.get_closest_marker(self.tr_prefix):
            test_case_ids = item.get_closest_marker(self.tr_prefix).kwargs.get('ids')

            if rep.when == 'call' and test_case_ids:
                self.add_result(
                    self.clean_test_ids(test_case_ids),
                    {"passed": 1, "failed": 5, "skipped": 2}.get(outcome.get_result().outcome),
                    comment=rep.longrepr,
                    duration=rep.duration
                )

    def pytest_sessionfinish(self, session, exitstatus):
        """Publish results in TestRail."""
        print(f'\n\n[{self.tr_prefix}] Start publishing')
        if self.results:
            tests_list = [str(result['case_id']) for result in self.results]
            print(f'[{self.tr_prefix}] Testcases to publish:')
            print(textwrap.fill(f', '.join(tests_list), 110))

            if self.testrun_id:
                self.publish_results(self.testrun_id)
            elif self.testplan_id:
                testruns = self.get_available_testruns(self.testplan_id)
                print(f'[{self.tr_prefix}] Testruns to update: {", ".join([str(elt) for elt in testruns])}')
                for testrun_id in testruns:
                    self.publish_results(testrun_id)
            else:
                print(f'[{self.tr_prefix}] No data published')

            if self.close_on_complete and self.testrun_id:
                self.close_test_run(self.testrun_id)
            elif self.close_on_complete and self.testplan_id:
                self.close_test_plan(self.testplan_id)
        print(f'[{self.tr_prefix}] End publishing')

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
        results = (self.__process_result(result) for result in self.results)
        if not self.publish_blocked:  # Manage case of "blocked" testcases.
            print(f'[{self.tr_prefix}] Option "Don\'t publish blocked testcases" activated')
            blocked_tests_list = [test.get('case_id') for test in self.get_tests(testrun_id)
                                  if test.get('status_id') == self.test_status["blocked"]]
            print(f'[{self.tr_prefix}] Blocked testcases excluded: {", ".join(str(elt) for elt in blocked_tests_list)}')
            results = (self.__process_result(r) for r in self.results if r.get('case_id') not in blocked_tests_list)
        if self.include_all:  # Prompt enabling include all test cases from test suite when creating test run.
            print(f'[{self.tr_prefix}] Option "Include all testcases from test suite for test run" activated')

        queue = Queue(self.max_concurrent_requests * 2)

        def do_work():
            while True:
                r = queue.get()
                self.publish_result(r, testrun_id)
                queue.task_done()

        for _ in range(self.max_concurrent_requests):
            thread = Thread(target=do_work)
            thread.daemon = True
            thread.start()

        for r in results:
            queue.put(r)
            queue.join()

    def __process_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
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
        return entry

    def __formatted_comment(self, comment: str) -> str:
        # Indent text to avoid string formatting by TestRail. Limit size of comment.
        _payload = str(comment)
        _comment = '# Pytest result: #\n'
        _comment += 'Log truncated\n...\n' if len(_payload) > self.max_comment_size else u''
        _comment += '    ' + _payload[-self.max_comment_size:].replace('\n', '\n    ')
        return _comment

    def publish_result(self, result: Dict[str, Any], testrun_id: int):
        response = self.client.send_post(
            f'{URL.add_result}/{testrun_id}/{result.get("case_id")}',
            result,
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            if error not in (
                    'No (active) test found for the run/case combination.',
            ):
                print(f'[{self.tr_prefix}] Info: Testcases not published for following reason: "{error}"')

    def create_test_run(self, assign_user_id, project_id, suite_id, include_all, testrun_name, tr_keys):
        data = {
            'suite_id': suite_id,
            'name': testrun_name,
            'assignedto_id': assign_user_id,
            'include_all': include_all,
            'case_ids': tr_keys,
        }
        response = self.client.send_post(URL.add_testrun.format(project_id), data, cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to create testrun: "{}"'.format(self.tr_prefix, error))
        else:
            self.testrun_id = response['id']
            print(f'[{self.tr_prefix}] New testrun created with name "{testrun_name}" and ID={self.testrun_id}')

    def close_test_run(self, testrun_id):
        """Close testrun."""
        response = self.client.send_post(URL.close_testrun.format(testrun_id), data={}, cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            print(f'[{self.tr_prefix}] Failed to close test run: "{error}"')
        else:
            print(f'[{self.tr_prefix}] Test run with ID={self.testrun_id} was closed')

    def close_test_plan(self, testplan_id):
        """Close test plan."""
        response = self.client.send_post(URL.close_testplan.format(testplan_id), data={}, cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            print(f'[{self.tr_prefix}] Failed to close test plan: "{error}"')
        else:
            print(f'[{self.tr_prefix}] Test plan with ID={self.testplan_id} was closed')

    @property
    def is_testrun_available(self) -> bool:
        """Ask if testrun is available in TestRail."""
        response = self.client.send_get(URL.get_testrun.format(self.testrun_id), cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            return False
        return response['is_completed'] is False

    @property
    def is_testplan_available(self) -> bool:
        """Ask if testplan is available in TestRail."""
        response = self.client.send_get(URL.get_testplan.format(self.testplan_id), cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            print(f'[{self.tr_prefix}] Failed to retrieve testplan: "{error}"')
            return False
        return response['is_completed'] is False

    def get_available_testruns(self, plan_id: int) -> List[int]:
        """Get a list of available testruns associated to a testplan in TestRail."""
        testruns_list = []
        response = self.client.send_get(URL.get_testplan.format(plan_id), cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            print(f'[{self.tr_prefix}] Failed to retrieve testplan: "{error}"')
        else:
            for entry in response['entries']:
                for run in entry['runs']:
                    if not run['is_completed']:
                        testruns_list.append(run['id'])
        return testruns_list

    def get_tests(self, run_id: int) -> List[Dict[str, Any]]:
        response = self.client.send_get(URL.get_tests.format(run_id), cert_check=self.cert_check)
        error = self.client.get_error(response)
        if error:
            print(f'[{self.tr_prefix}] Failed to get tests: "{error}"')
            raise TestsNotFoundException
        return response
