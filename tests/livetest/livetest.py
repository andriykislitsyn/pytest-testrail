import pytest
import time

from pytest_testrail.plugin import pytestrail


@pytestrail.case('C1788')
def test_func4():
    pytest.skip()


@pytestrail.case('C1789')
def test_func5():
    time.sleep(0.5)
